#!/usr/bin/env bash
# scripts/setup_new_machine.sh
# ==============================
# Bootstrap Python venv + pip install on a new macOS/Linux PC.
# Does NOT install Docker, Node, Ollama — see docs/NEW_MACHINE_SETUP.md
#
# Usage (bash, from repo root):
#   chmod +x scripts/setup_new_machine.sh
#   ./scripts/setup_new_machine.sh

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "=== mytbot: setup_new_machine.sh ==="
echo "Repo: $ROOT"

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 not found. Install Python 3.12+." >&2
  exit 1
fi

python3 -c "import sys; assert sys.version_info[:2] >= (3, 12), 'Need Python 3.12+'" 2>/dev/null || {
  echo "WARNING: Python 3.12+ recommended. Current version:"
  python3 --version
}

if [[ ! -d .venv ]]; then
  echo "Creating .venv ..."
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

python -m pip install --upgrade pip
pip install -r requirements.txt

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo ""
  echo "Created .env from .env.example — edit POSTGRES_* and API keys before running."
else
  echo ".env already exists — not overwritten."
fi

echo ""
echo "Done (Python deps)."
echo "Next manual steps:"
echo "  1. Edit .env"
echo "  2. docker compose up -d"
echo "  3. alembic upgrade head"
echo "  4. Install Ollama + ollama pull qwen2.5:7b && ollama pull llama3.1:8b"
echo "  5. (cd ui && npm ci && npm run build)"
echo "  6. python run.py"
echo ""
echo "Full checklist: docs/NEW_MACHINE_SETUP.md"
