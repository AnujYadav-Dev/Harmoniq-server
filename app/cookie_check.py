"""`python -m app.cookie_check <file>`: is this cookies file a signed-in YouTube session? Used by update-cookies.sh.

A plain download can't tell: YouTube often serves dead cookies as a logged-out visitor. A search reports them instead,
with a "no longer valid" warning, and yt-dlp then drops the sign-in cookies from the file. The file gets rewritten, so
always pass a copy.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yt_dlp

from app.config import Settings, get_settings
from app.providers.ytdlp_options import base_options

PROBE = "ytsearch1:me at the zoo"
SIGN_IN_COOKIE = "SID"


class _Warnings:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        self.messages.append(msg)

    def error(self, msg: str) -> None:
        self.messages.append(msg)


def has_sign_in_cookie(path: Path) -> bool:
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        fields = line.removeprefix("#HttpOnly_").split("\t")
        if not line.startswith("# ") and len(fields) >= 7 and fields[5] == SIGN_IN_COOKIE:
            return True
    return False


def check_cookies(path: Path, settings: Settings) -> str | None:
    """Returns what's wrong with the cookies, or None if YouTube accepts them."""
    if not has_sign_in_cookie(path):
        return f"no {SIGN_IN_COOKIE} cookie, so this isn't a signed-in session"
    logger = _Warnings()
    options = base_options(settings.model_copy(update={"ytdlp_cookies_file": str(path)})) | {
        "logger": logger, "skip_download": True, "extract_flat": True, "playlistend": 1,
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.extract_info(PROBE, download=False)
    except Exception as exc:
        return f"the test search failed: {exc.__class__.__name__}"
    if any("no longer valid" in message for message in logger.messages):
        return "YouTube says these cookies are no longer valid"
    if not has_sign_in_cookie(path):
        return "YouTube rejected the session"
    return None


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python -m app.cookie_check <file>")
        return 2
    problem = check_cookies(Path(sys.argv[1]), get_settings())
    print(problem or "YouTube accepts these cookies.")
    return 1 if problem else 0


if __name__ == "__main__":
    sys.exit(main())
