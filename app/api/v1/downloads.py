from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse

from app.api.deps import Client, count_upstream_request, enforce_limit, get_services, require_client
from app.config import FORMATS
from app.domain.errors import AppError
from app.domain.models import VIDEO_ID_RE, DownloadRequest
from app.jobs.pipeline import dedupe_key, normalize_quality, overrides_json
from app.jobs.registry import Job
from app.media.artwork import is_allowed_url
from app.services import Services

router = APIRouter()


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None


def _links(job: Job) -> dict:
    base = f"/v1/downloads/{job.id}"
    return {"self": base, "events": f"{base}/events", "file": f"{base}/file", "artwork": f"{base}/artwork",
            "lyrics": f"{base}/lyrics"}


def job_view(job: Job, services: Services) -> dict:
    if job.status == "completed" and job.file_key and services.store.get(job.file_key) is None:
        job.set_status("expired")
    view: dict = {
        "job_id": job.id,
        "status": job.status,
        "progress": round(job.progress, 4),
        "video_id": job.video_id,
        "format": job.format,
        "created_at": _iso(job.created_at),
        "updated_at": _iso(job.updated_at),
    }
    if not job.terminal and job.stage:
        view["stage"] = job.stage
    if job.status == "completed" and job.file_meta:
        stored = services.store.get(job.file_key)
        meta = stored.meta if stored else job.file_meta
        last_access = meta.get("last_access") or job.updated_at
        view["cache_hit"] = job.cache_hit
        view["file"] = {
            k: meta.get(k) for k in ("filename", "mime_type", "size_bytes", "sha256", "duration", "codec",
                                     "bitrate_kbps", "sample_rate", "channels", "transcoded", "quality")
        } | {"expires_at": _iso(last_access + services.settings.file_ttl_seconds)}
        view["metadata"] = meta.get("metadata")
        links = _links(job)
        view["links"] = {"file": links["file"], "artwork": links["artwork"], "lyrics": links["lyrics"]}
    if job.status == "failed":
        view["error"] = job.error
        view["attempts"] = job.attempts
    return view


def _owned_job(services: Services, job_id: str, client: Client) -> Job:
    job = services.registry.get(job_id)
    if job is None:
        raise AppError("job_not_found", "Unknown job. It may have expired or the server restarted; submit it again.")
    if client.device not in job.owners:
        raise AppError("forbidden", "This job belongs to another device.")
    return job


@router.post("/downloads", status_code=202, summary="Start an audio download job",
             responses={202: {"description": "Job accepted"}})
async def create_download(body: DownloadRequest, request: Request, response: Response,
                          client: Client = Depends(require_client)):
    services = get_services(request)
    s = services.settings
    if not VIDEO_ID_RE.match(body.video_id):
        raise AppError("invalid_video_id", "A video ID is 11 characters of A-Z, a-z, 0-9, _ or -.")
    fmt = body.format or s.default_format
    if fmt not in FORMATS:
        raise AppError("invalid_format", f"format must be one of {', '.join(FORMATS)}.")
    if body.quality not in ("best", "standard"):
        raise AppError("invalid_format", "quality must be 'best' or 'standard'.")
    if body.metadata and body.metadata.artwork_url and not is_allowed_url(body.metadata.artwork_url):
        raise AppError("invalid_request", "artwork_url must be an https URL on googleusercontent.com or ytimg.com.")

    idem_key = request.headers.get("idempotency-key", "")[:100]
    if idem_key and (existing := services.registry.idempotent_job(client.device, idem_key)):
        return _accepted(existing)

    if services.registry.active_count(client.device) >= s.max_active_jobs_per_device:
        raise AppError("too_many_active_jobs", f"At most {s.max_active_jobs_per_device} downloads can run at once.",
                       retry_after=10)
    enforce_limit(request, response, client, "downloads", s.rate_limit_downloads_per_hour, 3600)

    quality = normalize_quality(fmt, body.quality)
    key = dedupe_key(body.video_id, fmt, quality, overrides_json(body.metadata), body.embed_artwork,
                     f"lyrics={body.embed_lyrics};enrich={body.enrich}")
    new_job = Job(video_id=body.video_id, format=fmt, quality=quality, dedupe_key=key,
                  overrides=body.metadata, embed_artwork=body.embed_artwork, embed_lyrics=body.embed_lyrics,
                  enrich=body.enrich, owners={client.device})

    if (stored := services.store.get(key)) is not None:
        new_job.complete(key, stored.meta, cache_hit=True)
        services.registry.add(new_job)
        services.store.touch(key)
        job = new_job
    elif (running := services.registry.find_inflight(key)) is not None:
        running.owners.add(client.device)
        job = running
    else:
        count_upstream_request(request)
        services.jobs.submit(new_job)
        job = new_job

    if idem_key:
        services.registry.remember_idempotency(client.device, idem_key, job.id)
    return _accepted(job, headers=dict(response.headers))


