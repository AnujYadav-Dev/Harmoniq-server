"""TrackMetadata: the logical tags of one song, plus which provider supplied each field."""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field, fields

from app.domain.models import MetadataOverrides
from app.metadata.text import date_precision, normalize_date

LIST_FIELDS = ("artists", "genres")
# Fields that don't take part in merging / provenance.
_INTERNAL = {"sources", "musicbrainz", "extras"}


@dataclass
class TrackMetadata:
    title: str | None = None
    artists: list[str] = field(default_factory=list)
    album_artist: str | None = None
    album: str | None = None
    track_number: int | None = None
    track_total: int | None = None
    disc_number: int | None = None
    disc_total: int | None = None
    date: str | None = None  # YYYY-MM-DD, YYYY-MM or YYYY
    genres: list[str] = field(default_factory=list)
    isrc: str | None = None
    explicit: bool | None = None
    duration: int | None = None
    artwork_url: str | None = None
    lyrics: str | None = None
    lyrics_synced: bool = False
    lyrics_source: str | None = None
    musicbrainz: dict[str, str] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    extras: dict = field(default_factory=dict)  # provider hand-offs (album browse id, lyrics id, …); not serialised

    # -- merging ------------------------------------------------------------------------------------
    def fill(self, other: TrackMetadata | None, source: str) -> None:
        """Copy fields from `other` that are still empty here. Dates are also upgraded to a more precise value
        of the same year (a full release date beats a bare year)."""
        if other is None:
            return
        forced = set(other.extras.get("force", ()))  # fields a provider knows better than earlier ones
        for f in fields(self):
            name = f.name
            if name in _INTERNAL or name in ("lyrics_synced", "lyrics_source"):
                continue
            new = getattr(other, name)
            if new in (None, "", []):
                continue
            cur = getattr(self, name)
            if name == "date" and cur and not _better_date(new, cur) and name not in forced:
                continue
            if name != "date" and cur not in (None, "", []) and name not in forced:
                continue
            setattr(self, name, list(new) if name in LIST_FIELDS else new)
            self.sources[name] = source
            if name == "lyrics":
                self.lyrics_synced, self.lyrics_source = other.lyrics_synced, other.lyrics_source
        for k, v in other.musicbrainz.items():
            self.musicbrainz.setdefault(k, v)
        for k, v in other.extras.items():
            if k != "force":
                self.extras.setdefault(k, v)

    @property
    def main_artist(self) -> str | None:
        return self.album_artist or (self.artists[0] if self.artists else None)

    @property
    def year(self) -> int | None:
        return int(self.date[:4]) if self.date else None

    def to_public(self) -> dict:
        """JSON for the API / sidecar (lyrics text excluded; it has its own endpoint)."""
        data = asdict(self)
        for key in ("extras", "lyrics"):
            data.pop(key)
        data["year"] = self.year
        data["lyrics"] = {
            "available": bool(self.lyrics), "synced": self.lyrics_synced, "source": self.lyrics_source,
            "lines": len(self.lyrics.splitlines()) if self.lyrics else 0,
        }
        data.pop("lyrics_synced")
        data.pop("lyrics_source")
        return data


def _better_date(new: str, cur: str) -> bool:
    return new[:4] == cur[:4] and date_precision(new) > date_precision(cur)


def from_overrides(o: MetadataOverrides | None) -> TrackMetadata:
    if o is None:
        return TrackMetadata()
    artists = list(o.artists or ([o.artist] if o.artist else []))
    genres = list(o.genres or ([o.genre] if o.genre else []))
    return TrackMetadata(
        title=o.title, artists=artists, album_artist=o.album_artist, album=o.album,
        track_number=o.track_number, disc_number=o.disc_number,
        date=normalize_date(o.date) or (str(o.year) if o.year else None),
        genres=genres, artwork_url=o.artwork_url,
    )


@dataclass
class MetadataContext:
    """What providers can see. `info` (the yt-dlp info dict) arrives later when the resolver runs alongside the
    download; providers that need it call `wait_info()`."""

    video_id: str
    overrides: MetadataOverrides | None = None
    fetch_lyrics: bool = True
    enrich: bool = True  # MusicBrainz / Last.fm
    info: dict | None = None
    info_ready: threading.Event = field(default_factory=threading.Event)
    cancel: threading.Event = field(default_factory=threading.Event)
    warnings: list[str] = field(default_factory=list)

    def set_info(self, info: dict | None) -> None:
        self.info = info
        self.info_ready.set()

    def wait_info(self, timeout: float) -> dict | None:
        self.info_ready.wait(timeout)
        return self.info
