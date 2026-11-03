"""Baseline metadata from the yt-dlp info dict: YouTube's own music fields, else title/channel heuristics."""

from __future__ import annotations

from app.metadata.model import MetadataContext, TrackMetadata
from app.metadata.text import clean_channel, normalize_date, split_title


class YouTubeProvider:
    name = "youtube"
    needs_enrich = False

    def __init__(self, info_timeout: float = 120.0) -> None:
        self.info_timeout = info_timeout

    def enrich(self, ctx: MetadataContext, current: TrackMetadata) -> TrackMetadata | None:
        info = ctx.wait_info(self.info_timeout)
        if not info:
            return None
        title_artist, title_song = split_title(info.get("title") or "")
        artists = list(info.get("artists") or ([info["artist"]] if info.get("artist") else []))
        if not artists:
            artists = [title_artist] if title_artist else ([c] if (c := clean_channel(info.get("channel")
                                                                                      or info.get("uploader"))) else [])
        return TrackMetadata(
            title=info.get("track") or title_song or info.get("title"),
            artists=artists,
            album=info.get("album"),
            date=normalize_date(info.get("release_date")) or normalize_date(info.get("release_year")),
            track_number=info.get("track_number"),
            duration=int(info["duration"]) if info.get("duration") else None,
        )
