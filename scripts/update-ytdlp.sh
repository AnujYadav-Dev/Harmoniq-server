#!/usr/bin/env bash
# Update yt-dlp (and its matching yt-dlp-ejs) inside .venv. Restart the server afterwards.
#   ./scripts/update-ytdlp.sh                  # latest stable
#   ./scripts/update-ytdlp.sh 2026.10.26       # pin a version
#   ./scripts/update-ytdlp.sh --nightly        # latest nightly (pre-release)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || { echo "No .venv found. Run ./scripts/setup.sh first." >&2; exit 1; }
case "${1:-}" in
  "")        "$PY" -m pip install --upgrade "yt-dlp[default]" ;;
  --nightly) "$PY" -m pip install --upgrade --pre "yt-dlp[default]" ;;
  *)         "$PY" -m pip install --upgrade "yt-dlp[default]==$1" ;;
esac
"$PY" -c "import yt_dlp; print('yt-dlp now', yt_dlp.version.__version__)"
echo "Restart the server to use the new version."
