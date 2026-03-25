#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────
#  Airbnb Room Analyzer – macOS / Linux launcher
# ─────────────────────────────────────────────────────────────────
set -e
cd "$(dirname "$0")"

# Require Python 3.9+
if ! command -v python3 &>/dev/null; then
  echo "ERROR: Python 3 is not installed."
  echo "Download it from https://www.python.org/downloads/"
  exit 1
fi

PY_VER=$(python3 -c 'import sys; print(sys.version_info[:2] >= (3,9))')
if [ "$PY_VER" != "True" ]; then
  echo "ERROR: Python 3.9 or newer is required."
  exit 1
fi

# Create / reuse virtual environment
if [ ! -d "venv" ]; then
  echo "Creating virtual environment…"
  python3 -m venv venv
fi

source venv/bin/activate

# Install / upgrade dependencies silently if needed
if ! python -c "import flask, google.genai, PIL, playwright" &>/dev/null 2>&1; then
  echo "Installing dependencies (one-time setup)…"
  pip install -q --upgrade pip
  pip install -q -r requirements.txt
  echo "Installing Chromium browser for Playwright…"
  playwright install chromium
fi

echo ""
echo "  ╔══════════════════════════════════════╗"
echo "  ║   Airbnb Room Analyzer  🏠           ║"
echo "  ║   Opening http://localhost:5000      ║"
echo "  ║   Press Ctrl+C to quit               ║"
echo "  ╚══════════════════════════════════════╝"
echo ""

python app.py
