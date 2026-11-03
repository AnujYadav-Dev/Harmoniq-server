"""FastAPI application factory. Start it with `python -m app` (see app/__main__.py)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import logging.handlers
import platform
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version

from fastapi import FastAPI

from app import __version__
from app.api import errors
from app.api.v1 import downloads, playlists, search, system, videos
from app.config import Settings, get_settings, prepend_tools_to_path
from app.jobs.maintenance import canary_loop, sweeper_loop
from app.media.ffmpeg import tool_version
from app.providers.ytdlp_client import yt_dlp_version
from app.services import Services

log = logging.getLogger("harmoniq")


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = errors.request_id_var.get()
        return True


def configure_logging(settings: Settings) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s [%(request_id)s] %(message)s")
    root = logging.getLogger()
    root.setLevel(settings.log_level.upper())
    for handler in list(root.handlers):
        if getattr(handler, "_harmoniq", False):
            root.removeHandler(handler)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    log_dir = settings.data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handlers.append(logging.handlers.RotatingFileHandler(log_dir / "server.log", maxBytes=10 * 1024**2,
                                                         backupCount=3, encoding="utf-8"))
    for handler in handlers:
        handler._harmoniq = True
        handler.setFormatter(fmt)
        handler.addFilter(_RequestIdFilter())
        root.addHandler(handler)


def _pkg_version(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def collect_versions(settings: Settings) -> dict:
    return {
        "yt_dlp": yt_dlp_version(),
        "yt_dlp_ejs": _pkg_version("yt-dlp-ejs"),
        "ytmusicapi": _pkg_version("ytmusicapi"),
        "ffmpeg": tool_version(settings.tool("ffmpeg")),
        settings.ytdlp_js_runtime: tool_version(settings.tool(settings.ytdlp_js_runtime), ("--version",)),
        "python": platform.python_version(),
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    services = Services.build(settings)
    app.state.services = services
    services.versions = await asyncio.to_thread(collect_versions, settings)
    log.info("harmoniq-server %s starting; components: %s; metadata providers: %s", __version__,
             services.versions, services.resolver.names)
    if not settings.tokens:
        log.warning("APP_TOKENS is empty: every /v1 request will be rejected. Run the setup script or edit .env.")
    for name in ("ffmpeg", "ffprobe", settings.ytdlp_js_runtime):
        if settings.tool(name) is None:
            log.warning("%s not found (TOOLS_DIR=%s). Downloads will fail until it is installed.", name,
                        settings.tools_dir)

    services.store.clean_tmp(0)
    await services.jobs.start()
    background = [asyncio.create_task(sweeper_loop(services.store, services.registry, settings), name="sweeper")]
    if settings.canary_video_id:
        background.append(asyncio.create_task(canary_loop(services.pipeline, services.health, settings),
                                              name="canary"))
    try:
        yield
    finally:
        for task in background:
            task.cancel()
        with contextlib.suppress(Exception):
            await asyncio.gather(*background, return_exceptions=True)
        await services.jobs.stop()
        services.close()
        log.info("harmoniq-server stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    prepend_tools_to_path(settings)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    configure_logging(settings)

    app = FastAPI(
        title="harmoniq-server",
        version=__version__,
        summary="YouTube search, preview info and tagged audio downloads for the Harmoniq app.",
        lifespan=lifespan,
        docs_url="/docs" if settings.is_dev else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.is_dev else None,
    )
    app.state.settings = settings
    errors.install(app)
    app.include_router(system.router)
    for module in (search, videos, playlists, downloads):
        app.include_router(module.router, prefix="/v1")
    return app
