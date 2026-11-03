"""Shared HTTP plumbing for metadata providers: one client, polite User-Agent, per-host pacing, small caches."""

from __future__ import annotations

import logging
import threading
import time

import httpx
from cachetools import TTLCache

from app import __version__

log = logging.getLogger("harmoniq.metadata")


class HostPacer:
    """Minimum interval between requests to one host (MusicBrainz asks for ≤ 1 request/second)."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self.interval
        if (delay := start - time.monotonic()) > 0:
            time.sleep(delay)


class MetadataHttp:
    def __init__(self, contact: str = "", timeout: float = 8.0) -> None:
        ua = f"HarmoniqServer/{__version__}"
        ua += f" ( {contact} )" if contact else " ( personal music server )"
        self.client = httpx.Client(timeout=timeout, follow_redirects=True,
                                   headers={"User-Agent": ua, "Accept": "application/json"})
        self._pacers: dict[str, HostPacer] = {}
        self._lock = threading.Lock()

    def pacer(self, host: str, interval: float) -> HostPacer:
        with self._lock:
            return self._pacers.setdefault(host, HostPacer(interval))

    def get_json(self, url: str, params: dict | None = None, *, pace: HostPacer | None = None,
                 retries: int = 1, timeout: float | None = None) -> dict | list | None:
        """GET → JSON, or None on 404 / repeated failure. Never raises."""
        return self.get_json_status(url, params, pace=pace, retries=retries, timeout=timeout)[1]

    def get_json_status(self, url: str, params: dict | None = None, *, pace: HostPacer | None = None,
                        retries: int = 1, timeout: float | None = None) -> tuple[int, dict | list | None]:
        """Like get_json, but also returns the HTTP status (0 = network error / timeout)."""
        status = 0
        for attempt in range(retries + 1):
            if pace:
                pace.wait()
            try:
                resp = self.client.get(url, params=params, timeout=timeout or httpx.USE_CLIENT_DEFAULT)
            except (httpx.HTTPError, RuntimeError) as exc:  # RuntimeError: client closed during shutdown
                log.info("GET %s failed: %s", url, exc)
                status = 0
                continue
            status = resp.status_code
            if status in (429, 502, 503) and attempt < retries:
                time.sleep(1.5)
                continue
            if status != 200:
                if status != 404:
                    log.info("GET %s → HTTP %s", url, status)
                return status, None
            try:
                return status, resp.json()
            except ValueError:
                return status, None
        return status, None

    def close(self) -> None:
        self.client.close()


class ThreadSafeTTL:
    def __init__(self, maxsize: int, ttl: float) -> None:
        self._cache = TTLCache(maxsize=maxsize, ttl=ttl)
        self._lock = threading.Lock()

    def get(self, key, default=None):
        with self._lock:
            return self._cache.get(key, default)

    def set(self, key, value) -> None:
        with self._lock:
            self._cache[key] = value
