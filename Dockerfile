# syntax=docker/dockerfile:1.7
# Server mode: one image with Python, FFmpeg, Deno and the app (docs/deployment.md §4).
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DENO_INSTALL=/usr/local \
    DENO_DIR=/data/cache/deno \
    DATA_DIR=/data \
    TOOLS_DIR=""

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl unzip tini \
 && curl -fsSL https://deno.land/install.sh | sh -s -- --yes \
 && apt-get purge -y unzip && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.lock ./
RUN pip install -r requirements.lock

# yt-dlp in its own layer so it can be refreshed cheaply:
#   docker compose build --no-cache                       (latest)
#   docker compose build --build-arg YTDLP_SPEC="yt-dlp[default]==2026.10.26"
ARG YTDLP_SPEC="yt-dlp[default]"
RUN pip install --upgrade "$YTDLP_SPEC" \
 && ffmpeg -hide_banner -version | head -1 && ffprobe -hide_banner -version | head -1 \
 && deno --version | head -1 && python -c "import yt_dlp; print('yt-dlp', yt_dlp.version.__version__)"

COPY app ./app
RUN mkdir -p /data && chmod 0777 /data
EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "app"]
