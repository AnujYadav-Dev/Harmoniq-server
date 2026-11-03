"""Lyrics: YouTube Music (timed, licensed) → LRCLIB (open API). Synced lyrics win over plain ones.

Output follows Harmoniq's LRC parser (utils/TimedContent.kt): no [ti:]/[ar:] header lines (they'd be shown as
lyrics) and timestamps as [mm:ss.mmm], because the parser reads the fraction as milliseconds.
"""

from __future__ import annotations

import logging
import re

from app.metadata.http import MetadataHttp
from app.metadata.model import MetadataContext, TrackMetadata
from app.providers.ytmusic_search import YTMusicSearch

log = logging.getLogger("harmoniq.metadata.lyrics")

LRCLIB_TIMEOUT = 5.0  # LRCLIB is often slow or overloaded; never let it eat the metadata budget
_TS = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")
_HEADER = re.compile(r"^\[(ti|ar|al|au|by|re|ve|length|offset|#)\s*:.*\]\s*$", re.IGNORECASE)


def ms_stamp(ms: int) -> str:
    ms = max(0, int(ms))
    minutes, rest = divmod(ms, 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"[{minutes:02d}:{seconds:02d}.{millis:03d}]"


def normalize_lrc(text: str) -> str:
    """Any LRC dialect → Harmoniq-friendly LRC: headers dropped, every timestamp rewritten as [mm:ss.mmm]."""
    out = []
    for line in text.splitlines():
        line = line.rstrip()
        if not line or _HEADER.match(line):
            continue
        stamps = list(_TS.finditer(line))
        if not stamps:
            continue  # untimed line inside synced lyrics would be shown at the wrong time
        lyric = _TS.sub("", line).strip()
        for m in stamps:  # "[00:10.00][00:50.00]chorus" → one line per timestamp
            frac = m.group(3) or "0"
            millis = int(frac.ljust(3, "0")[:3])  # .5 → 500, .34 → 340, .345 → 345
            out.append((int(m.group(1)) * 60_000 + int(m.group(2)) * 1000 + millis, lyric))
    out.sort(key=lambda p: p[0])
    return "\n".join(f"{ms_stamp(t)}{lyric}" for t, lyric in out)


def clean_plain(text: str) -> str:
    lines = [ln.rstrip() for ln in text.strip().splitlines()]
    return "\n".join(lines).strip()


class LyricsProvider:
    name = "lyrics"
    needs_enrich = False

    def __init__(self, music: YTMusicSearch, http: MetadataHttp, sources: list[str]) -> None:
        self.music = music
        self.http = http
        self.sources = [s for s in sources if s in ("ytmusic", "lrclib")]

    def enrich(self, ctx: MetadataContext, current: TrackMetadata) -> TrackMetadata | None:
        if not ctx.fetch_lyrics or not self.sources:
            return None
        plain: tuple[str, str] | None = None
        for source in self.sources:
            if ctx.cancel.is_set():
                break
            try:
                found = self._ytmusic(current) if source == "ytmusic" else self._lrclib(ctx, current)
            except Exception as exc:
                log.info("Lyrics from %s failed: %s", source, exc)
                continue
            if not found:
                continue
            text, synced, label = found
            if synced:
                return TrackMetadata(lyrics=text, lyrics_synced=True, lyrics_source=label)
            plain = plain or (text, label)
        if plain:
            return TrackMetadata(lyrics=plain[0], lyrics_synced=False, lyrics_source=plain[1])
        return None

    def _ytmusic(self, current: TrackMetadata) -> tuple[str, bool, str] | None:
        browse_id = current.extras.get("lyrics_id")
        if not browse_id:
            return None
        data = self.music.lyrics(browse_id)
        if not data or not data.get("lyrics"):
            return None
        source = (data.get("source") or "").replace("Source:", "").strip()
        label = f"YouTube Music ({source})" if source else "YouTube Music"
        lines = data["lyrics"]
        if data.get("hasTimestamps") and isinstance(lines, list):
            text = "\n".join(f"{ms_stamp(getattr(ln, 'start_time', 0))}{getattr(ln, 'text', '').strip()}"
                             for ln in lines)
            return text, True, label
        if isinstance(lines, str):
            return clean_plain(lines), False, label
        return None

    def _lrclib(self, ctx: MetadataContext, current: TrackMetadata) -> tuple[str, bool, str] | None:
        artist = current.main_artist
        if not current.title or not artist:
            return None
        params = {"track_name": current.title, "artist_name": artist}
        if current.album:
            params["album_name"] = current.album
        if current.duration:
            params["duration"] = current.duration
        status, hit = self.http.get_json_status("https://lrclib.net/api/get", params, retries=0,
                                                timeout=LRCLIB_TIMEOUT)
        if status == 404 and current.duration and not ctx.cancel.is_set():  # no exact match: try a search
            results = self.http.get_json("https://lrclib.net/api/search",
                                         {"track_name": current.title, "artist_name": artist},
                                         retries=0, timeout=LRCLIB_TIMEOUT) or []
            close = [r for r in results if isinstance(r, dict) and r.get("duration")
                     and abs(r["duration"] - current.duration) <= 3]
            hit = close[0] if close else None
        if not isinstance(hit, dict) or hit.get("instrumental"):
            return None
        if (synced := (hit.get("syncedLyrics") or "").strip()) and (lrc := normalize_lrc(synced)):
            return lrc, True, "LRCLIB"
        if plain := (hit.get("plainLyrics") or "").strip():
            return clean_plain(plain), False, "LRCLIB"
        return None