def _accepted(job: Job, headers: dict | None = None) -> JSONResponse:
    body = {"job_id": job.id, "status": job.status, "cache_hit": job.cache_hit,
            "created_at": _iso(job.created_at), "links": _links(job)}
    headers = {k: v for k, v in (headers or {}).items() if k.lower().startswith("ratelimit")}
    return JSONResponse(body, status_code=202, headers={"Location": _links(job)["self"], **headers})


@router.get("/downloads/{job_id}", summary="Job status, progress and metadata")
async def get_download(job_id: str, request: Request, client: Client = Depends(require_client)):
    services = get_services(request)
    return job_view(_owned_job(services, job_id, client), services)


@router.get("/downloads/{job_id}/events", summary="Progress as Server-Sent Events")
async def download_events(job_id: str, request: Request, client: Client = Depends(require_client)):
    services = get_services(request)
    job = _owned_job(services, job_id, client)

    async def stream():
        last = None
        idle = 0.0
        while True:
            if await request.is_disconnected():
                return
            view = job_view(job, services)
            data = json.dumps(view, separators=(",", ":"))
            if job.terminal:
                yield f"event: {job.status}\ndata: {data}\n\n"
                return
            if data != last:
                last, idle = data, 0.0
                yield f"event: progress\ndata: {data}\n\n"
            elif idle >= 15:
                idle = 0.0
                yield ": keep-alive\n\n"
            await asyncio.sleep(0.25)
            idle += 0.25

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _completed_file(services: Services, job: Job):
    job_view(job, services)  # refreshes "expired"
    if job.status == "expired":
        raise AppError("job_expired", "The file was removed from the server cache. Submit the download again.")
    if job.status != "completed":
        raise AppError("job_not_ready", f"The job is {job.status}.")
    stored = services.store.get(job.file_key)
    if stored is None:
        raise AppError("job_expired", "The file was removed from the server cache. Submit the download again.")
    return stored


@router.get("/downloads/{job_id}/file", summary="Download the finished audio (Range supported)",
            response_class=FileResponse)
async def download_file(job_id: str, request: Request, client: Client = Depends(require_client)):
    services = get_services(request)
    stored = _completed_file(services, _owned_job(services, job_id, client))
    services.store.touch(stored.key)
    sha = stored.meta.get("sha256", "")
    return FileResponse(stored.audio, media_type=stored.meta.get("mime_type"),
                        filename=stored.meta.get("filename"),
                        headers={"ETag": f'"{sha}"', "X-Content-SHA256": sha, "Cache-Control": "private, no-store"})


@router.get("/downloads/{job_id}/artwork", summary="Square cover art (JPEG)", response_class=FileResponse)
async def download_artwork(job_id: str, request: Request, client: Client = Depends(require_client)):
    services = get_services(request)
    stored = _completed_file(services, _owned_job(services, job_id, client))
    if stored.artwork is None:
        raise AppError("artwork_not_available", "No cover art was found for this video.")
    return FileResponse(stored.artwork, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@router.get("/downloads/{job_id}/lyrics", summary="Lyrics as LRC (synced) or plain text",
            response_class=PlainTextResponse)
async def download_lyrics(job_id: str, request: Request, client: Client = Depends(require_client)):
    services = get_services(request)
    stored = _completed_file(services, _owned_job(services, job_id, client))
    text = stored.meta.get("lyrics_text")
    if not text:
        raise AppError("lyrics_not_available", "No lyrics were found for this song.")
    synced = bool((stored.meta.get("metadata") or {}).get("lyrics", {}).get("synced"))
    name = stored.meta.get("filename", "lyrics").rsplit(".", 1)[0] + (".lrc" if synced else ".txt")
    return PlainTextResponse(text, headers={"X-Lyrics-Synced": "true" if synced else "false",
                                            "Content-Disposition": f"inline; filename*=UTF-8''{quote(name)}"})


@router.delete("/downloads/{job_id}", summary="Cancel a job or release its file")
async def delete_download(job_id: str, request: Request, client: Client = Depends(require_client)):
    services = get_services(request)
    job = _owned_job(services, job_id, client)
    if not job.terminal:
        if len(job.owners) > 1:
            job.owners.discard(client.device)  # someone else still wants this download
        else:
            services.jobs.cancel(job)
        return {"job_id": job.id, "status": "cancelled"}

    job.owners.discard(client.device)
    if not job.owners:
        services.registry.remove(job.id)
        if job.status == "completed" and job.file_key and services.registry.references(job.file_key) == 0:
            services.store.delete(job.file_key)
    return Response(status_code=204)
