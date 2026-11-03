"""YouTube Music catalogue access via ytmusicapi (unauthenticated): song search, track/album lookups, lyrics."""

from __future__ import annotations

import threading

from ytmusicapi import YTMusic


class YTMusicSearch:
    def __init__(self) -> None:
        self._client: YTMusic | None = None
        self._lock = threading.Lock()

    def _yt(self) -> YTMusic:
        if self._client is None:
            self._client = YTMusic()
        return self._client

    def search_songs(self, query: str, limit: int) -> list[dict]:
        with self._lock:  # one shared HTTP session; calls are short
            results = self._yt().search(query, filter="songs", limit=limit)
        return [r for r in results if r.get("videoId")][:limit]

    def watch_track(self, video_id: str) -> tuple[dict | None, str | None]:
        """Canonical catalogue entry for a video: (track dict, lyrics browse id)."""
        with self._lock:
            wp = self._yt().get_watch_playlist(videoId=video_id, limit=1)
        tracks = wp.get("tracks") or []
        track = next((t for t in tracks if t.get("videoId") == video_id), tracks[0] if tracks else None)
        return track, wp.get("lyrics")

    def album(self, browse_id: str) -> dict:
        with self._lock:
            return self._yt().get_album(browse_id)

    def lyrics(self, browse_id: str) -> dict | None:
        with self._lock:
            try:
                return self._yt().get_lyrics(browse_id, timestamps=True)
            except TypeError:  # older ytmusicapi without timestamp support
                return self._yt().get_lyrics(browse_id)
