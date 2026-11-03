"""`python -m app [--dev]` starts the server as a single process (job state lives in memory)."""

from __future__ import annotations

import os
import sys

import uvicorn

from app.config import get_settings


def main() -> None:
    if "--dev" in sys.argv[1:]:
        os.environ["APP_ENV"] = "development"  # inherited by the reload subprocess
        get_settings.cache_clear()
    settings = get_settings()
    dev = settings.is_dev
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        workers=1,
        reload=dev,
        reload_dirs=["app"] if dev else None,
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_level=settings.log_level.lower(),
        timeout_graceful_shutdown=60,
    )


if __name__ == "__main__":
    main()
