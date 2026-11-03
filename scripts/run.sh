#!/usr/bin/env bash
# Start harmoniq-server from the local .venv with tools/ on PATH.
#   ./scripts/run.sh          # production-style, single process
#   ./scripts/run.sh --dev    # auto-reload + Swagger UI at /docs
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
[ -x .venv/bin/python ] || { echo "No .venv found. Run ./scripts/setup.sh first." >&2; exit 1; }
export PATH="$ROOT/tools:$PATH"
exec .venv/bin/python -m app "$@"
