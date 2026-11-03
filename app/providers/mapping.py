"""Turns raw ytmusicapi / yt-dlp dicts into the public API models."""

from __future__ import annotations

from app.config import Settings
from app.domain.models import (
    Album, Artist, AudioFormat, Channel, OutputOption, PlaylistDetail, PlaylistResult, PreviewInfo, SearchResult,
    Thumbnail, VideoFlags, VideoMetadata,
)

MP3_BEST_KBPS = 245  # libmp3lame -q:a 0 average


def duration_text(seconds: int | float | None) -> str | None:
    if seconds is None:
        return None
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _thumbs(raw: list[dict] | None) -> list[Thumbnail]:
    out = []
    for t in raw or []:
        if t.get("url"):
            out.append(Thumbnail(url=t["url"], width=t.get("width"), height=t.get("height")))
    out.sort(key=lambda t: (t.width or 0) * (t.height or 0))
    return out


def music_result(r: dict) -> SearchResult:
    artists = [Artist(name=a["name"], channel_id=a.get("id")) for a in r.get("artists") or [] if a.get("name")]
    album = r.get("album")
    duration = r.get("duration_seconds")
    return SearchResult(
        video_id=r["videoId"],
        result_type="song",
        title=r.get("title") or "",
        artists=artists or None,
        album=Album(name=album["name"], browse_id=album.get("id")) if album and album.get("name") else None,
        duration=duration,
        duration_text=r.get("duration") or duration_text(duration),
        is_explicit=r.get("isExplicit"),
        thumbnails=_thumbs(r.get("thumbnails")),
        watch_url=f"https://music.youtube.com/watch?v={r['videoId']}",
    )


def _int(value) -> int | None:
    try:
        return int(str(value).replace(",", "")) if value is not None else None
    except ValueError:
        return None


def playlist_watch_url(playlist_id: str) -> str:
    return f"https://music.youtube.com/playlist?list={playlist_id}"


def strip_browse_prefix(browse_id: str) -> str:
    return browse_id[2:] if browse_id.startswith("VL") else browse_id


def playlist_result(r: dict) -> PlaylistResult:
    playlist_id = strip_browse_prefix(r["browseId"])
    author = r.get("author")
    if isinstance(author, list):  # some ytmusicapi versions return [{"name": ...}]
        author = ", ".join(a.get("name", "") for a in author if a.get("name")) or None
    return PlaylistResult(
        playlist_id=playlist_id,
        title=r.get("title") or "",
        author=author or None,
        item_count=_int(r.get("itemCount")),
        thumbnails=_thumbs(r.get("thumbnails")),
        watch_url=playlist_watch_url(playlist_id),
    )


def playlist_track(t: dict) -> SearchResult | None:
    """A playlist entry in the search-result shape; None for unavailable or id-less entries."""
    video_id = t.get("videoId")
    if not video_id or len(video_id) != 11 or t.get("isAvailable") is False:
        return None
    is_song = t.get("videoType") == "MUSIC_VIDEO_TYPE_ATV"
    artists = [Artist(name=a["name"], channel_id=a.get("id")) for a in t.get("artists") or [] if a.get("name")]
    album = t.get("album")
    duration = t.get("duration_seconds")
    return SearchResult(
        video_id=video_id,
        result_type="song" if is_song else "video",
        title=t.get("title") or "",
        artists=artists or None,
        album=Album(name=album["name"], browse_id=album.get("id")) if album and album.get("name") else None,
        duration=duration,
        duration_text=t.get("duration") or duration_text(duration),
        is_explicit=t.get("isExplicit"),
        thumbnails=_thumbs(t.get("thumbnails")),
        watch_url=(f"https://music.youtube.com/watch?v={video_id}" if is_song
                   else f"https://www.youtube.com/watch?v={video_id}"),
    )


def playlist_detail(p: dict, playlist_id: str, max_tracks: int) -> PlaylistDetail:
    raw = p.get("tracks") or []
    tracks = [r for r in (playlist_track(t) for t in raw[:max_tracks]) if r is not None]
    track_count = _int(p.get("trackCount"))
    author = p.get("author")
    if isinstance(author, dict):
        author = author.get("name")
    elif isinstance(author, list):
        author = ", ".join(a.get("name", "") for a in author if a.get("name")) or None
    return PlaylistDetail(
        playlist_id=playlist_id,
        title=p.get("title") or "",
        author=author or None,
        description=p.get("description") or None,
        track_count=track_count,
        duration=_int(p.get("duration_seconds")),
        thumbnails=_thumbs(p.get("thumbnails")),
        watch_url=playlist_watch_url(playlist_id),
        tracks=tracks,
        truncated=len(raw) > max_tracks or (track_count is not None and track_count > max_tracks),
    )


