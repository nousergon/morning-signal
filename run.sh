#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

set -a
source "$PROJECT_DIR/.env"
set +a

exec "$PROJECT_DIR/.venv/bin/python" generate_episode.py "$@"
