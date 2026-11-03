"""Public API models (they also generate the OpenAPI schema used by the Android client)."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
# YouTube Music returns browse ids ("VL" + playlist id); both forms are accepted
PLAYLIST_ID_RE = re.compile(r"^(VL)?([A-Za-z0-9_-]{10,64})$")
SEARCH_TYPES = ("song", "video", "playlist", "all")

AudioFormatName = Literal["m4a", "opus", "mp3"]
JobStatus = Literal[
    "queued", "resolving", "downloading", "processing", "tagging", "verifying",
    "completed", "failed", "cancelled", "expired",
]
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "expired"})


class Thumbnail(BaseModel):
    url: str
    width: int | None = None
    height: int | None = None


class Artist(BaseModel):
    name: str
    channel_id: str | None = None


class Album(BaseModel):
    name: str
    browse_id: str | None = None


class Channel(BaseModel):
    name: str | None = None
    id: str | None = None
    url: str | None = None


class SearchResult(BaseModel):
    video_id: str
    result_type: Literal["song", "video"]
    title: str
    artists: list[Artist] | None = None
    album: Album | None = None
    duration: int | None = None
    duration_text: str | None = None
    is_explicit: bool | None = None
    thumbnails: list[Thumbnail] = Field(default_factory=list)
    channel: Channel | None = None
    view_count: int | None = None
    watch_url: str


class PlaylistResult(BaseModel):
    result_type: Literal["playlist"] = "playlist"
    playlist_id: str
    title: str
    author: str | None = None
    item_count: int | None = None
    thumbnails: list[Thumbnail] = Field(default_factory=list)
    watch_url: str


class SearchSections(BaseModel):
    """`type=all`: one list per type. A failed section is empty and named in `errors` (section → error code)."""

    songs: list[SearchResult] = Field(default_factory=list)
    videos: list[SearchResult] = Field(default_factory=list)
    playlists: list[PlaylistResult] = Field(default_factory=list)
    errors: dict[str, str] = Field(default_factory=dict)


class SearchResponse(BaseModel):
    query: str
    type: Literal["song", "video", "playlist", "all"]
    source: Literal["youtube_music", "youtube", "mixed"]
    cached: bool = False
    results: list[SearchResult]
    playlists: list[PlaylistResult] | None = None
    sections: SearchSections | None = None


class PlaylistDetail(BaseModel):
    playlist_id: str
    title: str
    author: str | None = None
    description: str | None = None
    track_count: int | None = None
    duration: int | None = None
    thumbnails: list[Thumbnail] = Field(default_factory=list)
    watch_url: str
    tracks: list[SearchResult]
    truncated: bool = False


class VideoFlags(BaseModel):
    embeddable: bool
    age_restricted: bool
    is_live: bool
    downloadable: bool
    reason: str | None = None


class AudioFormat(BaseModel):
    format_id: str
    codec: str | None = None
    container: str | None = None
    abr_kbps: int | None = None
    sample_rate: int | None = None
    filesize_approx: int | None = None


class OutputOption(BaseModel):
    format: AudioFormatName
    transcode: bool
    est_size_bytes: int | None = None
    default: bool = False
    min_android_api: int | None = None


class VideoMetadata(BaseModel):
    video_id: str
    title: str
    track: str | None = None
    artist: str | None = None
    album: str | None = None
    release_year: int | None = None
    channel: Channel
    duration: int | None = None
    upload_date: str | None = None
    description: str | None = None
    view_count: int | None = None
    like_count: int | None = None
    categories: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    chapters: list[dict] = Field(default_factory=list)
    thumbnails: list[Thumbnail] = Field(default_factory=list)
    flags: VideoFlags
    audio_formats: list[AudioFormat] = Field(default_factory=list)
    output_options: list[OutputOption] = Field(default_factory=list)


class PreviewInfo(BaseModel):
    video_id: str
    embeddable: bool
    age_restricted: bool
    is_live: bool
    embed_url: str
    watch_url: str
    thumbnail: str | None = None
    title: str
    duration: int | None = None


class MetadataOverrides(BaseModel):
    """Optional values that win over everything the server finds (e.g. the song-search result the user tapped)."""

    title: str | None = Field(None, max_length=300)
    artist: str | None = Field(None, max_length=300, description="Single artist; use `artists` for several")
    artists: list[str] | None = Field(None, max_length=20)
    album_artist: str | None = Field(None, max_length=300)
    album: str | None = Field(None, max_length=300)
    year: int | None = Field(None, ge=1000, le=9999)
    date: str | None = Field(None, pattern=r"^\d{4}(-\d{2}(-\d{2})?)?$", description="YYYY, YYYY-MM or YYYY-MM-DD")
    track_number: int | None = Field(None, ge=1, le=9999)
    disc_number: int | None = Field(None, ge=1, le=99)
    genre: str | None = Field(None, max_length=100)
    genres: list[str] | None = Field(None, max_length=5)
    artwork_url: str | None = Field(
        None, max_length=1000,
        description="Optional cover URL (e.g. the square art from a song search result). "
                    "Only googleusercontent.com / ytimg.com hosts are accepted.",
    )


class DownloadRequest(BaseModel):
    video_id: str
    format: str | None = None
    quality: str = "best"
    metadata: MetadataOverrides | None = None
    embed_artwork: bool = True
    embed_lyrics: bool = Field(True, description="Embed synced (or plain) lyrics when found")
    enrich: bool = Field(True, description="Use MusicBrainz / Last.fm for release date, genres, disc, ISRC")


class LyricsInfo(BaseModel):
    available: bool
    synced: bool = False
    source: str | None = None
    lines: int = 0


class TrackInfo(BaseModel):
    """Resolved song metadata ("Find Music"): what the file will be tagged with."""

    video_id: str
    title: str | None = None
    artists: list[str] = Field(default_factory=list)
    album_artist: str | None = None
    album: str | None = None
    track_number: int | None = None
    track_total: int | None = None
    disc_number: int | None = None
    disc_total: int | None = None
    date: str | None = None
    year: int | None = None
    genres: list[str] = Field(default_factory=list)
    isrc: str | None = None
    explicit: bool | None = None
    duration: int | None = None
    artwork_url: str | None = None
    lyrics: LyricsInfo
    musicbrainz: dict[str, str] = Field(default_factory=dict)
    sources: dict[str, str] = Field(default_factory=dict)
    video_type: str | None = None
    suggested_filename: dict[str, str] = Field(default_factory=dict)
