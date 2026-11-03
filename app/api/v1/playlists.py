from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from app.api.deps import Client, count_upstream_request, get_services, require_client
from app.domain.errors import AppError
from app.domain.models import PLAYLIST_ID_RE, PlaylistDetail
from app.providers import mapping
from app.services import Services

router = APIRouter()
log = logging.getLogger("harmoniq.playlists")


def normalize_playlist_id(playlist_id: str) -> str:
    """`VLPL…` (browse id) or `PL…` → `PL…`; raises invalid_playlist_id."""
    match = PLAYLIST_ID_RE.match(playlist_id)
    if not match:
        raise AppError("invalid_playlist_id",
                       "A playlist ID is 10-64 characters of A-Z, a-z, 0-9, _ or -, optionally prefixed with VL.")
    return match.group(2)


@router.get("/playlists/{playlist_id}", response_model=PlaylistDetail,
            summary="Playlist info and its playable tracks (same shape as search results)")
async def get_playlist(playlist_id: str, request: Request, limit: int | None = None,
                       client: Client = Depends(require_client)) -> PlaylistDetail:
    services: Services = get_services(request)
    s = services.settings
    playlist_id = normalize_playlist_id(playlist_id)
    max_tracks = s.max_playlist_tracks if limit is None else limit
    if not 1 <= max_tracks <= s.max_playlist_tracks:
        raise AppError("invalid_query", f"limit must be between 1 and {s.max_playlist_tracks}.")

    cache_key = f"{playlist_id}:{max_tracks}"
    if (cached := services.playlist_cache.get(cache_key)) is not None:
        return cached
    count_upstream_request(request)
    try:
        raw = await asyncio.wait_for(run_in_threadpool(services.music.playlist, playlist_id, max_tracks),
                                     s.metadata_timeout_seconds)
    except TimeoutError as exc:
        raise AppError("upstream_timeout", "Loading the playlist took too long.") from exc
    except KeyError as exc:  # ytmusicapi can't find the page contents: private, deleted or not a real playlist
        raise AppError("playlist_not_found", "This playlist is private, deleted or can't be opened.") from exc
    except AppError:
        raise
    except Exception as exc:
        log.warning("playlist %s failed: %s", playlist_id, exc)
        raise AppError("upstream_error", "YouTube Music didn't return the playlist.") from exc
    detail = mapping.playlist_detail(raw, playlist_id, max_tracks)
    services.playlist_cache[cache_key] = detail
    return detail
