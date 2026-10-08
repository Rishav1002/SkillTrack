#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
python3 -m venv "$ROOT/.venv"
source "$ROOT/.venv/bin/activate"
pip install -r "$ROOT/backend/requirements.txt"
cd "$ROOT/frontend"
npm install
npm run build
printf '\nSkillTrack is ready. Start it with: source .venv/bin/activate && python run_demo.py\n'
