import os
import re
import json
import sys
import time
import random
import threading
import webbrowser
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup
from PIL import Image
from flask import Flask, render_template, request, jsonify
from google import genai
from google.genai import types as genai_types
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

app = Flask(__name__)

# ─── Listing Cache ─────────────────────────────────────────────────────────────

CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "listing_cache.json")

def _listing_id(url):
    m = re.search(r'/rooms/(\d+)', url)
    return m.group(1) if m else None

def _norm_url(url):
    return url.split("?")[0].split("#")[0].rstrip("/")

def _cache_key(listing_id_or_url, task_count, mode):
    return f"{listing_id_or_url}|{task_count}|{mode}"

def _load_cache():
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_cache(cache):
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        print(f"  [warn] cache save failed: {e}")

def _cache_lookup(url, task_count, mode):
    cache = _load_cache()
    lid = _listing_id(url)
    if lid:
        entry = cache.get(_cache_key(lid, task_count, mode))
        if entry:
            return entry
    # Fallback: normalized URL
    return cache.get(_cache_key(_norm_url(url), task_count, mode))

def _prune_lower_counts(cache, url, task_count, mode):
    """Remove any entries for the same listing+mode that have fewer tasks than task_count."""
    lid = _listing_id(url)
    norm = _norm_url(url)
    to_delete = []
    for key, entry in cache.items():
        if entry.get("mode") != mode:
            continue
        entry_tc = entry.get("task_count", 0)
        if entry_tc >= task_count:
            continue
        entry_lid = _listing_id(entry.get("url", ""))
        entry_norm = _norm_url(entry.get("url", ""))
        same = (lid and entry_lid == lid) or (entry_norm == norm)
        if same:
            to_delete.append(key)
    for k in to_delete:
        del cache[k]

def _cache_store(url, task_count, mode, job_result, address_hint):
    cache = _load_cache()
    entry = {
        "address_hint": address_hint,
        "url": _norm_url(url),
        "task_count": task_count,
        "mode": mode,
        "cached_at": int(time.time()),
        "job_result": job_result,
    }
    lid = _listing_id(url)
    if lid:
        cache[_cache_key(lid, task_count, mode)] = entry
    cache[_cache_key(_norm_url(url), task_count, mode)] = entry
    _prune_lower_counts(cache, url, task_count, mode)
    _save_cache(cache)


# ─── Airbnb Scraper ───────────────────────────────────────────────────────────

def find_images_recursive(data, depth=0):
    """Walk nested JSON and collect Airbnb CDN image URLs."""
    if depth > 15:
        return []
    images = []
    if isinstance(data, str):
        if "muscache.com" in data and "/pictures/" in data:
            images.append(data)
    elif isinstance(data, (list, tuple)):
        for item in data:
            images.extend(find_images_recursive(item, depth + 1))
    elif isinstance(data, dict):
        priority_keys = {"url", "picture", "photo", "image", "src", "baseUrl",
                         "large", "medium", "xlarge", "x_large", "scrimmed_url"}
        for key, value in data.items():
            if key.lower() in priority_keys and isinstance(value, str) and "muscache.com" in value:
                images.append(value)
            else:
                images.extend(find_images_recursive(value, depth + 1))
    return images


def _parse_images_from_html(html):
    """Extract CDN image URLs from rendered HTML."""
    images = set()

    # Parse Next.js embedded JSON
    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(1))
            for img in find_images_recursive(data):
                images.add(img.split("?")[0])
        except Exception:
            pass

    # Raw regex for CDN picture URLs
    for found in re.findall(r'https://a0\.muscache\.com/im/pictures/[^\s"\'<>?]+', html):
        images.add(found.split("?")[0])
    for found in re.findall(r'https://a0\.muscache\.com/pictures/[^\s"\'<>?]+', html):
        images.add(found.split("?")[0])

    # og:image meta tags
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("meta", property="og:image"):
        content = tag.get("content", "")
        if "muscache.com" in content:
            images.add(content.split("?")[0])

    # Filter to only likely listing photos (not icons/static assets)
    return [u for u in images if "/pictures/" in u]


_SCRAPE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.airbnb.com/",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "same-origin",
}


def _extract_listing_details(text):
    """Parse bedroom/bathroom counts and summary from raw page text."""
    details = {"bedrooms": None, "bathrooms": None, "summary": ""}
    bed = re.search(r'(\d+)\s+bedroom', text, re.IGNORECASE)
    if bed:
        details["bedrooms"] = int(bed.group(1))
    elif re.search(r'\bstudio\b', text, re.IGNORECASE):
        details["bedrooms"] = 0
    bath = re.search(r'([\d.]+)\s+bath', text, re.IGNORECASE)
    if bath:
        details["bathrooms"] = float(bath.group(1))
    summary = re.search(r'(\d+\s+guest[^·\n]*(?:·[^·\n]+){1,5})', text, re.IGNORECASE)
    if summary:
        details["summary"] = summary.group(1).strip()
    return details


def _try_fetch_direct(url):
    """Attempt to fetch listing HTML via plain HTTP. Returns (html, listing_details) or None."""
    try:
        r = requests.get(url, headers=_SCRAPE_HEADERS, timeout=15)
        if r.status_code == 200 and "__NEXT_DATA__" in r.text:
            print("  Fast path: fetched HTML directly (no browser needed).")
            soup = BeautifulSoup(r.text, "html.parser")
            body_text = soup.get_text(" ")
            details = _extract_listing_details(body_text)
            og = soup.find("meta", property="og:title")
            if og and og.get("content"):
                details["title"] = og["content"].split(" - Airbnb")[0].strip()
            return r.text, details
    except Exception as e:
        print(f"  Direct fetch failed: {e}")
    return None, None


