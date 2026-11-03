from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request

from app import __version__
from app.api.deps import Client, get_services, require_client
from app.config import FORMATS
from app.domain.errors import AppError

router = APIRouter()

API_VERSION = "1.0.0"


@router.get("/healthz", include_in_schema=False)
async def healthz():
    return {"status": "ok"}


def readiness_checks(request: Request) -> dict[str, bool]:
    services = get_services(request)
    s = services.settings
    writable = True
    probe = services.store.tmp_dir / f".ready-{uuid.uuid4().hex}"
    try:
        probe.write_bytes(b"ok")
        os.remove(probe)
    except OSError:
        writable = False
    return {
        "data_writable": writable,
        "disk_space": services.store.free_bytes() >= s.disk_min_free_bytes,
        "ffmpeg": s.tool("ffmpeg") is not None,
        "ffprobe": s.tool("ffprobe") is not None,
        "js_runtime": s.tool(s.ytdlp_js_runtime) is not None,
    }


@router.get("/readyz", include_in_schema=False)
async def readyz(request: Request):
    checks = readiness_checks(request)
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        raise AppError("service_unavailable", "Not ready: " + ", ".join(failed))
    return {"status": "ready", "checks": checks}


@router.get("/v1/meta", summary="Server capabilities and versions")
async def meta(request: Request, client: Client = Depends(require_client)):
    services = get_services(request)
    s = services.settings
    canary = services.health.canary
    return {
        "api_version": API_VERSION,
        "server_version": __version__,
        "formats": list(FORMATS),
        "default_format": s.default_format,
        "max_duration_seconds": s.max_duration_seconds,
        "limits": {
            "search_per_minute": s.rate_limit_search_per_min,
            "downloads_per_hour": s.rate_limit_downloads_per_hour,
            "max_active_jobs": s.max_active_jobs_per_device,
            "max_concurrent_downloads": s.max_concurrent_downloads,
        },
        "components": services.versions,
        "features": {
            "metadata_providers": services.resolver.names,
            "lyrics_providers": s.lyrics_provider_list if "lyrics" in services.resolver.names else [],
            "musicbrainz": "musicbrainz" in services.resolver.names,
            "lastfm": "lastfm" in services.resolver.names,
            "filename_template": s.filename_template,
        },
        "queue": {"waiting": services.jobs.depth, "active": len(services.registry.active_jobs())},
        "status": {
            "youtube": services.health.status,
            "last_canary_at": datetime.fromtimestamp(canary["at"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if canary else None,
            "last_canary_ok": canary.get("ok") if canary else None,
        },
    }
