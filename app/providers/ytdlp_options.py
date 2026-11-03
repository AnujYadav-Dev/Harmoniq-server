"""Shared yt-dlp options: tool locations, cookies, proxy, PO-token provider, politeness."""

from __future__ import annotations

import logging

from app.config import Settings

log = logging.getLogger("harmoniq.yt-dlp")


class YtDlpLogger:
    """Routes yt-dlp's output into our logging and remembers the last error text."""

    def __init__(self) -> None:
        self.last_error: str | None = None

    def debug(self, msg: str) -> None:
        # yt-dlp sends info-level messages through debug() prefixed with "[".
        log.debug(msg)

    def info(self, msg: str) -> None:
        log.debug(msg)

    def warning(self, msg: str) -> None:
        log.warning(msg)

    def error(self, msg: str) -> None:
        # Logged as a warning: the client maps it to an error code (and may recover via another route).
        self.last_error = msg
        log.warning(msg)


def base_options(settings: Settings) -> dict:
    runtime = settings.ytdlp_js_runtime
    runtime_path = settings.tool(runtime)
    opts: dict = {
        "quiet": True,
        "noprogress": True,
        "no_warnings": False,
        "noplaylist": True,
        "allowed_extractors": ["youtube", "youtube:search"],
        "socket_timeout": 20,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 2,
        "concurrent_fragment_downloads": 1,
        "fixup": "never",  # our own FFmpeg step rewrites the container anyway
        "js_runtimes": {runtime: {"path": runtime_path} if runtime_path else {}},
        "cachedir": str(settings.data_dir / "cache" / "yt-dlp"),
        "sleep_interval_requests": settings.ytdlp_sleep_requests or None,
    }
    if ffmpeg := settings.tool("ffmpeg"):
        opts["ffmpeg_location"] = ffmpeg
    if cookies := settings.cookies_path:
        opts["cookiefile"] = cookies
    if settings.ytdlp_proxy:
        opts["proxy"] = settings.ytdlp_proxy
    if settings.pot_provider_url:
        opts["extractor_args"] = {"youtubepot-bgutilhttp": {"base_url": [settings.pot_provider_url]}}
    return opts
