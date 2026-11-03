"""AppError → RFC 9457 application/problem+json, plus request IDs."""

from __future__ import annotations

import contextvars
import logging
import re
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.domain.errors import ERRORS, AppError

log = logging.getLogger("harmoniq.api")

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def problem(request: Request, code: str, detail: str | None = None, *, retry_after: int | None = None) -> JSONResponse:
    status, title, retryable = ERRORS[code]
    body = {
        "type": f"https://harmoniq.dev/errors/{code}",
        "title": title,
        "status": status,
        "code": code,
        "detail": detail or title,
        "request_id": request_id_var.get(),
        "retryable": retryable,
    }
    headers = {"Retry-After": str(retry_after)} if retry_after else None
    return JSONResponse(body, status_code=status, headers=headers, media_type="application/problem+json")


def install(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        incoming = request.headers.get("x-request-id", "")
        rid = incoming if _REQUEST_ID_RE.match(incoming) else uuid.uuid4().hex[:16]
        token = request_id_var.set(rid)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-ID"] = rid
        return response

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):
        return problem(request, exc.code, exc.detail, retry_after=exc.retry_after)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        return problem(request, "invalid_request", f"{where}: {first.get('msg', 'invalid value')}")

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 404:
            body = {"type": "about:blank", "title": "Not Found", "status": 404, "code": "not_found",
                    "detail": "No such endpoint.", "request_id": request_id_var.get(), "retryable": False}
            return JSONResponse(body, status_code=404, media_type="application/problem+json")
        return JSONResponse({"title": str(exc.detail), "status": exc.status_code, "code": "http_error",
                             "request_id": request_id_var.get()}, status_code=exc.status_code,
                            media_type="application/problem+json")

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception):
        log.exception("Unhandled error on %s %s", request.method, request.url.path)
        return problem(request, "internal_error", "Something went wrong on the server.")
