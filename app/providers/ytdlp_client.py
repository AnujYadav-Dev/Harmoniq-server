"""Everything that calls yt-dlp: video search, metadata extraction and audio download."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import yt_dlp
from yt_dlp.utils import DownloadError, ExtractorError

from app.config import Settings
from app.domain.errors import AppError, JobCancelled
from app.providers.ytdlp_options import YtDlpLogger, base_options

log = logging.getLogger("harmoniq.youtube")
T = TypeVar("T")

WATCH_URL = "https://www.youtube.com/watch?v={}"


class YouTubeHealth:
    """Remembers recent bot-checks and the last canary run, for /v1/meta."""

    BLOCK_WINDOW = 600

    def __init__(self) -> None:
        self.last_blocked_at: float | None = None
        self.canary: dict | None = None

    def mark_blocked(self) -> None:
        self.last_blocked_at = time.time()

    @property
    def status(self) -> str:
        if self.last_blocked_at and time.time() - self.last_blocked_at < self.BLOCK_WINDOW:
            return "blocked"
        if self.canary and not self.canary.get("ok"):
            return "degraded"
        return "ok"


class Throttle:
    """Keeps a minimum gap between YouTube extractions across all threads."""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self, cancel: threading.Event | None = None) -> None:
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self.min_interval
        delay = start - time.monotonic()
        if delay > 0:
            if cancel is not None:
                if cancel.wait(delay):
                    raise JobCancelled()
            else:
                time.sleep(delay)


# (lower-case fragment of yt-dlp's message, error code) — first match wins.
_ERROR_PATTERNS: list[tuple[str, str]] = [
    ("not a bot", "upstream_blocked"),
    ("http error 429", "upstream_blocked"),
    ("too many requests", "upstream_blocked"),
    ("confirm your age", "age_restricted"),
    ("age-restricted", "age_restricted"),
    ("inappropriate for some users", "age_restricted"),
    ("live event will begin", "live_not_supported"),
    ("premieres in", "live_not_supported"),
    ("this live event", "live_not_supported"),
    ("private video", "video_unavailable"),
    ("members-only", "video_unavailable"),
    ("join this channel", "video_unavailable"),
    ("not available in your country", "video_unavailable"),
    ("has been removed", "video_unavailable"),
    ("account associated with this video has been terminated", "video_unavailable"),
    ("video unavailable", "video_unavailable"),
    ("this video is not available", "video_unavailable"),
    ("requires payment", "video_unavailable"),
    ("timed out", "upstream_timeout"),
    ("read timed out", "upstream_timeout"),
]


def map_ytdlp_error(exc: BaseException, health: YouTubeHealth | None = None) -> AppError:
    if isinstance(exc, AppError):
        return exc
    message = str(exc)
    lowered = message.lower()
    for fragment, code in _ERROR_PATTERNS:
        if fragment in lowered:
            if code == "upstream_blocked" and health is not None:
                health.mark_blocked()
            detail = {
                "upstream_blocked": "YouTube asked the server to confirm it is not a bot, or is rate limiting it.",
                "age_restricted": "This video is age-restricted and the server has no YouTube cookies.",
            }.get(code, _clean(message))
            return AppError(code, detail)
    log.warning("Unmapped yt-dlp error: %s", message)
    return AppError("upstream_error", _clean(message))


def _clean(message: str) -> str:
    return message.removeprefix("ERROR: ").replace("[youtube] ", "").strip()[:500]


class YtDlpClient:
    """Wraps yt-dlp. With YTDLP_IP_VERSION=auto, a bot-check on the default route is retried once over IPv6,
    and the route that worked is remembered. Shared (CGNAT) IPv4 addresses are often flagged while the same
    connection's IPv6 address is fine.
    """

    IPV6, IPV4 = "::", "0.0.0.0"

    def __init__(self, settings: Settings, health: YouTubeHealth) -> None:
        self.settings = settings
        self.health = health
        self.throttle = Throttle(settings.yt_min_interval_seconds)
        self._preferred: str | None = None  # source address that worked after a fallback

    def _ydl(self, source_address: str | None = None, **extra) -> tuple[yt_dlp.YoutubeDL, YtDlpLogger]:
        logger = YtDlpLogger()
        opts = base_options(self.settings) | {"logger": logger} | extra
        if source_address:
            opts["source_address"] = source_address
        return yt_dlp.YoutubeDL(opts), logger

    def _routes(self) -> list[str | None]:
        mode = self.settings.ytdlp_ip_version
        if mode == "4":
            return [self.IPV4]
        if mode == "6":
            return [self.IPV6]
        routes: list[str | None] = [self._preferred] if self._preferred else []
        for route in (None, self.IPV6):  # None = whatever the OS prefers
            if route not in routes:
                routes.append(route)
        return routes

    def _with_fallback(self, call: Callable[[str | None], T]) -> T:
        """Runs `call(source_address)`, moving to the next route only when YouTube bot-checks us."""
        routes = self._routes()
        first_error: AppError | None = None
        for index, route in enumerate(routes):
            try:
                result = call(route)
            except AppError as err:
                if err.code != "upstream_blocked":
                    if first_error is not None and err.code in ("upstream_error", "upstream_timeout"):
                        # the fallback route itself doesn't work here (e.g. no IPv6 connectivity)
                        log.info("Fallback route %s failed too: %s", route, err.detail)
                        break
                    raise
                first_error = first_error or err
                if index + 1 < len(routes):
                    log.warning("YouTube bot-checked route %s; retrying via %s", route or "default", routes[index + 1])
                continue
            if route != self._preferred:
                log.info("Using source address %s for YouTube from now on", route or "default")
            self._preferred = route
            return result
        self.health.mark_blocked()
        raise first_error

    def search(self, query: str, limit: int) -> list[dict]:
        def call(route: str | None) -> list[dict]:
            ydl, _ = self._ydl(route, extract_flat="in_playlist", skip_download=True)
            try:
                with ydl:
                    info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
            except (DownloadError, ExtractorError) as exc:
                raise map_ytdlp_error(exc) from exc
            return list((info or {}).get("entries") or [])
        return self._with_fallback(call)

    def extract(self, video_id: str) -> dict:
        """Full metadata including the format list, without downloading."""
        def call(route: str | None) -> dict:
            self.throttle.wait()
            ydl, _ = self._ydl(route, skip_download=True, ignore_no_formats_error=True, format="bestaudio/best")
            try:
                with ydl:
                    info = ydl.extract_info(WATCH_URL.format(video_id), download=False)
            except (DownloadError, ExtractorError) as exc:
                raise map_ytdlp_error(exc) from exc
            if not info:
                raise AppError("video_not_found")
            return ydl.sanitize_info(info)
        return self._with_fallback(call)

    def download(
        self,
        video_id: str,
        selector: str,
        out_dir: Path,
        *,
        check: Callable[[dict], None],
        on_progress: Callable[[dict], None],
        cancel: threading.Event,
    ) -> tuple[dict, Path]:
        """Extract, run `check(info)` (may raise AppError), then download the selected audio format."""

        def hook(d: dict) -> None:
            if cancel.is_set():
                raise JobCancelled()
            on_progress(d)

        def call(route: str | None) -> dict:
            self.throttle.wait(cancel)
            ydl, _ = self._ydl(
                route,
                format=selector,
                outtmpl={"default": str(out_dir / "source.%(ext)s")},
                progress_hooks=[hook],
            )
            try:
                with ydl:
                    info = ydl.extract_info(WATCH_URL.format(video_id), download=False)
                    if not info:
                        raise AppError("video_not_found")
                    check(info)
                    if cancel.is_set():
                        raise JobCancelled()
                    ydl.process_info(info)
            except (JobCancelled, AppError):
                raise
            except (DownloadError, ExtractorError) as exc:
                if cancel.is_set():
                    raise JobCancelled() from exc
                raise map_ytdlp_error(exc) from exc
            return info

        info = self._with_fallback(call)
        if cancel.is_set():
            raise JobCancelled()

        path = Path(info.get("filepath") or "")
        if not path.is_file():
            candidates = [p for p in out_dir.glob("source.*") if p.suffix not in (".part", ".ytdl")]
            if not candidates:
                raise AppError("upstream_error", "yt-dlp finished without producing a file.")
            path = candidates[0]
        return info, path


def yt_dlp_version() -> str:
    return yt_dlp.version.__version__
