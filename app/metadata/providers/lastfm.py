"""Optional genre fallback from Last.fm artist top tags (only when LASTFM_API_KEY is set), as in the old notebook."""

from __future__ import annotations

from app.metadata.http import MetadataHttp, ThreadSafeTTL
from app.metadata.model import MetadataContext, TrackMetadata
from app.metadata.text import fold, genre_case

# Tags that describe people's opinions or nationality, not a genre.
SKIP_TAGS = {
    "seen live", "favorites", "favourite", "favorite", "love", "awesome", "classic", "american", "british", "indian",
    "female vocalists", "male vocalists", "all", "spotify", "youtube", "beautiful", "good", "00s", "90s", "80s",
}


class LastFmProvider:
    name = "lastfm"
    needs_enrich = True

    def __init__(self, http: MetadataHttp, api_key: str) -> None:
        self.http = http
        self.api_key = api_key
        self._cache = ThreadSafeTTL(maxsize=1024, ttl=7 * 86400)

    def enrich(self, ctx: MetadataContext, current: TrackMetadata) -> TrackMetadata | None:
        artist = current.main_artist
        if not self.api_key or current.genres or not artist:
            return None
        key = fold(artist)
        genres = self._cache.get(key)
        if genres is None:
            data = self.http.get_json("https://ws.audioscrobbler.com/2.0/", {
                "method": "artist.getTopTags", "artist": artist, "autocorrect": 1,
                "api_key": self.api_key, "format": "json",
            }) or {}
            tags = (data.get("toptags") or {}).get("tag") or []
            genres = [genre_case(t["name"]) for t in tags
                      if t.get("name") and t["name"].lower() not in SKIP_TAGS and int(t.get("count", 0)) >= 10][:3]
            self._cache.set(key, genres)
        return TrackMetadata(genres=list(genres)) if genres else None
