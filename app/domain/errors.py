"""Stable error codes shared by the HTTP layer and the job pipeline (docs/api.md §Errors)."""

from __future__ import annotations

# code → (HTTP status, title, retryable)
ERRORS: dict[str, tuple[int, str, bool]] = {
    "invalid_query": (400, "Invalid query", False),
    "invalid_request": (422, "Invalid request", False),
    "unauthorized": (401, "Unauthorized", False),
    "forbidden": (403, "Forbidden", False),
    "job_not_found": (404, "Job not found", False),
    "video_not_found": (404, "Video not found", False),
    "artwork_not_available": (404, "No artwork", False),
    "lyrics_not_available": (404, "No lyrics", False),
    "job_not_ready": (409, "Job not ready", True),
    "job_expired": (410, "Job expired", False),
    "invalid_video_id": (422, "Invalid video ID", False),
    "invalid_format": (422, "Invalid format", False),
    "video_unavailable": (422, "Video unavailable", False),
    "age_restricted": (422, "Age-restricted video", False),
    "live_not_supported": (422, "Live streams are not supported", False),
    "duration_limit_exceeded": (422, "Video too long", False),
    "rate_limited": (429, "Rate limited", True),
    "too_many_active_jobs": (429, "Too many active jobs", True),
    "upstream_blocked": (502, "Blocked by YouTube", True),
    "upstream_error": (502, "YouTube error", True),
    "processing_failed": (502, "Audio processing failed", True),
    "queue_full": (503, "Queue full", True),
    "service_unavailable": (503, "Service unavailable", True),
    "upstream_timeout": (504, "YouTube timed out", True),
    "internal_error": (500, "Internal server error", True),
}


class AppError(Exception):
    def __init__(self, code: str, detail: str | None = None, *, retry_after: int | None = None):
        if code not in ERRORS:
            raise ValueError(f"unknown error code {code!r}")
        self.code = code
        self.status, self.title, self.retryable = ERRORS[code]
        self.detail = detail or self.title
        self.retry_after = retry_after
        super().__init__(f"{code}: {self.detail}")

    def as_job_error(self) -> dict:
        return {"code": self.code, "detail": self.detail, "retryable": self.retryable}


class JobCancelled(Exception):
    """Raised inside the pipeline thread when a job is cancelled or timed out."""
