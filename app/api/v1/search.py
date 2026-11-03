from __future__ import annotations

import asyncio
import hashlib
import logging
import unicodedata

from fastapi import APIRouter, Depends, Request, Response
from starlette.concurrency import run_in_threadpool

from app.api.deps import Client, count_upstream_request, enforce_limit, get_services, require_client
from app.domain.errors import AppError
from app.domain.models import SEARCH_TYPES, SearchResponse, SearchSections
from app.providers import mapping
from app.services import Services

router = APIRouter()
log = logging.getLogger("harmoniq.search")

DEFAULT_LIMIT = 20
MAX_LIMIT = 50
# `type=all` section name → single search type
SECTIONS = {"songs": "song", "videos": "video", "playlists": "playlist"}


async def _run(services: Services, fn, *args):
    try:
        return await asyncio.wait_for(run_in_threadpool(fn, *args), services.settings.search_timeout_seconds)
    except TimeoutError as exc:
        raise AppError("upstream_timeout", "Search took too long.") from exc


async def search_one(services: Services, type: str, query: str, limit: int) -> tuple[SearchResponse, bool]:
    """One single-type search, cached per (type, limit, query). Returns (response, was cached)."""
    cache_key = f"{type}:{limit}:{hashlib.sha1(query.lower().encode()).hexdigest()}"
    if (cached := services.search_cache.get(cache_key)) is not None:
        return cached.model_copy(update={"query": query, "cached": True}), True

    result: SearchResponse | None = None
    if type == "playlist":
        raw = await _run(services, services.music.search_playlists, query, limit)
        result = SearchResponse(query=query, type="playlist", source="youtube_music", results=[],
                                playlists=[mapping.playlist_result(r) for r in raw])
    elif type == "song":
        try:
            raw = await _run(services, services.music.search_songs, query, limit)
            result = SearchResponse(query=query, type="song", source="youtube_music",
                                    results=[mapping.music_result(r) for r in raw])
        except Exception as exc:  # fall back to plain YouTube search
            log.warning("YouTube Music search failed (%s); falling back to video search", exc)
    if result is None:
        raw = await _run(services, services.ytdlp.search, query, limit)
        results = [r for r in (mapping.video_result(e) for e in raw) if r is not None]
        result = SearchResponse(query=query, type=type, source="youtube", results=results)

    services.search_cache[cache_key] = result
    return result, False


async def search_all(services: Services, query: str, limit: int) -> tuple[SearchResponse, bool]:
    """Songs, videos and playlists fetched concurrently. Fails only when every section fails."""
    outcomes = await asyncio.gather(*(search_one(services, t, query, limit) for t in SECTIONS.values()),
                                    return_exceptions=True)
    sections = SearchSections()
    failures: list[BaseException] = []
    all_cached = True
    for name, outcome in zip(SECTIONS, outcomes):
        if isinstance(outcome, BaseException):
            if not isinstance(outcome, Exception):
                raise outcome  # cancellation etc.
            log.warning("search section %s failed: %s", name, outcome)
            failures.append(outcome)
            sections.errors[name] = outcome.code if isinstance(outcome, AppError) else "upstream_error"
            all_cached = False
            continue
        part, cached = outcome
        all_cached = all_cached and cached
        if name == "playlists":
            sections.playlists = part.playlists or []
        else:
            setattr(sections, name, part.results)
    if len(failures) == len(SECTIONS):
        first = next((f for f in failures if isinstance(f, AppError)), None)
        raise first or AppError("upstream_error", "Search failed.")
    response = SearchResponse(query=query, type="all", source="mixed", cached=all_cached, results=[],
                              sections=sections)
    return response, all_cached


@router.get("/search", response_model=SearchResponse,
            summary="Search songs (YouTube Music), videos (YouTube), playlists, or all three")
async def search(request: Request, response: Response, q: str = "", type: str = "song", limit: int | None = None,
                 client: Client = Depends(require_client)) -> SearchResponse:
    services = get_services(request)
    s = services.settings
    query = " ".join(unicodedata.normalize("NFC", q).split())
    if not 1 <= len(query) <= 200:
        raise AppError("invalid_query", "q must be 1-200 characters.")
    if type not in SEARCH_TYPES:
        raise AppError("invalid_query", "type must be one of: " + ", ".join(SEARCH_TYPES) + ".")
    if limit is None:
        limit = s.search_all_section_limit if type == "all" else DEFAULT_LIMIT
    if not 1 <= limit <= MAX_LIMIT:
        raise AppError("invalid_query", f"limit must be between 1 and {MAX_LIMIT}.")
    enforce_limit(request, response, client, "search", s.rate_limit_search_per_min, 60)
    # "all" is one search for the device but three upstream searches
    for _ in range(len(SECTIONS) if type == "all" else 1):
        count_upstream_request(request)

    if type == "all":
        result, cached = await search_all(services, query, limit)
    else:
        result, cached = await search_one(services, type, query, limit)
    response.headers["X-Cache"] = "HIT" if cached else "MISS"
    return result
