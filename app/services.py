"""Everything the routes need, built once at startup and stored on `app.state.services`."""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field

from cachetools import TTLCache

from app.config import Settings
from app.jobs.manager import JobManager
from app.jobs.pipeline import DownloadPipeline
from app.jobs.registry import JobRegistry
from app.metadata.http import MetadataHttp
from app.metadata.resolver import MetadataResolver, build_providers
from app.providers.ytdlp_client import YouTubeHealth, YtDlpClient
from app.providers.ytmusic_search import YTMusicSearch
from app.storage.local import LocalFileStore


class RateLimiter:
    """Fixed-window counters kept in memory."""

    def __init__(self) -> None:
        self._counts: dict[tuple[str, str, int], int] = defaultdict(int)
        self._last_prune = time.time()

    def hit(self, bucket: str, ident: str, limit: int, window: int) -> tuple[bool, int, int]:
        """Returns (allowed, remaining, seconds until reset)."""
        now = time.time()
        index = int(now // window)
        key = (bucket, ident, index)
        if self._counts[key] >= limit:
            return False, 0, int((index + 1) * window - now) + 1
        self._counts[key] += 1
        self._prune(now)
        return True, limit - self._counts[key], int((index + 1) * window - now) + 1

    def _prune(self, now: float) -> None:
        if now - self._last_prune < 300:
            return
        self._last_prune = now
        for key in [k for k in self._counts if (k[2] + 2) * 3600 < now]:
            del self._counts[key]


@dataclass
class Services:
    settings: Settings
    store: LocalFileStore
    registry: JobRegistry
    health: YouTubeHealth
    ytdlp: YtDlpClient
    music: YTMusicSearch
    pipeline: DownloadPipeline
    jobs: JobManager
    resolver: MetadataResolver | None = None
    http: MetadataHttp | None = None
    limiter: RateLimiter = field(default_factory=RateLimiter)
    search_cache: TTLCache | None = None
    metadata_cache: TTLCache | None = None
    info_cache: TTLCache | None = None  # raw yt-dlp info dicts, shared by /videos and /track
    track_cache: TTLCache | None = None
    playlist_cache: TTLCache | None = None
    versions: dict = field(default_factory=dict)

    @classmethod
    def build(cls, settings: Settings) -> Services:
        store = LocalFileStore(settings.data_dir)
        registry = JobRegistry()
        health = YouTubeHealth()
        ytdlp = YtDlpClient(settings, health)
        music = YTMusicSearch()
        http = MetadataHttp(settings.musicbrainz_contact)
        resolver = MetadataResolver(build_providers(settings, music, http))
        pipeline = DownloadPipeline(settings, ytdlp, store, resolver)
        return cls(
            settings=settings, store=store, registry=registry, health=health, ytdlp=ytdlp,
            music=music, pipeline=pipeline, jobs=JobManager(settings, registry, pipeline),
            resolver=resolver, http=http,
            search_cache=TTLCache(maxsize=512, ttl=settings.search_cache_ttl),
            metadata_cache=TTLCache(maxsize=256, ttl=settings.metadata_cache_ttl),
            info_cache=TTLCache(maxsize=128, ttl=settings.metadata_cache_ttl),
            track_cache=TTLCache(maxsize=256, ttl=settings.metadata_cache_ttl),
            playlist_cache=TTLCache(maxsize=128, ttl=settings.metadata_cache_ttl),
        )

    def close(self) -> None:
        self.pipeline.shutdown()
        if self.http:
            self.http.close()
