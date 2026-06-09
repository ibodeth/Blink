#!/usr/bin/env bash
# Blink launcher (Linux/macOS).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$ROOT_DIR"

# Create / activate a virtual environment.
if [ ! -d ".venv" ]; then
  echo "[Blink] Creating virtual environment..."
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "[Blink] Installing dependencies..."
pip install --upgrade pip >/dev/null
pip install -r requirements.txt

# Build the frontend if Node is available and no build exists yet.
if command -v npm >/dev/null 2>&1 && [ ! -d "frontend/dist" ]; then
  echo "[Blink] Building frontend..."
  (cd frontend && npm install && npm run build)
fi

echo "[Blink] Starting Blink ($*)..."
exec python main.py "$@"
