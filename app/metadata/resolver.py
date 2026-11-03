"""MetadataResolver: runs the providers in order and merges their answers. Never fails a job.

Order matters: each provider only fills fields that are still empty (see TrackMetadata.fill), so the most
trustworthy sources run first. Request overrides beat everything.
"""

from __future__ import annotations

import copy
import logging

from app.config import Settings
from app.metadata.http import MetadataHttp
from app.metadata.model import MetadataContext, TrackMetadata, from_overrides
from app.metadata.providers import MetadataProvider
from app.metadata.providers.lastfm import LastFmProvider
from app.metadata.providers.lyrics import LyricsProvider
from app.metadata.providers.musicbrainz import MusicBrainzProvider
from app.metadata.providers.youtube import YouTubeProvider
from app.metadata.providers.ytmusic import YTMusicProvider
from app.providers.ytmusic_search import YTMusicSearch

log = logging.getLogger("harmoniq.metadata")


def build_providers(settings: Settings, music: YTMusicSearch, http: MetadataHttp) -> list[MetadataProvider]:
    available = {
        "ytmusic": lambda: YTMusicProvider(music, settings.artwork_max_px),
        "youtube": lambda: YouTubeProvider(),
        "musicbrainz": lambda: MusicBrainzProvider(http),
        "lastfm": lambda: LastFmProvider(http, settings.lastfm_api_key) if settings.lastfm_api_key else None,
        "lyrics": lambda: LyricsProvider(music, http, settings.lyrics_provider_list),
    }
    providers = []
    for name in settings.metadata_provider_list:
        factory = available.get(name)
        if factory is None:
            log.warning("Unknown metadata provider %r in METADATA_PROVIDERS; ignored", name)
            continue
        if (provider := factory()) is not None:
            providers.append(provider)
    return providers


class MetadataResolver:
    def __init__(self, providers: list[MetadataProvider]) -> None:
        self.providers = providers

    @property
    def names(self) -> list[str]:
        return [p.name for p in self.providers]

    def resolve(self, ctx: MetadataContext, *, partial: list | None = None) -> TrackMetadata:
        """`partial`, if given, receives the TrackMetadata object straight away, so a caller that runs out of time
        can still use whatever was merged so far (see snapshot())."""
        md = from_overrides(ctx.overrides)
        skip = {"sources", "extras", "musicbrainz", "lyrics_synced", "lyrics_source"}
        for name, value in vars(md).items():
            if name not in skip and value not in (None, "", [], {}):
                md.sources[name] = "request"
        if partial is not None:
            partial.append(md)
        for provider in self.providers:
            if ctx.cancel.is_set():
                break
            if provider.needs_enrich and not ctx.enrich:
                continue
            try:
                md.fill(provider.enrich(ctx, md), provider.name)
            except Exception as exc:  # enrichment is best-effort by design
                log.warning("Metadata provider %s failed: %s", provider.name, exc)
                ctx.warnings.append(f"{provider.name}: {exc.__class__.__name__}")
        return md


def snapshot(md: TrackMetadata) -> TrackMetadata:
    return copy.deepcopy(md)