def get_airbnb_images(url):
    """Return a deduplicated list of listing image URLs. Tries direct HTTP first, falls back to browser."""
    html, listing_details = _try_fetch_direct(url)

    if html:
        images = set(_parse_images_from_html(html))
        SKIP = ["airbnb-platform-assets", "AirbnbPlatformAssets",
                "Favicons", "favicon", "static/packages", "UserProfile",
                "search-bar", "category-icons", "/user/User/", "/User/original/"]
        listing_photos = [
            u for u in images
            if not any(skip in u for skip in SKIP) and re.search(r'/pictures/', u)
        ]
        if listing_photos:
            print(f"  Direct fetch found {len(listing_photos)} image(s).")
            return listing_photos, listing_details

    print("  Falling back to browser scrape…")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            },
        )
        # Mask automation signals
        ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            "window.chrome = { runtime: {} };"
        )
        page = ctx.new_page()

        seen_urls = set()
        listing_details = {"bedrooms": None, "bathrooms": None, "summary": ""}

        try:
            # Start collecting image URLs
            page.on("response", lambda r: seen_urls.add(r.url) if ("muscache.com" in r.url and "/pictures/" in r.url) else None)

            # Navigate directly to the listing
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(1000)

            # Scroll to trigger lazy-load
            for pct in [0.5, 1.0]:
                page.evaluate(f"window.scrollTo(0, document.body.scrollHeight * {pct})")
                page.wait_for_timeout(300)
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(500)

            # Try clicking into the photo gallery
            for selector in [
                'button[aria-label*="photo" i]',
                'button[data-testid*="photo"]',
                '[data-testid="photo-viewer-section"] button',
                'div[data-testid*="photo"] button',
            ]:
                try:
                    btn = page.query_selector(selector)
                    if btn:
                        btn.click()
                        page.wait_for_timeout(1000)
                        break
                except Exception:
                    pass

            # ── Extract listing details from page text ──────────────────────
            listing_details = _extract_listing_details(page.inner_text("body"))
            try:
                page_title = page.title()
                if page_title:
                    listing_details["title"] = page_title.split(" - Airbnb")[0].strip()
            except Exception:
                pass
            print(f"  Listing details: {listing_details}")

        except PWTimeout:
            print("  [warn] page load timed out - using partial data")
        except Exception as e:
            print(f"  [warn] could not extract listing details: {e}")

        try:
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        html = page.content()

        # Grab src attributes directly from rendered <img> tags
        try:
            img_srcs = page.eval_on_selector_all(
                'img[src*="muscache.com"]',
                "els => els.map(e => e.src)"
            )
            for src in img_srcs:
                seen_urls.add(src)
        except Exception:
            pass

        browser.close()

    images = set(_parse_images_from_html(html))
    for u in seen_urls:
        if "/pictures/" in u:
            images.add(u.split("?")[0])

    SKIP = ["airbnb-platform-assets", "AirbnbPlatformAssets",
            "Favicons", "favicon", "static/packages", "UserProfile",
            "search-bar", "category-icons", "/user/User/", "/User/original/"]
    listing_photos = [
        u for u in images
        if not any(skip in u for skip in SKIP) and re.search(r'/pictures/', u)
    ]

    print(f"  Browser fallback found {len(listing_photos)} listing image(s).")
    return listing_photos, listing_details


# ─── Gemini Analysis ──────────────────────────────────────────────────────────

VISION_PROMPT = """You are analyzing interior photos from an Airbnb vacation rental listing.
Each photo is labeled [Photo 0], [Photo 1], [Photo 2], etc. immediately before the image.

STEP 1 - Scan EVERY photo individually before grouping anything:
Go through each labeled photo one by one. For each photo write down (mentally) what room it shows.
Do NOT skip any photo. Every photo label must be assigned to exactly one interior room or skipped if outdoor/exterior.

EXCLUDE entirely — do not create a room entry for:
- Any outdoor or exterior space: patios, decks, balconies, pools, gardens, yards, driveways, building exteriors, views from windows.
- Any photo that is primarily outdoors even if a doorway or window is visible.

Room identification rules (interior only):
- Bedroom: bed, pillows, headboard, nightstands, wardrobe/dresser. Number multiple bedrooms (Bedroom 1, Bedroom 2, etc.).
- Bathroom: toilet, bathtub, shower, vanity with mirror, tiled wet-room floor, towel bars. Even if only partially visible. Half Bathroom = toilet + sink only.
- Classify any photo containing bathroom fixtures as a Bathroom - never as a Bedroom.
- Kitchen: countertops, stove/oven, refrigerator, kitchen sink, cabinets.
- Living Room: sofa/couch, coffee table, TV, armchairs.
- Dining Room: dining table with chairs.
- Other standard interior names: Foyer, Hallway, Laundry Room, Home Office.

STEP 2 - Group photos by room:
Create one entry per distinct room. Assign ALL photo labels for that room to photo_indices.
For each photo rate your confidence (0-100) that it truly shows that room.
Omit any photo with confidence below 70.
Pick the single best photo as photo_index.

STEP 3 - Verify counts before outputting:
Count your bedrooms. Count your bathrooms. These MUST match the listing details provided.
If the counts are off, re-examine the photos and correct your groupings before outputting.

Return ONLY a valid JSON object - no markdown - using this schema:

{{
  "property_description": "one-line description",
  "rooms": [
    {{
      "name": "Room Name",
      "emoji": "🏠",
      "photo_index": 0,
      "photo_indices": [
        {{"index": 0, "confidence": 95}},
        {{"index": 2, "confidence": 80}}
      ]
    }}
  ]
}}
photo_index and all photo_indices index values MUST be label numbers you actually saw.
STOP after the closing }} - do not add anything else."""


