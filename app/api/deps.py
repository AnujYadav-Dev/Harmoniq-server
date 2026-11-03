"""Request-scoped dependencies: services, app-token auth, device identity, rate limits."""

from __future__ import annotations

import hmac
import re
from dataclasses import dataclass

from fastapi import Request, Response

from app.domain.errors import AppError
from app.services import Services

_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")


def get_services(request: Request) -> Services:
    return request.app.state.services


@dataclass
class Client:
    token_id: str
    device: str  # "dev:<X-Device-Id>" or "ip:<address>"


def require_client(request: Request, response: Response) -> Client:
    services = get_services(request)
    settings = services.settings
    presented = request.headers.get("x-app-token", "")
    token_id = None
    for tid, secret in settings.tokens.items():
        if secret and hmac.compare_digest(presented.encode(), secret.encode()):
            token_id = tid
    if token_id is None:
        raise AppError("unauthorized", "Missing or invalid X-App-Token. Install the latest app build.")

    device_header = request.headers.get("x-device-id", "")
    if _DEVICE_ID_RE.match(device_header):
        device = f"dev:{device_header.lower()}"
    else:
        device = f"ip:{request.client.host if request.client else 'unknown'}"
    request.state.device = device
    return Client(token_id=token_id, device=device)


def count_upstream_request(request: Request) -> None:
    """Global safety net for routes that cost YouTube traffic (search, metadata, new downloads).

    Cheap routes (job status, events, files) are not counted, so polling can't starve them.
    """
    services = get_services(request)
    ok, _, reset = services.limiter.hit("global", "all", services.settings.rate_limit_global_per_min, 60)
    if not ok:
        raise AppError("rate_limited", "The server is receiving too many requests.", retry_after=reset)


def enforce_limit(request: Request, response: Response, client: Client, bucket: str, limit: int, window: int) -> None:
    services = get_services(request)
    ok, remaining, reset = services.limiter.hit(bucket, client.device, limit, window)
    response.headers["RateLimit-Limit"] = str(limit)
    response.headers["RateLimit-Remaining"] = str(remaining)
    response.headers["RateLimit-Reset"] = str(reset)
    if not ok:
        raise AppError("rate_limited", f"Limit of {limit} {bucket} requests reached.", retry_after=reset)
