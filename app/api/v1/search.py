from __future__ import annotations

import asyncio
import hashlib
import logging
import unicodedata

from fastapi import APIRouter, Depends, Request, Response
from starlette.concurrency import run_in_threadpool

from app.api.deps import Client, count_upstream_request, enforce_limit, get_services, require_client
from app.domain.errors import AppError
from app.domain.models import SearchResponse
from app.providers import mapping

router = APIRouter()
log = logging.getLogger("harmoniq.search")


@router.get("/search", response_model=SearchResponse, summary="Search songs (YouTube Music) or videos (YouTube)")
async def search(request: Request, response: Response, q: str = "", type: str = "song", limit: int = 20,
                 client: Client = Depends(require_client)) -> SearchResponse:
    services = get_services(request)
    s = services.settings
    query = " ".join(unicodedata.normalize("NFC", q).split())
    if not 1 <= len(query) <= 200:
        raise AppError("invalid_query", "q must be 1-200 characters.")
    if type not in ("song", "video"):
        raise AppError("invalid_query", "type must be 'song' or 'video'.")
    if not 1 <= limit <= 50:
        raise AppError("invalid_query", "limit must be between 1 and 50.")
    enforce_limit(request, response, client, "search", s.rate_limit_search_per_min, 60)
    count_upstream_request(request)

    cache_key = f"{type}:{limit}:{hashlib.sha1(query.lower().encode()).hexdigest()}"
    if (cached := services.search_cache.get(cache_key)) is not None:
        response.headers["X-Cache"] = "HIT"
        return cached.model_copy(update={"query": query, "cached": True})

    async def run(fn, *args):
        try:
            return await asyncio.wait_for(run_in_threadpool(fn, *args), s.search_timeout_seconds)
        except TimeoutError as exc:
            raise AppError("upstream_timeout", "Search took too long.") from exc

    result: SearchResponse | None = None
    if type == "song":
        try:
            raw = await run(services.music.search_songs, query, limit)
            result = SearchResponse(query=query, type="song", source="youtube_music",
                                    results=[mapping.music_result(r) for r in raw])
        except Exception as exc:  # fall back to plain YouTube search
            log.warning("YouTube Music search failed (%s); falling back to video search", exc)
    if result is None:
        raw = await run(services.ytdlp.search, query, limit)
        results = [r for r in (mapping.video_result(e) for e in raw) if r is not None]
        result = SearchResponse(query=query, type=type, source="youtube", results=results)

    services.search_cache[cache_key] = result
    response.headers["X-Cache"] = "MISS"
    return result
