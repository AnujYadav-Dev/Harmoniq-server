"""YouTube Music catalogue: canonical artists, album, year, track n/total, square album art, lyrics id.

Music videos (OMV) and uploads (UGC) usually have no album; for those the provider searches the song catalogue
for the same recording and borrows its album data (metadata only; the audio stays the video the user picked).
"""

from __future__ import annotations

import logging

from app.media.artwork import upscale_music_art
from app.metadata.http import ThreadSafeTTL
from app.metadata.model import MetadataContext, TrackMetadata
from app.metadata.text import artists_overlap, is_non_original, normalize_date, split_title, titles_match
from app.providers.ytmusic_search import YTMusicSearch

log = logging.getLogger("harmoniq.metadata.ytmusic")

ATV = "MUSIC_VIDEO_TYPE_ATV"  # "Art Track": the album audio of a song


def parse_length(text: str | None) -> int | None:
    if not text:
        return None
    try:
        parts = [int(p) for p in str(text).split(":")]
    except ValueError:
        return None
    seconds = 0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds


class YTMusicProvider:
    name = "ytmusic"
    needs_enrich = False

    def __init__(self, music: YTMusicSearch, artwork_px: int = 1200) -> None:
        self.music = music
        self.artwork_px = artwork_px
        self._albums = ThreadSafeTTL(maxsize=256, ttl=86400)

    def _album(self, browse_id: str) -> dict | None:
        album = self._albums.get(browse_id)
        if album is None:
            album = self.music.album(browse_id)
            self._albums.set(browse_id, album)
        return album

    def enrich(self, ctx: MetadataContext, current: TrackMetadata) -> TrackMetadata | None:
        track, lyrics_id = self.music.watch_track(ctx.video_id)
        if not track:
            return None
        md = TrackMetadata()
        video_type = track.get("videoType")
        artists = [a["name"] for a in track.get("artists") or [] if a.get("name")]
        title = track.get("title") or ""
        if video_type != ATV:
            title, artists = self._clean_video_title(title, artists, video_type)
        md.title, md.artists = title or None, artists
        md.duration = parse_length(track.get("length"))
        md.date = normalize_date(track.get("year"))
        md.extras.update(video_type=video_type, lyrics_id=lyrics_id)

        album_ref = track.get("album") or {}
        album_id = album_ref.get("id")
        md.album = album_ref.get("name")
        catalog_video_id = ctx.video_id
        if not album_id and title and artists:
            hit = self._find_catalog_song(title, artists, md.duration)
            if hit:
                album_id = (hit.get("album") or {}).get("id")
                md.album = (hit.get("album") or {}).get("name")
                catalog_video_id = hit["videoId"]
                md.extras["catalog_video_id"] = catalog_video_id
                if not lyrics_id:
                    _, md.extras["lyrics_id"] = self.music.watch_track(catalog_video_id)
        if album_id:
            md.extras["album_id"] = album_id
            self._apply_album(md, album_id, catalog_video_id)
        return md

    @staticmethod
    def _clean_video_title(title: str, artists: list[str], video_type: str | None) -> tuple[str, list[str]]:
        """'Artist - Song (Official Video)' → 'Song'. For uploads (UGC) the 'Artist' part replaces the channel."""
        title_artist, song = split_title(title)
        if title_artist:
            if artists_overlap([title_artist], artists):
                return song, artists
            if video_type == "MUSIC_VIDEO_TYPE_UGC" or not artists:
                return song, [title_artist]
            return song, artists
        return song, artists

    def _find_catalog_song(self, title: str, artists: list[str], duration: int | None) -> dict | None:
        try:
            results = self.music.search_songs(f"{artists[0]} {title}", 5)
        except Exception as exc:
            log.info("Catalogue search failed: %s", exc)
            return None
        for r in results:
            r_artists = [a.get("name", "") for a in r.get("artists") or []]
            r_duration = r.get("duration_seconds")
            if not titles_match(r.get("title", ""), title) or not artists_overlap(r_artists, artists):
                continue
            if duration and r_duration and abs(r_duration - duration) > 15:
                continue
            if is_non_original(r.get("title", ""), title) or not r.get("album"):
                continue
            return r
        return None

    def _apply_album(self, md: TrackMetadata, album_id: str, video_id: str) -> None:
        album = self._album(album_id)
        if not album:
            return
        md.album = album.get("title") or md.album
        album_artists = [a["name"] for a in album.get("artists") or [] if a.get("name")]
        if album_artists:
            md.album_artist = album_artists[0]
        tracks = album.get("tracks") or []
        md.track_total = album.get("trackCount") or (len(tracks) or None)
        if not md.date:
            md.date = normalize_date(album.get("year"))
        thumbs = sorted(album.get("thumbnails") or [], key=lambda t: t.get("width") or 0)
        if thumbs:
            md.artwork_url = upscale_music_art(thumbs[-1]["url"], self.artwork_px)

        match = next((t for t in tracks if t.get("videoId") == video_id), None)
        if match is None and md.title:
            # The album often lists a different video id (music video vs. audio) for the same song.
            candidates = [t for t in tracks if titles_match(t.get("title", ""), md.title)]
            if md.duration:
                candidates = [t for t in candidates
                              if not t.get("duration_seconds") or abs(t["duration_seconds"] - md.duration) <= 3]
            match = candidates[0] if len(candidates) == 1 else None
        if match is not None:
            md.track_number = match.get("trackNumber") or (tracks.index(match) + 1)
            md.explicit = match.get("isExplicit")
            if not md.artists:
                md.artists = [a["name"] for a in match.get("artists") or [] if a.get("name")]
        if md.track_total == 1 and md.track_number is None:
            md.track_number = 1
