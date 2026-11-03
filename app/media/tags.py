"""Writing TrackMetadata into m4a / mp3 / opus files, and the suggested filename.

The mapping targets TagLib's property map, which is what Harmoniq's `:metaphony` reader uses
(docs/server-implementation-plan.md §2 and §3.6). tests/test_tagging.py reads the result back with TagLib itself.
"""

from __future__ import annotations

import base64
import re
import unicodedata
from pathlib import Path

from mutagen.flac import Picture
from mutagen.id3 import (
    APIC, COMM, ID3, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK, TSRC, TSSE, TXXX, UFID, USLT, WOAS,
)
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from mutagen.oggopus import OggOpus

from app.metadata.model import TrackMetadata

# Bump when the tagging output changes, so cached files are rebuilt instead of served with old tags.
TAGGER_VERSION = "3"
# Harmoniq splits ARTIST/GENRE on ";" "/" "," "+" by default; "; " also reads well in other players.
JOIN = "; "

_ITUNES = "----:com.apple.iTunes:"
_MB_KEYS = {  # TrackMetadata.musicbrainz key → (MP4/ID3 TXXX description, Vorbis key)
    "release_group_id": ("MusicBrainz Release Group Id", "MUSICBRAINZ_RELEASEGROUPID"),
    "release_id": ("MusicBrainz Album Id", "MUSICBRAINZ_ALBUMID"),
    "recording_id": ("MusicBrainz Track Id", "MUSICBRAINZ_TRACKID"),
}


def display_filename(md: TrackMetadata, ext: str, template: str = "{artist} - {title}", fallback: str = "audio") -> str:
    values = {
        "artist": md.main_artist or "Unknown Artist", "title": md.title or fallback, "album": md.album or "",
        "year": str(md.year or ""), "track": f"{md.track_number:02d}" if md.track_number else "",
    }
    base = template.format(**values) if md.main_artist else (md.title or fallback)
    base = unicodedata.normalize("NFC", base)
    base = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", base)
    base = re.sub(r"\s+", " ", base).strip(" .-") or fallback
    return f"{base[:150].rstrip(' .')}.{ext}"


def _n_of(n: int | None, total: int | None) -> str:
    return f"{n}/{total}" if total else str(n)


def write_tags(path: Path, fmt: str, md: TrackMetadata, cover: bytes | None, *, video_id: str,
               encoder: str) -> None:
    watch_url = f"https://www.youtube.com/watch?v={video_id}"
    source = f"youtube:{video_id}"
    artist = JOIN.join(md.artists) if md.artists else None
    genre = JOIN.join(md.genres) if md.genres else None
    {"m4a": _write_mp4, "mp3": _write_id3, "opus": _write_vorbis}[fmt](
        path, md, cover, artist=artist, genre=genre, watch_url=watch_url, source=source, encoder=encoder)