TASKS_PROMPT_FLAT = """You are generating a checklist for each room in an Airbnb vacation rental.

For each room listed below, generate EXACTLY {task_count} unique items using ONLY these two types:
1. Mess or cleanup scenarios a host would need to address after checkout (e.g. "wet towel on the floor", "grease splattered on stovetop", "toothpaste in sink", "sheets tangled and stained"). These should make up the vast majority of items.
2. Extremely common, simple actions a guest performs in that room (e.g. "turn on the light", "turn off the lamp", "turn on the TV", "close the blinds"). Only include actions that virtually every guest would do.
Do NOT include: activities, hobbies, games, cooking recipes, or anything creative. Items should be 3-8 words. NO candle wax items.

Rooms:
{room_list}

Return ONLY a valid JSON object - no markdown - using this schema:
{{"rooms": [{{"name": "Room Name", "tasks": ["item 1", "item 2"]}}]}}"""


TASKS_PROMPT_TEMPLATE = """You are generating a list of everyday household tasks for each room in a home.

For each room listed below, generate EXACTLY {task_count} unique tasks. Each task must:
- Be a normal, routine household chore or errand that anyone would do in that type of room
- Require 3-6 sequential physical steps to complete
- Have steps that flow in a natural, logical order

Good examples:
- "Change the bed sheets": ["Pull off the pillowcases and set aside", "Strip the fitted sheet and flat sheet from the mattress", "Put the dirty sheets in the laundry hamper", "Stretch the clean fitted sheet over each corner of the mattress", "Lay the flat sheet evenly on top", "Slide fresh pillowcases onto each pillow"]
- "Replace the toilet paper roll": ["Open the cabinet under the sink", "Take out a new roll", "Remove the empty cardboard tube from the holder", "Slide the new roll onto the holder", "Discard the cardboard tube"]
- "Unload the dishwasher": ["Open the dishwasher door", "Pull out the bottom rack", "Put away dishes and bowls in the cabinet", "Pull out the top rack", "Put away glasses and mugs", "Remove and put away the utensil basket"]

Only include tasks that are genuinely applicable to the specific room. Do not include Airbnb-specific or guest-specific tasks.

Rooms:
{room_list}

Return ONLY a valid JSON object - no markdown - using this exact schema:
{{"rooms": [{{"name": "Room Name", "tasks": [{{"name": "short task name", "steps": ["step 1", "step 2", "step 3"]}}]}}]}}"""


def _count_room_type(rooms, keyword):
    return sum(1 for r in rooms if keyword in r.get("name", "").lower())


def verify_rooms(result, listing_details):
    """Compare identified rooms against listing details. Returns verification dict."""
    rooms = result.get("rooms", [])
    found_beds  = _count_room_type(rooms, "bedroom")
    found_baths = _count_room_type(rooms, "bath")

    expected_beds  = listing_details.get("bedrooms")
    expected_baths = listing_details.get("bathrooms")

    issues = []
    if expected_beds is not None and found_beds != expected_beds:
        issues.append(
            f"listing shows {expected_beds} bedroom(s) but {found_beds} were identified"
        )
    if expected_baths is not None and found_baths != int(expected_baths):
        issues.append(
            f"listing shows {expected_baths} bathroom(s) but {found_baths} were identified"
        )

    return {
        "passed": len(issues) == 0,
        "expected_bedrooms":  expected_beds,
        "expected_bathrooms": expected_baths,
        "found_bedrooms":     found_beds,
        "found_bathrooms":    found_baths,
        "issues":             issues,
    }


FLASH_MODELS = [
    "gemini-2.0-flash-lite",
    "gemini-2.0-flash",
    "gemini-1.5-flash-8b",
    "gemini-1.5-flash",
]

def _get_available_models(client):
    """Return Gemini models with flash variants first, then fallback to others."""
    available = []
    try:
        for m in client.models.list():
            name = m.name
            if "gemini" in name and "generateContent" in (m.supported_actions or []):
                available.append(name)
        print(f"  Available models: {available}")
    except Exception as e:
        print(f"  Could not list models: {e}")

    flash = [m for m in FLASH_MODELS if any(m in a for a in available)]
    others = [m for m in available if not any(f in m for f in FLASH_MODELS) and "embedding" not in m]
    return flash + others if flash else others


def _gemini_call(client, models_to_try, parts, max_tokens=4096):
    """Call Gemini with the given parts, trying models in order. Returns (text, model_name)."""
    last_err = None
    for model_name in models_to_try:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=genai_types.Content(role="user", parts=parts),
                config=genai_types.GenerateContentConfig(
                    temperature=0.2,
                    max_output_tokens=max_tokens,
                ),
            )
            print(f"  Using model: {model_name}")
            return response.text.strip(), model_name
        except Exception as e:
            print(f"  Model {model_name} failed: {e}")
            last_err = e
            continue
    raise ValueError(f"No Gemini model available. Last error: {last_err}")


