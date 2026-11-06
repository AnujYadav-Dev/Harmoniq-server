"""In-process job runner: an asyncio queue drained by MAX_CONCURRENT_DOWNLOADS workers."""

from __future__ import annotations

import asyncio
import logging
import time

from app.alerts import BlockAlerter
from app.config import Settings
from app.domain.errors import AppError, JobCancelled
from app.jobs.pipeline import DownloadPipeline
from app.jobs.registry import Job, JobRegistry

log = logging.getLogger("harmoniq.jobs")

RETRYABLE = {"upstream_error", "processing_failed"}
BACKOFF_SECONDS = (10, 30)


class JobManager:
    def __init__(self, settings: Settings, registry: JobRegistry, pipeline: DownloadPipeline,
                 alerter: BlockAlerter | None = None) -> None:
        self.settings = settings
        self.registry = registry
        self.pipeline = pipeline
        self.alerter = alerter
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []

    async def start(self) -> None:
        self._workers = [asyncio.create_task(self._worker(i), name=f"download-worker-{i}")
                         for i in range(max(1, self.settings.max_concurrent_downloads))]

    async def stop(self) -> None:
        for job in self.registry.active_jobs():
            job.cancel_reason = "shutdown"
            job.cancel.set()
        for task in self._workers:
            task.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)

    @property
    def depth(self) -> int:
        return self.queue.qsize()

    def submit(self, job: Job) -> None:
        if self.queue.qsize() >= self.settings.queue_max_depth:
            raise AppError("queue_full", "Too many downloads are waiting. Try again shortly.", retry_after=30)
        self.registry.add(job)
        self.queue.put_nowait(job.id)

    def cancel(self, job: Job) -> None:
        job.cancel_reason = "cancelled"
        job.cancel.set()
        if job.status == "queued":  # never started: finish it right away
            job.set_status("cancelled")
            self.registry.release_inflight(job)

    async def _worker(self, index: int) -> None:
        while True:
            job_id = await self.queue.get()
            try:
                job = self.registry.get(job_id)
                if job is not None and job.status == "queued" and not job.cancel.is_set():
                    await self._run(job)
            except Exception:
                log.exception("Worker %d crashed while handling %s", index, job_id)
            finally:
                self.queue.task_done()

    async def _run(self, job: Job) -> None:
        deadline = time.monotonic() + self.settings.job_timeout_seconds
        try:
            while True:
                job.attempts += 1
                task = asyncio.ensure_future(asyncio.to_thread(self.pipeline.run, job))
                done, _ = await asyncio.wait({task}, timeout=max(0.0, deadline - time.monotonic()))
                if not done:
                    job.cancel_reason = "timeout"
                    job.cancel.set()
                    await asyncio.wait({task}, timeout=30)  # the thread notices the flag at its next checkpoint
                    job.fail(AppError("upstream_timeout",
                                      f"The download took longer than {self.settings.job_timeout_seconds} s.").as_job_error())
                    return
                try:
                    key, meta = task.result()
                except JobCancelled:
                    if job.cancel_reason == "timeout":
                        job.fail(AppError("upstream_timeout").as_job_error())
                    else:
                        job.set_status("cancelled")
                    return
                except AppError as err:
                    retry_index = job.attempts - 1
                    if err.code in RETRYABLE and retry_index < len(BACKOFF_SECONDS) and not job.cancel.is_set():
                        wait = BACKOFF_SECONDS[retry_index]
                        log.warning("Job %s attempt %d failed (%s); retrying in %d s", job.id, job.attempts, err, wait)
                        job.progress = 0.0
                        job.set_status("queued")
                        if await self._sleep_unless_cancelled(job, min(wait, max(0.0, deadline - time.monotonic()))):
                            if job.status != "cancelled":
                                job.set_status("cancelled")
                            return
                        continue
                    log.info("Job %s failed: %s", job.id, err)
                    job.fail(err.as_job_error())
                    if err.code == "upstream_blocked" and self.alerter is not None:
                        self.alerter.record_blocked(str(err))
                    return
                except Exception as exc:
                    log.exception("Job %s crashed", job.id)
                    job.fail(AppError("internal_error", f"Unexpected error: {exc.__class__.__name__}").as_job_error())
                    return
                job.complete(key, meta)
                return
        finally:
            self.registry.release_inflight(job)

    @staticmethod
    async def _sleep_unless_cancelled(job: Job, seconds: float) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if job.cancel.is_set():
                return True
            await asyncio.sleep(min(0.5, end - time.monotonic()))
        return job.cancel.is_set()
