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
            return r.text, _extract_listing_details(body_text)
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
            print(f"  Listing details: {listing_details}")

        except PWTimeout:
            print("  [warn] page load timed out - using partial data")
        except Exception as e:
            print(f"  [warn] could not extract listing details: {e}")

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

After identifying rooms, also generate a task checklist for each room.
For each room generate EXACTLY {task_count} unique checklist items. Mix:
1. General everyday actions a guest might do in that room (e.g. "turn on the TV", "brew a pot of coffee")
2. Mess or damage scenarios a host would address after checkout (e.g. "wet towel on the floor", "grease on stovetop")
Aim for 80% mess/damage scenarios and 20% random activities that can be done in that room. Items should be 3-8 words, specific to the room. NO candle wax items.

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
      ],
      "tasks": ["item 1", "item 2"]
    }}
  ]
}}
photo_index and all photo_indices index values MUST be label numbers you actually saw.
STOP after the closing }} - do not add anything else."""


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
    "gemini-2.0-flash",
    "gemini-1.5-flash",
    "gemini-1.5-flash-8b",
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
    """Call Gemini with the given parts, trying models in order. Returns raw text."""
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
            return response.text.strip()
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


def analyze_with_gemini(image_urls, api_key, correction_hint="", listing_details=None, task_count=25):
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
        p = VISION_PROMPT.format(task_count=task_count)
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
    def _download_image(args):
        idx, url = args
        try:
            r = requests.get(url, headers=dl_headers, timeout=15)
            if r.status_code == 200:
                img = Image.open(BytesIO(r.content))
                if img.mode in ("RGBA", "P", "CMYK"):
                    img = img.convert("RGB")
                img.thumbnail((512, 512), Image.Resampling.LANCZOS)
                buf = BytesIO()
                img.save(buf, format="JPEG", quality=85)
                return idx, url, buf.getvalue()
        except Exception as e:
            print(f"  [warn] could not load image {url}: {e}")
        return idx, url, None

    results_map = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_download_image, (i, url)): i for i, url in enumerate(image_urls[:22])}
        for future in as_completed(futures):
            idx, url, data = future.result()
            if data:
                results_map[idx] = (url, data)

    loaded_urls = []
    image_parts = []
    for idx in sorted(results_map):
        url, data = results_map[idx]
        label_idx = len(loaded_urls)
        image_parts.append((
            genai_types.Part(text=f"[Photo {label_idx}]"),
            genai_types.Part.from_bytes(data=data, mime_type="image/jpeg"),
        ))
        loaded_urls.append(url)

    if not loaded_urls:
        raise ValueError(
            "None of the listing images could be downloaded. "
            "Airbnb may be restricting access - try a different listing URL."
        )

    def _run_vision(hint=""):
        p = [genai_types.Part(text=_build_prompt(hint))]
        for label, img in image_parts:
            p.append(label)
            p.append(img)
        print(f"  [1/1] Sending {len(loaded_urls)} images to Gemini (combined call)…")
        raw = _strip_fences(_gemini_call(client, models, p, max_tokens=32768))
        print(f"  Response (first 500 chars):\n{raw[:500]}")
        return _try_parse(raw)

    # ── Vision call with up to 2 internal retries if counts mismatch ─────────
    hint = correction_hint
    vision_result = _run_vision(hint)
    for attempt in range(2):
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
        hint = "Your previous response was wrong: " + "; ".join(issues) + ". Look at every photo again."
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

    return {
        "property_description": vision_result.get("property_description", ""),
        "rooms": rooms,
    }


# ─── Job Store ────────────────────────────────────────────────────────────────

import uuid
_jobs = {}  # job_id -> {"status": "pending"|"done"|"error", "result": ..., "error": ...}


def _run_job(job_id, airbnb_url, api_key, task_count=25):
    _start = time.time()
    try:
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
        result = analyze_with_gemini(image_urls, api_key, listing_details=listing_details, task_count=task_count)
        print(f"[{job_id}]   Identified {len(result.get('rooms', []))} room(s).")

        verification = verify_rooms(result, listing_details)
        print(f"[{job_id}] [4/4] Verification: {verification}")

        if not verification["passed"] and verification["issues"]:
            hint = "The actual listing has: " + "; ".join(verification["issues"]) + "."
            print(f"[{job_id}]   Retrying with correction hint: {hint}")
            result = analyze_with_gemini(image_urls, api_key, correction_hint=hint, listing_details=listing_details, task_count=task_count)
            verification = verify_rooms(result, listing_details)
            verification["retried"] = True
            print(f"[{job_id}]   Post-retry verification: {verification}")

        result["verification"] = verification
        result["task_count"] = task_count
        elapsed = round(time.time() - _start, 1)
        print(f"[{job_id}] ✓ Done in {elapsed}s")
        _jobs[job_id] = {"status": "done", "result": {"success": True, "image_count": len(image_urls), "data": result}}

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
    job_id = str(uuid.uuid4())
    _jobs[job_id] = {"status": "pending"}
    threading.Thread(target=_run_job, args=(job_id, airbnb_url, api_key, task_count), daemon=True).start()
    return jsonify({"job_id": job_id})


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

    all_tasks  = room.get("tasks", [])
    task_count = job["result"]["data"].get("task_count", len(all_tasks))
    available  = [t for t in all_tasks if t not in exclude]
    if len(available) < task_count:
        available = all_tasks  # reset if pool is nearly exhausted

    if task_count < 11:
        # Show all generated tasks as-is
        selected = random.sample(available, min(task_count, len(available)))
    else:
        # Pick a random number between 7-11 from the larger pool
        count    = random.randint(7, 11)
        selected = random.sample(available, min(count, len(available)))

    return jsonify({"tasks": selected})


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