def _strip_fences(raw):
    if "```json" in raw:
        return raw.split("```json", 1)[1].split("```", 1)[0].strip()
    if "```" in raw:
        return raw.split("```", 1)[1].split("```", 1)[0].strip()
    return raw


def _strip_tasks_field(text):
    """Remove any 'tasks' key and its array value from raw JSON text.

    Handles both complete arrays and truncated (cut-off mid-array) responses.
    Task items are plain strings so they never contain nested brackets.
    """
    # Pass 1: remove complete "tasks": [ ... ] blocks (possibly with trailing comma)
    text = re.sub(
        r',?\s*"tasks"\s*:\s*\[(?:[^"]*"(?:[^"\\]|\\.)*")*[^"]*\]',
        '',
        text,
        flags=re.DOTALL,
    )
    # Pass 2: remove truncated "tasks": [ ... <EOF> and properly close open brackets
    match = re.search(r',?\s*"tasks"\s*:\s*\[', text, flags=re.DOTALL)
    if match:
        before = text[:match.start()]
        stack = []
        for ch in before:
            if ch == '{':
                stack.append('}')
            elif ch == '[':
                stack.append(']')
            elif ch in ']}' and stack and stack[-1] == ch:
                stack.pop()
        text = before + ''.join(reversed(stack))
    return text


