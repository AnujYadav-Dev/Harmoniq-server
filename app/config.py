"""Settings, read from environment variables and the `.env` file (see docs/deployment.md §5)."""

from __future__ import annotations

import os
import shutil
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FORMATS = ("m4a", "opus", "mp3")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    # Core
    host: str = "0.0.0.0"
    port: int = 8000
    app_env: str = "production"
    log_level: str = "INFO"
    app_tokens: str = ""
    public_base_url: str = ""
    data_dir: Path = PROJECT_ROOT / "data"
    tools_dir: str = str(PROJECT_ROOT / "tools")

    # Limits
    max_concurrent_downloads: int = 2
    queue_max_depth: int = 20
    max_active_jobs_per_device: int = 3
    rate_limit_search_per_min: int = 30
    rate_limit_downloads_per_hour: int = 60
    rate_limit_global_per_min: int = 120
    max_duration_seconds: int = 900
    job_timeout_seconds: int = 600
    yt_min_interval_seconds: float = 2.0
    search_timeout_seconds: float = 10.0
    metadata_timeout_seconds: float = 20.0
    max_playlist_tracks: int = 200
    search_all_section_limit: int = 10

    # Caching and storage
    search_cache_ttl: int = 21600
    metadata_cache_ttl: int = 3600
    job_ttl_seconds: int = 86400
    file_ttl_seconds: int = 86400
    storage_max_bytes: int = 5 * 1024**3
    disk_min_free_bytes: int = 1024**3

    # Audio
    default_format: str = "m4a"
    mp3_standard_bitrate: str = "192k"
    artwork_max_px: int = 1200

    # YouTube access
    ytdlp_js_runtime: str = "deno"
    pot_provider_url: str = ""
    ytdlp_cookies_file: str = ""
    ytdlp_proxy: str = ""
    ytdlp_ip_version: str = "auto"
    ytdlp_sleep_requests: float = 0.5
    canary_video_id: str = ""

    # Metadata
    metadata_providers: str = "ytmusic,youtube,musicbrainz,lastfm,lyrics"
    lyrics_providers: str = "ytmusic,lrclib"
    musicbrainz_contact: str = ""
    lastfm_api_key: str = ""
    metadata_timeout_seconds: float = 25.0
    filename_template: str = "{artist} - {title}"

    @field_validator("default_format")
    @classmethod
    def _check_format(cls, v: str) -> str:
        if v not in FORMATS:
            raise ValueError(f"DEFAULT_FORMAT must be one of {FORMATS}")
        return v

    @field_validator("ytdlp_ip_version")
    @classmethod
    def _check_ip_version(cls, v: str) -> str:
        v = v.strip().lower() or "auto"
        if v not in ("auto", "4", "6"):
            raise ValueError("YTDLP_IP_VERSION must be auto, 4 or 6")
        return v

    @field_validator("filename_template")
    @classmethod
    def _check_template(cls, v: str) -> str:
        try:
            v.format(artist="a", title="t", album="b", year="2000", track="01")
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError("FILENAME_TEMPLATE may only use {artist} {title} {album} {year} {track}") from exc
        return v

    @field_validator("data_dir")
    @classmethod
    def _absolute_data_dir(cls, v: Path) -> Path:
        return v if v.is_absolute() else (PROJECT_ROOT / v)

    @property
    def is_dev(self) -> bool:
        return self.app_env.lower() == "development"

    @property
    def tokens(self) -> dict[str, str]:
        """`id:secret,id2:secret2` → {id: secret}. A bare secret gets the id `app`."""
        result: dict[str, str] = {}
        for i, part in enumerate(p.strip() for p in self.app_tokens.split(",")):
            if not part:
                continue
            token_id, sep, secret = part.partition(":")
            if not sep:
                token_id, secret = f"app{i or ''}", token_id
            result[token_id.strip()] = secret.strip()
        return result

    @property
    def metadata_provider_list(self) -> list[str]:
        return [p.strip().lower() for p in self.metadata_providers.split(",") if p.strip()]

    @property
    def lyrics_provider_list(self) -> list[str]:
        return [p.strip().lower() for p in self.lyrics_providers.split(",") if p.strip()]

    @property
    def tools_path(self) -> Path | None:
        if not self.tools_dir.strip():
            return None
        p = Path(self.tools_dir)
        return p if p.is_absolute() else (PROJECT_ROOT / p)

    def tool(self, name: str) -> str | None:
        """Absolute path of a tool binary: tools/ first, then PATH."""
        tools = self.tools_path
        if tools is not None:
            for candidate in (tools / name, tools / f"{name}.exe"):
                if candidate.is_file():
                    return str(candidate)
        return shutil.which(name)

    @property
    def cookies_path(self) -> str | None:
        if not self.ytdlp_cookies_file:
            return None
        p = Path(self.ytdlp_cookies_file)
        p = p if p.is_absolute() else (PROJECT_ROOT / p)
        return str(p) if p.is_file() else None


@lru_cache
def get_settings() -> Settings:
    return Settings()


def prepend_tools_to_path(settings: Settings) -> None:
    tools = settings.tools_path
    if tools is not None and tools.is_dir():
        os.environ["PATH"] = str(tools) + os.pathsep + os.environ.get("PATH", "")
