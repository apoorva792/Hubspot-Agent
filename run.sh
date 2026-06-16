#!/usr/bin/env bash
# One-command launcher: sets up a venv, installs deps, and starts the server.
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "No .env found. Copy .env.example to .env and fill in your keys:"
  echo "  cp .env.example .env"
  exit 1
fi

if [ ! -d .venv ]; then
  echo "Creating virtual environment…"
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "Installing dependencies…"
pip install -q -r backend/requirements.txt

echo "Starting on http://localhost:8000"
cd backend
exec uvicorn main:app --reload --port 8000