def _try_parse(text):
    """Try several strategies to extract valid JSON from Gemini output."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    m = re.search(r'\{[\s\S]*\}', text)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass

    candidate = re.search(r'\{[\s\S]*', text)
    if candidate:
        partial = candidate.group()
        opens  = partial.count('{') - partial.count('}')
        aopens = partial.count('[') - partial.count(']')
        partial += ']' * max(aopens, 0) + '}' * max(opens, 0)
        try:
            return json.loads(partial)
        except json.JSONDecodeError:
            pass

    raise ValueError(
        f"Could not parse Gemini response as JSON.\n"
        f"Raw output (first 300 chars): {text[:300]}"
    )


def analyze_with_gemini(image_urls, api_key, correction_hint="", listing_details=None, task_count=25, image_bytes_list=None, mode="tasks"):
    client = genai.Client(api_key=api_key)
    models = _get_available_models(client)
    if not models:
        raise ValueError("No Gemini models available for this API key.")

    dl_headers = {
        "User-Agent": "Mozilla/5.0 (compatible; ImageFetcher/1.0)",
        "Referer": "https://www.airbnb.com/",
    }

    # ── CALL 1: Vision - identify rooms and photo indices ─────────────────────
    vision_prompt = VISION_PROMPT

    beds  = (listing_details or {}).get("bedrooms")
    baths = (listing_details or {}).get("bathrooms")
    summary = (listing_details or {}).get("summary", "")

    def _build_prompt(hint=""):
        p = VISION_PROMPT
        detail_lines = []
        if summary:
            detail_lines.append(f'Listing summary: "{summary}"')
        if beds is not None:
            detail_lines.append(f"This listing has EXACTLY {beds} bedroom(s) - you MUST identify all {beds}.")
        if baths is not None:
            detail_lines.append(f"This listing has EXACTLY {baths} bathroom(s) - you MUST identify all {int(baths)}.")
        if detail_lines:
            p += (
                "\n\nLISTING DETAILS - treat these as hard requirements:\n"
                + "\n".join(detail_lines)
            )
        if hint:
            p += f"\n\nCORRECTION NEEDED: {hint} Re-examine every photo carefully."
        return p

    # ── Download and resize images in parallel ────────────────────────────────
    img_size    = (384, 384)
    img_quality = 75

    loaded_urls = []
    image_parts = []

    if image_bytes_list is not None:
        # Upload mode: bytes already in memory, just resize
        print(f"  Processing {len(image_bytes_list)} uploaded image(s) at 384px…")
        for idx, raw_bytes in enumerate(image_bytes_list):
            try:
                img = Image.open(BytesIO(raw_bytes))
                if img.mode in ("RGBA", "P", "CMYK"):
                    img = img.convert("RGB")
                img.thumbnail(img_size, Image.Resampling.LANCZOS)
                buf = BytesIO()
                img.save(buf, format="JPEG", quality=img_quality)
                image_parts.append((
                    genai_types.Part(text=f"[Photo {len(loaded_urls)}]"),
                    genai_types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg"),
                ))
                loaded_urls.append(f"upload_{idx}")
            except Exception as e:
                print(f"  [warn] could not process uploaded image {idx}: {e}")
    else:
        # URL mode: download from Airbnb
        print(f"  Downloading {len(image_urls)} image(s) at 384px…")

        def _download_image(args):
            idx, url = args
            try:
                r = requests.get(url, headers=dl_headers, timeout=15)
                if r.status_code == 200:
                    img = Image.open(BytesIO(r.content))
                    if img.mode in ("RGBA", "P", "CMYK"):
                        img = img.convert("RGB")
                    img.thumbnail(img_size, Image.Resampling.LANCZOS)
                    buf = BytesIO()
                    img.save(buf, format="JPEG", quality=img_quality)
                    return idx, url, buf.getvalue()
            except Exception as e:
                print(f"  [warn] could not load image {url}: {e}")
            return idx, url, None

        results_map = {}
        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = {ex.submit(_download_image, (i, url)): i for i, url in enumerate(image_urls)}
            for future in as_completed(futures):
                idx, url, data = future.result()
                if data:
                    results_map[idx] = (url, data)

        for idx in sorted(results_map):
            url, data = results_map[idx]
            image_parts.append((
                genai_types.Part(text=f"[Photo {len(loaded_urls)}]"),
                genai_types.Part.from_bytes(data=data, mime_type="image/jpeg"),
            ))
            loaded_urls.append(url)

    if not loaded_urls:
        raise ValueError(
            "None of the listing images could be downloaded. "
            "Airbnb may be restricting access - try a different listing URL."
        )

    # ── Pre-filter: remove exterior/outdoor images ────────────────────────────
    if len(loaded_urls) > 4:
        filter_parts = [genai_types.Part(text=(
            "For each labeled photo below, decide if it shows an INTERIOR room "
            "(bedroom, bathroom, kitchen, living room, dining room, hallway, laundry, office, etc.). "
            "Exclude any photo that is primarily outdoors or exterior: "
            "patios, decks, balconies, pools, gardens, yards, driveways, building fronts, aerial views. "
            "Return ONLY a JSON array of the integer indices that are interior. "
            "Example: [0, 1, 3, 5]"
        ))]
        for label, img in image_parts:
            filter_parts.append(label)
            filter_parts.append(img)
        try:
            raw_f, _ = _gemini_call(client, models, filter_parts, max_tokens=512)
            raw_f = _strip_fences(raw_f)
            interior_indices = json.loads(raw_f)
            if isinstance(interior_indices, list) and len(interior_indices) >= 1:
                interior_set = set(int(i) for i in interior_indices if isinstance(i, int))
                removed = len(loaded_urls) - len(interior_set)
                print(f"  Pre-filter: keeping {len(interior_set)}/{len(loaded_urls)} interior images (removed {removed} exterior).")
                image_parts = [image_parts[i] for i in sorted(interior_set) if i < len(image_parts)]
                loaded_urls = [loaded_urls[i] for i in sorted(interior_set) if i < len(loaded_urls)]
                # Re-label after filtering so indices are contiguous
                image_parts = [
                    (genai_types.Part(text=f"[Photo {new_i}]"), img_part)
                    for new_i, (_, img_part) in enumerate(image_parts)
                ]
        except Exception as e:
            print(f"  [warn] Pre-filter failed, using all images: {e}")

    used_model = ["unknown"]

    def _run_vision(hint=""):
        p = [genai_types.Part(text=_build_prompt(hint))]
        for label, img in image_parts:
            p.append(label)
            p.append(img)
        print(f"  [1/2] Sending {len(loaded_urls)} images to Gemini…")
        raw, used_model[0] = _gemini_call(client, models, p, max_tokens=16384)
        raw = _strip_fences(raw)
        raw = _strip_tasks_field(raw)
        print(f"  Vision response (first 300 chars):\n{raw[:300]}")
        return _try_parse(raw)

    # ── Vision call with up to 3 internal retries if counts mismatch ─────────
    hint = correction_hint
    vision_result = _run_vision(hint)
    for attempt in range(3):
        rooms = vision_result.get("rooms", [])
        found_beds  = sum(1 for r in rooms if "bedroom" in r.get("name", "").lower())
        found_baths = sum(1 for r in rooms if "bath" in r.get("name", "").lower())
        issues = []
        if beds is not None and found_beds != beds:
            issues.append(f"found {found_beds} bedroom(s) but listing has {beds}")
        if baths is not None and found_baths != int(baths):
            issues.append(f"found {found_baths} bathroom(s) but listing has {int(baths)}")
        if not issues:
            break

        hint_parts = []
        if beds is not None and found_beds < beds:
            missing = beds - found_beds
            hint_parts.append(
                f"You identified {found_beds} bedroom(s) but this listing has EXACTLY {beds}. "
                f"You are missing {missing} bedroom(s). "
                f"ANY room containing a bed — including small rooms, lofts, or rooms you grouped with another — "
                f"must be its own separate Bedroom entry. "
                f"Scan every single photo for beds, headboards, pillows, or nightstands."
            )
        elif beds is not None and found_beds > beds:
            hint_parts.append(
                f"You identified {found_beds} bedroom(s) but this listing has only {beds}. "
                f"Some rooms you labeled as bedrooms are likely other room types."
            )
        if baths is not None and found_baths != int(baths):
            hint_parts.append(
                f"You identified {found_baths} bathroom(s) but this listing has {int(baths)}. "
                f"Check for any missed bathrooms or incorrectly labeled rooms."
            )
        hint = " ".join(hint_parts)
        print(f"  Vision mismatch (attempt {attempt+1}): {issues}. Retrying…")
        vision_result = _run_vision(hint)

    rooms = vision_result.get("rooms", [])
    if not rooms:
        raise ValueError("Gemini could not identify any rooms in the listing photos.")

    # Resolve photo indices to URLs while we still have loaded_urls in scope
    for room in rooms:
        primary_idx = room.pop("photo_index", None)
        raw_indices = room.pop("photo_indices", [])

        # Normalise: entries may be int or {"index": N, "confidence": N}
        confident_indices = []
        for entry in raw_indices:
            if isinstance(entry, dict):
                idx = entry.get("index")
                conf = entry.get("confidence", 100)
                if conf >= 70:
                    confident_indices.append(idx)
            elif isinstance(entry, int):
                # Old format (no confidence) - include as-is
                confident_indices.append(entry)

        # Ensure primary is in the list and comes first (if it passed confidence)
        if primary_idx is not None and primary_idx not in confident_indices:
            confident_indices = [primary_idx] + confident_indices

        seen_idx = set()
        photo_urls = []
        for idx in confident_indices:
            if isinstance(idx, int) and 0 <= idx < len(loaded_urls) and idx not in seen_idx:
                photo_urls.append(loaded_urls[idx])
                seen_idx.add(idx)
        room["photo_urls"] = photo_urls

    # ── Call 2: Text-only tasks generation ───────────────────────────────────
    # Pick prompt and per-token estimate based on mode.
    # Flat tasks ~20 tokens each; multi-step tasks ~60 tokens each.
    # Flash models cap at ~8192 output tokens, so split into per-room calls
    # when the total expected output would exceed that limit.
    if mode == "rearrange":
        task_prompt_template = TASKS_PROMPT_FLAT
        # Flat tasks are short strings; safe to batch up to 60 per call
        max_per_call = 60
        split_threshold = 4000
        tokens_per_task = 20
    else:
        task_prompt_template = TASKS_PROMPT_TEMPLATE
        # Multi-step tasks are verbose; cap at 20 per call to stay under 8192 tokens
        max_per_call = 20
        split_threshold = 3000
        tokens_per_task = 60

    estimated_tokens = len(rooms) * task_count * tokens_per_task
    tasks_by_name = {}
    print(f"  [2/2] Generating {mode} tasks for {len(rooms)} room(s)…")

    def _generate_room_tasks(room_name):
        """Generate all tasks for one room, sub-batching if task_count > max_per_call."""
        accumulated = []
        remaining = task_count
        while remaining > 0:
            batch = min(max_per_call, remaining)
            prompt = task_prompt_template.format(room_list=f"- {room_name}", task_count=batch)
            raw, _ = _gemini_call(client, models, [genai_types.Part(text=prompt)], max_tokens=8192)
            raw = _strip_fences(raw)
            result = _try_parse(raw)
            for r in result.get("rooms", []):
                accumulated.extend(r.get("tasks", []))
            remaining -= batch
        return room_name, accumulated

    if task_count > 40 or estimated_tokens > split_threshold:
        # Per-room calls (sequential) to avoid truncation on large task counts
        for room in rooms:
            name, tasks = _generate_room_tasks(room["name"])
            tasks_by_name[name] = tasks
    else:
        room_list = "\n".join(f"- {r['name']}" for r in rooms)
        tasks_prompt = task_prompt_template.format(room_list=room_list, task_count=task_count)
        raw2, _ = _gemini_call(client, models, [genai_types.Part(text=tasks_prompt)], max_tokens=16384)
        raw2 = _strip_fences(raw2)
        tasks_result = _try_parse(raw2)
        tasks_by_name = {r["name"]: r.get("tasks", []) for r in tasks_result.get("rooms", [])}
    for room in rooms:
        room["tasks"] = tasks_by_name.get(room["name"], [])

    # ── Quick text-only call for unique features ──────────────────────────────
    room_names = ", ".join(r["name"] for r in rooms)
    prop_desc  = vision_result.get("property_description", "")
    features_prompt = (
        f'An Airbnb listing described as: "{prop_desc}". '
        f'It contains these rooms: {room_names}. '
        f'List 3-8 special amenities or standout features visible in the listing that go beyond a standard house. '
        f'Examples: "Hot tub on deck", "Pool table", "Home theater", "Rooftop terrace", "Sauna", "Floor-to-ceiling windows". '
        f'Return ONLY a JSON array of strings, e.g. ["Feature 1", "Feature 2"]. No other text.'
    )
    unique_features = []
    try:
        raw_f, _ = _gemini_call(client, models, [genai_types.Part(text=features_prompt)], max_tokens=512)
        raw_f = _strip_fences(raw_f)
        unique_features = json.loads(raw_f)
        if not isinstance(unique_features, list):
            unique_features = []
        print(f"  Unique features: {unique_features}")
    except Exception as e:
        print(f"  [warn] Could not get unique features: {e}")

    return {
        "property_description": prop_desc,
        "unique_features": unique_features,
        "rooms": rooms,
        "model_used": used_model[0],
        "mode": mode,
    }


# ─── Job Store ────────────────────────────────────────────────────────────────

import uuid
_jobs = {}  # job_id -> {"status": "pending"|"done"|"error", "result": ..., "error": ...}


def _run_job(job_id, airbnb_url, api_key, task_count=25, mode="tasks"):
    _start = time.time()
    try:
        # ── Check cache ───────────────────────────────────────────────────────
        cached = _cache_lookup(airbnb_url, task_count, mode)
        if cached:
            addr = cached.get("address_hint", airbnb_url)
            print(f"[{job_id}] Cache hit: {addr}")
            job_result = dict(cached["job_result"])
            job_result["data"] = dict(job_result["data"])
            job_result["data"]["from_cache"] = True
            job_result["data"]["cached_at"] = cached.get("cached_at")
            job_result["data"]["address_hint"] = addr
            _jobs[job_id] = {"status": "done", "result": job_result}
            return

        print(f"\n[{job_id}] [1/4] Fetching listing: {airbnb_url}")
        image_urls, listing_details = get_airbnb_images(airbnb_url)
        if not image_urls:
            _jobs[job_id] = {"status": "error", "error": (
                "No images were found in that listing. "
                "Airbnb may have blocked the request. "
                "Try opening the URL in your browser first, then paste it here."
            )}
            return
        print(f"[{job_id}] [2/4] Found {len(image_urls)} image(s). Listing details: {listing_details}")

        print(f"[{job_id}] [3/4] Sending to Gemini…")
        result = analyze_with_gemini(image_urls, api_key, listing_details=listing_details, task_count=task_count, mode=mode)
        print(f"[{job_id}]   Identified {len(result.get('rooms', []))} room(s).")

        verification = verify_rooms(result, listing_details)
        print(f"[{job_id}] [4/4] Verification: {verification}")

        result["verification"] = verification
        result["task_count"] = task_count
        elapsed = round(time.time() - _start, 1)
        print(f"[{job_id}] ✓ Done in {elapsed}s")

        job_result = {"success": True, "image_count": len(image_urls), "data": result}
        address_hint = (
            (listing_details or {}).get("title")
            or result.get("property_description")
            or (listing_details or {}).get("summary")
            or ""
        )
        _cache_store(airbnb_url, task_count, mode, job_result, address_hint)
        _jobs[job_id] = {"status": "done", "result": job_result}

    except Exception as exc:
        print(f"[{job_id}] [error] {exc}")
        _jobs[job_id] = {"status": "error", "error": str(exc)}


# ─── Flask Routes ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/logo")
def serve_logo():
    from flask import Response
    r = requests.get(
        "https://static.microventures.com/img/offerings/812040ef97a86d3817a57dd008d77412.jpg",
        timeout=10,
    )
    return Response(r.content, mimetype="image/jpeg")


@app.route("/analyze", methods=["POST"])
def analyze():
    body = request.get_json(force=True)
    airbnb_url = (body.get("url") or "").strip()
    api_key    = os.environ.get("GEMINI_API_KEY", "")

    if not airbnb_url:
        return jsonify({"error": "Please provide an Airbnb URL."}), 400
    if "airbnb.com" not in airbnb_url:
        return jsonify({"error": "That doesn't look like an Airbnb URL."}), 400
    if not api_key:
        return jsonify({"error": "GEMINI_API_KEY environment variable not set."}), 500

    task_count = max(1, min(100, int(body.get("task_count", 25))))
    mode = body.get("mode", "tasks")
    if mode not in ("rearrange", "tasks"):
        mode = "tasks"
    job_id = str(uuid.uuid4())
    _jobs[job_id] = {"status": "pending"}
    threading.Thread(target=_run_job, args=(job_id, airbnb_url, api_key, task_count, mode), daemon=True).start()
    return jsonify({"job_id": job_id})


def _run_job_upload(job_id, image_bytes_list, api_key, task_count=25, mode="tasks"):
    _start = time.time()
    try:
        print(f"\n[{job_id}] [upload] Processing {len(image_bytes_list)} uploaded image(s).")
        # Store raw bytes so /image/<job_id>/<idx> can serve them back to the browser
        _jobs[job_id]["images"] = {i: b for i, b in enumerate(image_bytes_list)}
        result = analyze_with_gemini(
            [], api_key,
            task_count=task_count,
            image_bytes_list=image_bytes_list,
            mode=mode,
        )
        print(f"[{job_id}]   Identified {len(result.get('rooms', []))} room(s).")
        result["verification"] = {"passed": None, "issues": [], "expected_bedrooms": None, "expected_bathrooms": None}
        result["task_count"] = task_count
        elapsed = round(time.time() - _start, 1)
        print(f"[{job_id}] ✓ Done in {elapsed}s")
        _jobs[job_id]["status"] = "done"
        _jobs[job_id]["result"] = {"success": True, "image_count": len(image_bytes_list), "data": result}
    except Exception as exc:
        print(f"[{job_id}] [error] {exc}")
        _jobs[job_id] = {"status": "error", "error": str(exc)}


@app.route("/analyze-upload", methods=["POST"])
def analyze_upload():
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return jsonify({"error": "GEMINI_API_KEY environment variable not set."}), 500

    files = request.files.getlist("images")
    if not files or all(f.filename == "" for f in files):
        return jsonify({"error": "No images were uploaded."}), 400

    task_count = max(1, min(100, int(request.form.get("task_count", 25))))
    mode = request.form.get("mode", "tasks")
    if mode not in ("rearrange", "tasks"):
        mode = "tasks"

    image_bytes_list = []
    for f in files:
        if f.filename:
            image_bytes_list.append(f.read())

    if not image_bytes_list:
        return jsonify({"error": "No valid image files found in the upload."}), 400

    job_id = str(uuid.uuid4())
    _jobs[job_id] = {"status": "pending"}
    threading.Thread(target=_run_job_upload, args=(job_id, image_bytes_list, api_key, task_count, mode), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/image/<job_id>/<int:idx>")
def serve_upload_image(job_id, idx):
    from flask import Response
    job = _jobs.get(job_id)
    if not job:
        return ("Not found", 404)
    images = job.get("images", {})
    img_bytes = images.get(idx)
    if img_bytes is None:
        return ("Not found", 404)
    return Response(img_bytes, mimetype="image/jpeg")


@app.route("/status/<job_id>")
def status(job_id):
    job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job["status"] == "pending":
        return jsonify({"status": "pending"})
    if job["status"] == "error":
        return jsonify({"status": "error", "error": job["error"]}), 500
    return jsonify({"status": "done", **job["result"]})


@app.route("/randomize", methods=["POST"])
def randomize():
    body      = request.get_json(force=True)
    job_id    = body.get("job_id", "")
    room_name = body.get("room_name", "")
    exclude   = set(body.get("exclude", []))

    job = _jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "Job not found or not complete"}), 404

    rooms    = job["result"]["data"]["rooms"]
    room     = next((r for r in rooms if r["name"] == room_name), None)
    if not room:
        return jsonify({"error": "Room not found"}), 404

    all_tasks = room.get("tasks", [])
    mode = job["result"]["data"].get("mode", "tasks")

    if mode == "rearrange":
        available = [t for t in all_tasks if t not in exclude]
        if not available:
            available = all_tasks
        task_count = job["result"]["data"].get("task_count", len(all_tasks))
        count = task_count if task_count < 11 else random.randint(7, 11)
        selected = random.sample(available, min(count, len(available)))
        return jsonify({"tasks": selected})
    else:
        available = [t for t in all_tasks if t.get("name") not in exclude]
        if not available:
            available = all_tasks
        task = random.choice(available)
        return jsonify({"task": task})


@app.route("/check-cache", methods=["POST"])
def check_cache():
    """Return cached settings for a URL if it exists with different task_count/mode."""
    body       = request.get_json(force=True)
    url        = (body.get("url") or "").strip()
    task_count = int(body.get("task_count", 25))
    mode       = body.get("mode", "tasks")

    cache = _load_cache()
    lid   = _listing_id(url)

    for entry in cache.values():
        entry_lid = _listing_id(entry.get("url", ""))
        match = (lid and entry_lid == lid) or (_norm_url(entry.get("url", "")) == _norm_url(url))
        if match:
            cached_tc   = entry.get("task_count")
            cached_mode = entry.get("mode")
            if cached_tc != task_count or cached_mode != mode:
                return jsonify({
                    "found": True,
                    "cached_task_count": cached_tc,
                    "cached_mode": cached_mode,
                    "address_hint": entry.get("address_hint", ""),
                    "cached_at": entry.get("cached_at"),
                })
            break  # same settings → normal cache hit, no prompt needed

    return jsonify({"found": False})


@app.route("/cached-listings")
def cached_listings():
    cache = _load_cache()
    seen = set()
    listings = []
    for entry in sorted(cache.values(), key=lambda x: x.get("cached_at", 0), reverse=True):
        url = entry.get("url", "")
        dedup = _listing_id(url) or url
        if dedup in seen:
            continue
        seen.add(dedup)
        first_image = None
        rooms = entry.get("job_result", {}).get("data", {}).get("rooms", [])
        for room in rooms:
            for u in room.get("photo_urls", []):
                if u and not str(u).startswith("upload_"):
                    first_image = u
                    break
            if first_image:
                break
        listings.append({
            "url": url,
            "address_hint": entry.get("address_hint", ""),
            "cached_at": entry.get("cached_at"),
            "first_image": first_image,
            "room_count": len(rooms),
            "task_count": entry.get("task_count", 25),
            "mode": entry.get("mode", "tasks"),
        })
    return jsonify(listings[:20])


@app.route("/order-tasks", methods=["POST"])
def order_tasks():
    body       = request.get_json(force=True)
    room_name  = body.get("room_name", "room")
    task_names = body.get("task_names", [])

    if len(task_names) <= 1:
        return jsonify({"ordered": task_names})

    api_key = os.environ.get("GEMINI_API_KEY", "")
    client  = genai.Client(api_key=api_key)
    models  = _get_available_models(client)

    prompt = (
        f"You are helping organize tasks in a {room_name} to be completed efficiently.\n\n"
        f"Put these tasks in the most logical order to minimize unnecessary movement "
        f"and complete them all without backtracking:\n"
        f"{json.dumps(task_names)}\n\n"
        f"Return ONLY a JSON array of the exact task names in the optimal order. "
        f"Example: [\"task c\", \"task a\", \"task b\"]"
    )
    try:
        raw, _ = _gemini_call(client, models, [genai_types.Part(text=prompt)], max_tokens=256)
        raw    = _strip_fences(raw)
        ordered = json.loads(raw)
        if isinstance(ordered, list) and set(ordered) == set(task_names):
            return jsonify({"ordered": ordered})
    except Exception as e:
        print(f"  [warn] order-tasks failed: {e}")

    return jsonify({"ordered": task_names})  # fallback: original order


# ─── Entry Point ──────────────────────────────────────────────────────────────

def _open_browser(port):
    time.sleep(1.8)
    webbrowser.open(f"http://localhost:{port}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    is_local = os.environ.get("RAILWAY_ENVIRONMENT") is None

    # Only open browser once, and only when running locally
    if is_local and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        threading.Thread(target=_open_browser, args=(port,), daemon=True).start()
        print("=" * 55)
        print("  Airbnb Room Analyzer")
        print(f"  Server: http://localhost:{port}")
        print("  Press Ctrl+C to quit")
        print("=" * 55)

    app.run(debug=False, port=port, host="0.0.0.0")
