from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from app.api.deps import Client, count_upstream_request, get_services, require_client
from app.domain.errors import AppError
from app.domain.models import VIDEO_ID_RE, LyricsInfo, PreviewInfo, TrackInfo, VideoMetadata
from app.media.tags import display_filename
from app.metadata.model import MetadataContext
from app.providers import mapping
from app.services import Services

router = APIRouter()


def _check_id(video_id: str) -> None:
    if not VIDEO_ID_RE.match(video_id):
        raise AppError("invalid_video_id", "A video ID is 11 characters of A-Z, a-z, 0-9, _ or -.")


async def load_info(request: Request, video_id: str) -> dict:
    """Raw yt-dlp info (cached), shared by /videos and /track."""
    services: Services = get_services(request)
    if (cached := services.info_cache.get(video_id)) is not None:
        return cached
    count_upstream_request(request)
    try:
        info = await asyncio.wait_for(run_in_threadpool(services.ytdlp.extract, video_id),
                                      services.settings.metadata_timeout_seconds)
    except TimeoutError as exc:
        raise AppError("upstream_timeout", "Fetching video details took too long.") from exc
    services.info_cache[video_id] = info
    return info


async def load_metadata(request: Request, video_id: str) -> VideoMetadata:
    services: Services = get_services(request)
    _check_id(video_id)
    if (cached := services.metadata_cache.get(video_id)) is not None:
        return cached
    meta = mapping.video_metadata(await load_info(request, video_id), services.settings)
    services.metadata_cache[video_id] = meta
    return meta


@router.get("/videos/{video_id}", response_model=VideoMetadata, summary="Full metadata for one video")
async def get_video(video_id: str, request: Request, client: Client = Depends(require_client)) -> VideoMetadata:
    return await load_metadata(request, video_id)


@router.get("/videos/{video_id}/preview", response_model=PreviewInfo, summary="Embed/preview information")
async def get_preview(video_id: str, request: Request, client: Client = Depends(require_client)) -> PreviewInfo:
    return mapping.preview_info(await load_metadata(request, video_id))


@router.get("/videos/{video_id}/track", response_model=TrackInfo,
            summary="Resolved song metadata (album, track number, date, genres…) before downloading")
async def get_track(video_id: str, request: Request, enrich: bool = True,
                    client: Client = Depends(require_client)) -> TrackInfo:
    services: Services = get_services(request)
    _check_id(video_id)
    cache_key = f"{video_id}:{enrich}"
    if (cached := services.track_cache.get(cache_key)) is not None:
        return cached
    info = await load_info(request, video_id)
    ctx = MetadataContext(video_id=video_id, fetch_lyrics=False, enrich=enrich)
    ctx.set_info(info)
    try:
        md = await asyncio.wait_for(run_in_threadpool(services.resolver.resolve, ctx),
                                    services.settings.metadata_timeout_seconds)
    except TimeoutError as exc:
        ctx.cancel.set()
        raise AppError("upstream_timeout", "Looking up the song took too long.") from exc
    md.title = md.title or info.get("title")
    has_lyrics = bool(md.extras.get("lyrics_id"))
    template = services.settings.filename_template
    track = TrackInfo(
        video_id=video_id, title=md.title, artists=md.artists, album_artist=md.album_artist, album=md.album,
        track_number=md.track_number, track_total=md.track_total, disc_number=md.disc_number,
        disc_total=md.disc_total, date=md.date, year=md.year, genres=md.genres, isrc=md.isrc, explicit=md.explicit,
        duration=md.duration, artwork_url=md.artwork_url,
        lyrics=LyricsInfo(available=has_lyrics, source="YouTube Music" if has_lyrics else None),
        musicbrainz=md.musicbrainz, sources=md.sources, video_type=md.extras.get("video_type"),
        suggested_filename={fmt: display_filename(md, fmt, template, video_id) for fmt in ("m4a", "opus", "mp3")},
    )
    services.track_cache[cache_key] = track
    return track