def video_result(e: dict) -> SearchResult | None:
    video_id = e.get("id")
    if not video_id or len(video_id) != 11 or e.get("live_status") in ("is_live", "is_upcoming"):
        return None
    if e.get("ie_key") not in (None, "Youtube"):
        return None  # channels / playlists in search results
    duration = int(e["duration"]) if e.get("duration") else None
    return SearchResult(
        video_id=video_id,
        result_type="video",
        title=e.get("title") or "",
        duration=duration,
        duration_text=duration_text(duration),
        thumbnails=_thumbs(e.get("thumbnails")),
        channel=Channel(name=e.get("channel") or e.get("uploader"), id=e.get("channel_id"), url=e.get("channel_url")),
        view_count=e.get("view_count"),
        watch_url=f"https://www.youtube.com/watch?v={video_id}",
    )


def video_flags(info: dict, settings: Settings) -> VideoFlags:
    is_live = info.get("live_status") in ("is_live", "is_upcoming", "post_live") or bool(info.get("is_live"))
    duration = info.get("duration")
    reason = None
    if is_live:
        reason = "live_not_supported"
    elif duration and duration > settings.max_duration_seconds:
        reason = "duration_limit_exceeded"
    elif not any(f.get("acodec") not in (None, "none") for f in info.get("formats") or []):
        reason = "video_unavailable"
    return VideoFlags(
        embeddable=info.get("playable_in_embed", True) is not False,
        age_restricted=(info.get("age_limit") or 0) >= 18,
        is_live=is_live,
        downloadable=reason is None,
        reason=reason,
    )


def _audio_formats(info: dict) -> list[AudioFormat]:
    out = []
    for f in info.get("formats") or []:
        if f.get("vcodec") not in (None, "none") or f.get("acodec") in (None, "none"):
            continue
        if "drc" in str(f.get("format_id", "")).lower():
            continue  # dynamic-range-compressed duplicates
        out.append(AudioFormat(
            format_id=str(f.get("format_id")),
            codec=(f.get("acodec") or "").split(".")[0] or None,
            container=f.get("ext"),
            abr_kbps=round(f["abr"]) if f.get("abr") else None,
            sample_rate=f.get("asr"),
            filesize_approx=f.get("filesize") or f.get("filesize_approx"),
        ))
    out.sort(key=lambda a: a.abr_kbps or 0, reverse=True)
    return out


def output_options(formats: list[AudioFormat], duration: int | None, default_format: str) -> list[OutputOption]:
    aac = next((f for f in formats if f.codec == "mp4a"), None)
    opus = next((f for f in formats if f.codec == "opus"), None)
    mp3_est = int(duration * MP3_BEST_KBPS * 1000 / 8) if duration else None
    return [
        OutputOption(format="m4a", transcode=aac is None, est_size_bytes=aac.filesize_approx if aac else None,
                     default=default_format == "m4a"),
        OutputOption(format="opus", transcode=opus is None, est_size_bytes=opus.filesize_approx if opus else None,
                     default=default_format == "opus", min_android_api=29),
        OutputOption(format="mp3", transcode=True, est_size_bytes=mp3_est, default=default_format == "mp3"),
    ]


def video_metadata(info: dict, settings: Settings) -> VideoMetadata:
    formats = _audio_formats(info)
    duration = int(info["duration"]) if info.get("duration") else None
    upload = info.get("upload_date")
    artists = info.get("artists") or ([info["artist"]] if info.get("artist") else [])
    return VideoMetadata(
        video_id=info["id"],
        title=info.get("title") or "",
        track=info.get("track"),
        artist=", ".join(artists) if artists else None,
        album=info.get("album"),
        release_year=info.get("release_year"),
        channel=Channel(name=info.get("channel") or info.get("uploader"), id=info.get("channel_id"),
                        url=info.get("channel_url")),
        duration=duration,
        upload_date=f"{upload[:4]}-{upload[4:6]}-{upload[6:8]}" if upload and len(upload) == 8 else None,
        description=info.get("description"),
        view_count=info.get("view_count"),
        like_count=info.get("like_count"),
        categories=info.get("categories") or [],
        tags=info.get("tags") or [],
        chapters=info.get("chapters") or [],
        thumbnails=_thumbs(info.get("thumbnails")),
        flags=video_flags(info, settings),
        audio_formats=formats,
        output_options=output_options(formats, duration, settings.default_format),
    )


def preview_info(meta: VideoMetadata) -> PreviewInfo:
    return PreviewInfo(
        video_id=meta.video_id,
        embeddable=meta.flags.embeddable,
        age_restricted=meta.flags.age_restricted,
        is_live=meta.flags.is_live,
        embed_url=f"https://www.youtube-nocookie.com/embed/{meta.video_id}?playsinline=1&rel=0",
        watch_url=f"https://www.youtube.com/watch?v={meta.video_id}",
        thumbnail=f"https://i.ytimg.com/vi/{meta.video_id}/hqdefault.jpg",
        title=meta.title,
        duration=meta.duration,
    )