def _write_mp4(path, md, cover, *, artist, genre, watch_url, source, encoder):
    audio = MP4(path)
    audio.delete()
    if audio.tags is None:
        audio.add_tags()
    t = audio.tags
    t["\xa9nam"] = [md.title or ""]
    if artist:
        t["\xa9ART"] = [artist]
        t[_ITUNES + "ARTISTS"] = [MP4FreeForm(a.encode()) for a in md.artists]
    if md.album_artist:
        t["aART"] = [md.album_artist]
    if md.album:
        t["\xa9alb"] = [md.album]
    if md.track_number:
        t["trkn"] = [(md.track_number, md.track_total or 0)]
    if md.disc_number:
        t["disk"] = [(md.disc_number, md.disc_total or 0)]
    if md.date:
        t["\xa9day"] = [md.date]
    if genre:
        t["\xa9gen"] = [genre]
    if md.lyrics:
        t["\xa9lyr"] = [md.lyrics]
    if md.isrc:
        t[_ITUNES + "ISRC"] = [MP4FreeForm(md.isrc.encode())]
    if md.explicit is not None:
        t["rtng"] = [1 if md.explicit else 0]
    for key, value in md.musicbrainz.items():
        if key in _MB_KEYS:
            t[_ITUNES + _MB_KEYS[key][0]] = [MP4FreeForm(value.encode())]
    t["\xa9cmt"] = [watch_url]
    t["\xa9too"] = [encoder]
    t["----:com.harmoniq:source"] = [MP4FreeForm(source.encode())]
    if cover:
        t["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG)]
    audio.save()


def _write_id3(path, md, cover, *, artist, genre, watch_url, source, encoder):
    audio = MP3(path)
    audio.delete()
    audio.tags = ID3()
    t = audio.tags
    t.add(TIT2(encoding=3, text=md.title or ""))
    if artist:
        t.add(TPE1(encoding=3, text=artist))
        t.add(TXXX(encoding=3, desc="ARTISTS", text=md.artists))
    if md.album_artist:
        t.add(TPE2(encoding=3, text=md.album_artist))
    if md.album:
        t.add(TALB(encoding=3, text=md.album))
    if md.track_number:
        t.add(TRCK(encoding=3, text=_n_of(md.track_number, md.track_total)))
    if md.disc_number:
        t.add(TPOS(encoding=3, text=_n_of(md.disc_number, md.disc_total)))
    if md.date:
        t.add(TDRC(encoding=3, text=md.date))
    if genre:
        t.add(TCON(encoding=3, text=genre))
    if md.lyrics:
        t.add(USLT(encoding=3, lang="eng", desc="", text=md.lyrics))  # empty desc → TagLib "LYRICS"
    if md.isrc:
        t.add(TSRC(encoding=3, text=md.isrc))
    if md.explicit is not None:
        t.add(TXXX(encoding=3, desc="ITUNESADVISORY", text="1" if md.explicit else "0"))
    for key, value in md.musicbrainz.items():
        if key == "recording_id":
            t.add(UFID(owner="http://musicbrainz.org", data=value.encode()))
        elif key in _MB_KEYS:
            t.add(TXXX(encoding=3, desc=_MB_KEYS[key][0], text=value))
    t.add(COMM(encoding=3, lang="eng", desc="", text=watch_url))
    t.add(WOAS(url=watch_url))
    t.add(TSSE(encoding=3, text=encoder))
    t.add(TXXX(encoding=3, desc="HARMONIQ_SOURCE", text=source))
    if cover:
        t.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
    audio.save(v2_version=4)


def _write_vorbis(path, md, cover, *, artist, genre, watch_url, source, encoder):
    audio = OggOpus(path)
    audio.delete()
    t = audio.tags
    t["TITLE"] = md.title or ""
    if artist:
        t["ARTIST"] = artist
        t["ARTISTS"] = list(md.artists)
    simple = {
        "ALBUMARTIST": md.album_artist, "ALBUM": md.album, "DATE": md.date, "GENRE": genre, "LYRICS": md.lyrics,
        "TRACKNUMBER": md.track_number, "TRACKTOTAL": md.track_total if md.track_number else None,
        "DISCNUMBER": md.disc_number, "DISCTOTAL": md.disc_total if md.disc_number else None, "ISRC": md.isrc,
        "ITUNESADVISORY": None if md.explicit is None else ("1" if md.explicit else "0"),
    }
    for key, value in simple.items():
        if value not in (None, ""):
            t[key] = str(value)
    for key, value in md.musicbrainz.items():
        if key in _MB_KEYS:
            t[_MB_KEYS[key][1]] = value
    t["COMMENT"] = watch_url
    t["ENCODER"] = encoder
    t["HARMONIQ_SOURCE"] = source
    if cover:
        pic = Picture()
        pic.type, pic.mime, pic.desc, pic.data = 3, "image/jpeg", "Cover", cover
        t["METADATA_BLOCK_PICTURE"] = base64.b64encode(pic.write()).decode("ascii")
    audio.save()


def read_back(path: Path, fmt: str) -> dict:
    """Re-open the file and report what a reader will see (used by the verification step)."""
    if fmt == "m4a":
        t = MP4(path).tags or {}
        return {"title": (t.get("\xa9nam") or [None])[0], "artist": (t.get("\xa9ART") or [None])[0],
                "cover": bool(t.get("covr")), "lyrics": bool(t.get("\xa9lyr"))}
    if fmt == "mp3":
        t = MP3(path).tags
        return {"title": str(t.get("TIT2")) if t and t.get("TIT2") else None,
                "artist": str(t.get("TPE1")) if t and t.get("TPE1") else None,
                "cover": bool(t and t.getall("APIC")), "lyrics": bool(t and t.getall("USLT"))}
    t = OggOpus(path).tags
    return {"title": (t.get("TITLE") or [None])[0], "artist": (t.get("ARTIST") or [None])[0],
            "cover": bool(t.get("METADATA_BLOCK_PICTURE")), "lyrics": bool(t.get("LYRICS"))}
