"""MusicBrainz (free, no key, CC0): full release date, genres, disc n/total, ISRC and MBIDs.

Matching happens at **album level** (release group by album title + artist). Per-recording search was tested and
rejected: remixes and reissues outrank the original recording. Requests are paced to 1/second, as MusicBrainz
asks, and results are cached per album so the other songs of the same album cost nothing.
"""

from __future__ import annotations

import logging
import re

from app.metadata.http import MetadataHttp, ThreadSafeTTL
from app.metadata.model import MetadataContext, TrackMetadata
from app.metadata.text import artists_overlap, fold, genre_case, normalize_date, titles_match

log = logging.getLogger("harmoniq.metadata.musicbrainz")

API = "https://musicbrainz.org/ws/2"
ALLOWED_PRIMARY = {"Album", "EP", "Single"}
REJECTED_SECONDARY = {"Live", "Remix", "Karaoke", "Interview", "DJ-mix", "Demo", "Audiobook", "Spokenword",
                      "Mixtape/Street"}
_EDITION = re.compile(r"\s*[\(\[][^\)\]]*\b(deluxe|edition|expanded|remaster(ed)?|anniversary|bonus|version)\b"
                      r"[^\)\]]*[\)\]]", re.IGNORECASE)
MAX_GENRES = 3


def album_key(title: str) -> str:
    return fold(_EDITION.sub("", title or ""))


def _phrase(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


class MusicBrainzProvider:
    name = "musicbrainz"
    needs_enrich = True

    def __init__(self, http: MetadataHttp) -> None:
        self.http = http
        self.pace = http.pacer("musicbrainz.org", 1.1)
        self._albums = ThreadSafeTTL(maxsize=512, ttl=86400)

    def enrich(self, ctx: MetadataContext, current: TrackMetadata) -> TrackMetadata | None:
        artist = current.main_artist
        if not current.album or not artist:
            return None
        album = self._album(current.album, artist, current.artists)
        if not album:
            return None
        md = TrackMetadata(date=album["date"], genres=album["genres"])
        forced = ["date"] if album["date"] and len(album["date"]) == 10 else []  # first release beats YouTube's
        md.musicbrainz["release_group_id"] = album["release_group_id"]
        if album.get("release_id"):
            md.musicbrainz["release_id"] = album["release_id"]
        track = self._match_track(album["tracks"], current)
        if track:
            md.disc_number, md.disc_total = track["disc"], album["disc_total"]
            md.track_number, md.track_total = track["position"], track["track_count"]
            md.isrc = track["isrc"]
            if track.get("recording_id"):
                md.musicbrainz["recording_id"] = track["recording_id"]
            if (album["disc_total"] or 1) > 1:
                # YouTube Music numbers multi-disc albums straight through; per-disc numbering is correct.
                forced += ["track_number", "track_total"]
        if forced:
            md.extras["force"] = tuple(forced)
        return md

    # -- album lookup (cached) ----------------------------------------------------------------------
    def _album(self, album: str, artist: str, artists: list[str]) -> dict | None:
        key = (album_key(album), fold(artist))
        cached = self._albums.get(key)
        if cached is not None:
            return cached or None
        result = self._lookup_album(album, artist, artists)
        self._albums.set(key, result or {})
        return result

    def _lookup_album(self, album: str, artist: str, artists: list[str]) -> dict | None:
        found = self.http.get_json(f"{API}/release-group", {
            "query": f"releasegroup:{_phrase(_EDITION.sub('', album).strip() or album)} AND artist:{_phrase(artist)}",
            "fmt": "json", "limit": 10,
        }, pace=self.pace)
        group = self._pick_group((found or {}).get("release-groups") or [], album, [artist, *artists])
        if not group:
            log.info("No MusicBrainz album match for %r by %r", album, artist)
            return None
        detail = self.http.get_json(f"{API}/release-group/{group['id']}",
                                    {"inc": "genres+releases", "fmt": "json"}, pace=self.pace) or {}
        genres = [g for g in sorted(detail.get("genres") or [], key=lambda g: -g.get("count", 0))]
        strong = [g["name"] for g in genres if g.get("count", 0) >= 2][:MAX_GENRES]
        result = {
            "release_group_id": group["id"],
            "date": normalize_date(detail.get("first-release-date") or group.get("first-release-date")),
            "genres": [genre_case(g) for g in (strong or [g["name"] for g in genres[:1]])],
            "tracks": [], "disc_total": None, "release_id": None,
        }
        releases = [r for r in detail.get("releases") or [] if r.get("status") == "Official"]
        releases.sort(key=lambda r: r.get("date") or "9999")
        if releases:
            release = self.http.get_json(f"{API}/release/{releases[0]['id']}",
                                         {"inc": "recordings+isrcs", "fmt": "json"}, pace=self.pace) or {}
            media = release.get("media") or []
            result["release_id"] = release.get("id")
            result["disc_total"] = len(media) or None
            for medium in media:
                for t in medium.get("tracks") or []:
                    rec = t.get("recording") or {}
                    result["tracks"].append({
                        "disc": medium.get("position"), "position": t.get("position"),
                        "track_count": medium.get("track-count"), "title": t.get("title") or rec.get("title", ""),
                        "length": (t.get("length") or rec.get("length") or 0) // 1000 or None,
                        "isrc": (rec.get("isrcs") or [None])[0], "recording_id": rec.get("id"),
                    })
        return result

    @staticmethod
    def _pick_group(groups: list[dict], album: str, artists: list[str]) -> dict | None:
        wanted = album_key(album)
        good = []
        for g in groups:
            if album_key(g.get("title", "")) != wanted:
                continue
            if g.get("primary-type") not in ALLOWED_PRIMARY:
                continue
            if REJECTED_SECONDARY & set(g.get("secondary-types") or []):
                continue
            credit = [c.get("name") or (c.get("artist") or {}).get("name", "") for c in g.get("artist-credit") or []]
            if not artists_overlap(credit, artists):
                continue
            if int(g.get("score", 0)) < 80:
                continue
            good.append(g)
        good.sort(key=lambda g: (-int(g.get("score", 0)), g.get("first-release-date") or "9999"))
        return good[0] if good else None

    @staticmethod
    def _match_track(tracks: list[dict], current: TrackMetadata) -> dict | None:
        if not current.title:
            return None
        candidates = [t for t in tracks if titles_match(t["title"], current.title)]
        if current.duration:
            close = [t for t in candidates if not t["length"] or abs(t["length"] - current.duration) <= 5]
            candidates = close
        if len(candidates) > 1 and current.track_number:
            candidates = [t for t in candidates if t["position"] == current.track_number] or candidates[:1]
        return candidates[0] if candidates else None
