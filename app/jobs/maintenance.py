"""Background housekeeping: the sweeper (every 10 min) and the optional hourly canary download."""

from __future__ import annotations

import asyncio
import logging
import time

from app.domain.errors import AppError
from app.jobs.pipeline import DownloadPipeline
from app.jobs.registry import Job, JobRegistry
from app.providers.ytdlp_client import YouTubeHealth, yt_dlp_version
from app.storage.local import LocalFileStore

log = logging.getLogger("harmoniq.maintenance")

SWEEP_INTERVAL = 600
CANARY_INTERVAL = 3600
TMP_MAX_AGE = 3600


def sweep_once(store: LocalFileStore, registry: JobRegistry, *, file_ttl: int, max_bytes: int, job_ttl: int) -> None:
    active = registry.active_jobs()
    store.sweep(file_ttl, max_bytes, keep={j.dedupe_key for j in active})
    store.clean_tmp(TMP_MAX_AGE, keep={j.id for j in active})
    for job in list(registry.jobs.values()):
        if job.status == "completed" and job.file_key and store.get(job.file_key) is None:
            job.set_status("expired")
    registry.cleanup(job_ttl)


async def sweeper_loop(store, registry, settings) -> None:
    while True:
        try:
            await asyncio.to_thread(sweep_once, store, registry, file_ttl=settings.file_ttl_seconds,
                                    max_bytes=settings.storage_max_bytes, job_ttl=settings.job_ttl_seconds)
        except Exception:
            log.exception("Sweeper failed")
        await asyncio.sleep(SWEEP_INTERVAL)


async def run_canary(pipeline: DownloadPipeline, health: YouTubeHealth, video_id: str, timeout: int) -> dict:
    job = Job(video_id=video_id, format="m4a", quality="best", dedupe_key="canary", embed_artwork=False)
    job.id = "canary"
    started = time.time()
    result = {"at": started, "video_id": video_id, "yt_dlp": yt_dlp_version()}
    try:
        await asyncio.wait_for(asyncio.to_thread(pipeline.run, job, publish=False), timeout)
        result |= {"ok": True}
    except AppError as err:
        result |= {"ok": False, "error": err.code, "detail": err.detail}
    except TimeoutError:
        job.cancel.set()
        result |= {"ok": False, "error": "upstream_timeout"}
    except Exception as exc:
        result |= {"ok": False, "error": "internal_error", "detail": repr(exc)}
    result["seconds"] = round(time.time() - started, 1)
    health.canary = result
    (log.info if result["ok"] else log.warning)("Canary %s", result)
    return result


async def canary_loop(pipeline, health, settings) -> None:
    await asyncio.sleep(60)  # let startup settle
    while True:
        await run_canary(pipeline, health, settings.canary_video_id, settings.job_timeout_seconds)
        await asyncio.sleep(CANARY_INTERVAL)
