"""In-memory job records. Lost on restart by design (docs/architecture.md §3)."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field

from app.domain.models import TERMINAL_STATUSES, MetadataOverrides

# Overall progress range covered by each stage.
STAGE_RANGES = {
    "queued": (0.0, 0.0),
    "resolving": (0.0, 0.10),
    "downloading": (0.10, 0.75),
    "processing": (0.75, 0.88),
    "tagging": (0.88, 0.95),
    "verifying": (0.95, 1.0),
}

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_job_id() -> str:
    """`dl_` + a ULID (time-sortable, 26 chars)."""
    value = (int(time.time() * 1000) << 80) | int.from_bytes(os.urandom(10), "big")
    chars = []
    for _ in range(26):
        value, rem = divmod(value, 32)
        chars.append(_CROCKFORD[rem])
    return "dl_" + "".join(reversed(chars))


@dataclass
class Job:
    video_id: str
    format: str
    quality: str
    dedupe_key: str
    overrides: MetadataOverrides | None = None
    embed_artwork: bool = True
    embed_lyrics: bool = True
    enrich: bool = True
    id: str = field(default_factory=new_job_id)
    owners: set[str] = field(default_factory=set)
    status: str = "queued"
    progress: float = 0.0
    stage: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    error: dict | None = None
    attempts: int = 0
    cache_hit: bool = False
    file_key: str | None = None
    file_meta: dict | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    cancel_reason: str | None = None

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def set_status(self, status: str) -> None:
        self.status = status
        self.stage = {"name": status, "progress": 0.0} if status in STAGE_RANGES else {}
        if status in STAGE_RANGES:
            self.progress = max(self.progress, STAGE_RANGES[status][0])
        if self.terminal:
            self.finished_at = time.time()
        self.updated_at = time.time()

    def set_stage_progress(self, fraction: float, **details) -> None:
        fraction = min(1.0, max(0.0, fraction))
        start, end = STAGE_RANGES.get(self.status, (self.progress, self.progress))
        self.progress = max(self.progress, round(start + (end - start) * fraction, 4))
        self.stage = {"name": self.status, "progress": round(fraction, 4),
                      **{k: v for k, v in details.items() if v is not None}}
        self.updated_at = time.time()

    def complete(self, key: str, meta: dict, *, cache_hit: bool = False) -> None:
        self.file_key, self.file_meta, self.cache_hit = key, meta, cache_hit
        self.progress = 1.0
        self.set_status("completed")

    def fail(self, error: dict) -> None:
        self.error = error
        self.set_status("failed")


class JobRegistry:
    IDEMPOTENCY_TTL = 86400

    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.inflight: dict[str, str] = {}  # dedupe_key → job_id while queued/running
        self._idempotency: dict[tuple[str, str], tuple[str, float]] = {}

    def add(self, job: Job) -> None:
        self.jobs[job.id] = job
        if not job.terminal:
            self.inflight[job.dedupe_key] = job.id

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def find_inflight(self, dedupe_key: str) -> Job | None:
        job = self.jobs.get(self.inflight.get(dedupe_key, ""))
        return job if job and not job.terminal else None

    def release_inflight(self, job: Job) -> None:
        if self.inflight.get(job.dedupe_key) == job.id:
            del self.inflight[job.dedupe_key]

    def active_count(self, owner: str) -> int:
        return sum(1 for j in self.jobs.values() if owner in j.owners and not j.terminal)

    def active_jobs(self) -> list[Job]:
        return [j for j in self.jobs.values() if not j.terminal]

    def references(self, file_key: str, exclude: str | None = None) -> int:
        return sum(1 for j in self.jobs.values()
                   if j.file_key == file_key and j.id != exclude and j.status == "completed" and j.owners)

    def remove(self, job_id: str) -> None:
        job = self.jobs.pop(job_id, None)
        if job:
            self.release_inflight(job)

    # Idempotency-Key support
    def idempotent_job(self, owner: str, key: str) -> Job | None:
        entry = self._idempotency.get((owner, key))
        if entry and entry[1] > time.time():
            return self.jobs.get(entry[0])
        return None

    def remember_idempotency(self, owner: str, key: str, job_id: str) -> None:
        self._idempotency[(owner, key)] = (job_id, time.time() + self.IDEMPOTENCY_TTL)

    def cleanup(self, job_ttl: int) -> None:
        now = time.time()
        for job_id, job in list(self.jobs.items()):
            if job.terminal and now - (job.finished_at or job.updated_at) > job_ttl:
                self.remove(job_id)
        for k, (_, expires) in list(self._idempotency.items()):
            if expires <= now:
                del self._idempotency[k]
