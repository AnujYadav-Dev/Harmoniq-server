#!/usr/bin/env bash
# One-time (and re-runnable) setup for local mode on Linux / macOS.
# Creates .venv from your installed Python, installs all libraries into it, downloads FFmpeg,
# ffprobe and Deno into tools/, and creates data/, secrets/ and .env (with a fresh app token).
#
#   ./scripts/setup.sh                    # uses python3
#   ./scripts/setup.sh --python /usr/bin/python3.13
#   ./scripts/setup.sh --update-tools
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
TOOLS="$ROOT/tools"
PYTHON="python3"
UPDATE_TOOLS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --python) PYTHON="$2"; shift 2 ;;
    --update-tools) UPDATE_TOOLS=1; shift ;;
    *) echo "unknown option $1"; exit 2 ;;
  esac
done
step() { printf '\033[36m==> %s\033[0m\n' "$*"; }

# 1. Python >= 3.11
if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
  echo "Need Python 3.11+ (3.13 recommended). Pass --python <path>." >&2; exit 1
fi
step "Using Python: $("$PYTHON" -c 'import sys; print(sys.executable, sys.version.split()[0])')"

# 2. venv + libraries
[ -x .venv/bin/python ] || { step "Creating .venv"; "$PYTHON" -m venv .venv; }
step "Installing Python libraries into .venv"
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.lock
.venv/bin/python -m pip install --quiet --upgrade "yt-dlp[default]"

# 3. Tools
mkdir -p "$TOOLS"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
OS="$(uname -s)"; ARCH="$(uname -m)"
fetch() { curl -fsSL --retry 3 -o "$2" "$1"; }

if [ "$UPDATE_TOOLS" = 1 ] || [ ! -x "$TOOLS/ffmpeg" ] || [ ! -x "$TOOLS/ffprobe" ]; then
  step "Downloading FFmpeg"
  case "$OS-$ARCH" in
    Linux-x86_64)            build="linux64-gpl" ;;
    Linux-aarch64|Linux-arm64) build="linuxarm64-gpl" ;;
    Darwin-*)                build="" ;;
    *) echo "No FFmpeg build for $OS-$ARCH; install ffmpeg yourself and leave TOOLS_DIR empty." >&2; build="skip" ;;
  esac
  if [ -n "$build" ] && [ "$build" != "skip" ]; then
    fetch "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-$build.tar.xz" "$TMP/ffmpeg.tar.xz"
    tar -xJf "$TMP/ffmpeg.tar.xz" -C "$TMP"
    cp "$TMP"/ffmpeg-master-latest-"$build"/bin/ffmpeg "$TMP"/ffmpeg-master-latest-"$build"/bin/ffprobe "$TOOLS/"
  elif [ -z "$build" ]; then  # macOS: static builds from evermeet.cx (x86_64; runs under Rosetta on Apple Silicon)
    fetch "https://evermeet.cx/ffmpeg/getrelease/zip" "$TMP/ffmpeg.zip"
    fetch "https://evermeet.cx/ffmpeg/getrelease/ffprobe/zip" "$TMP/ffprobe.zip"
    .venv/bin/python -m zipfile -e "$TMP/ffmpeg.zip" "$TOOLS/"
    .venv/bin/python -m zipfile -e "$TMP/ffprobe.zip" "$TOOLS/"
  fi
  chmod +x "$TOOLS"/ffmpeg "$TOOLS"/ffprobe 2>/dev/null || true
fi

if [ "$UPDATE_TOOLS" = 1 ] || [ ! -x "$TOOLS/deno" ]; then
  step "Downloading Deno"
  case "$OS-$ARCH" in
    Linux-x86_64)               target="x86_64-unknown-linux-gnu" ;;
    Linux-aarch64|Linux-arm64)  target="aarch64-unknown-linux-gnu" ;;
    Darwin-x86_64)              target="x86_64-apple-darwin" ;;
    Darwin-arm64)               target="aarch64-apple-darwin" ;;
    *) echo "No Deno build for $OS-$ARCH" >&2; exit 1 ;;
  esac
  fetch "https://github.com/denoland/deno/releases/latest/download/deno-$target.zip" "$TMP/deno.zip"
  .venv/bin/python -m zipfile -e "$TMP/deno.zip" "$TOOLS/"
  chmod +x "$TOOLS/deno"
fi

# 4. Folders and .env
mkdir -p data secrets
if [ ! -f .env ]; then
  step "Creating .env with a new app token"
  token="$(.venv/bin/python -c "import secrets; print('hq_' + secrets.token_hex(32))")"
  sed "s/^APP_TOKENS=\$/APP_TOKENS=app-v1:$token/" .env.example > .env
  printf '\033[33m    App token (put this in the Android build later): %s\033[0m\n' "$token"
fi

# 5. Self-check
step "Installed versions"
.venv/bin/python -c "import sys, yt_dlp; print('python ', sys.version.split()[0]); print('yt-dlp ', yt_dlp.version.__version__)"
"$TOOLS/ffmpeg" -hide_banner -version 2>/dev/null | head -1 || command -v ffmpeg
"$TOOLS/deno" --version | head -1
printf '\n\033[32mSetup complete. Start the server with:  ./scripts/run.sh\033[0m\n'
