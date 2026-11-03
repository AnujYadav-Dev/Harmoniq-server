"""Cover art: pick the best thumbnail, download it, square-crop and re-encode as JPEG."""

from __future__ import annotations

import io
import logging
import re
from urllib.parse import urlparse

import httpx
from PIL import Image

log = logging.getLogger("harmoniq.artwork")

ALLOWED_HOST_SUFFIXES = (".googleusercontent.com", ".ytimg.com", ".ggpht.com")
MAX_BYTES = 5 * 1024 * 1024


def is_allowed_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and any(host.endswith(s) or host == s[1:] for s in ALLOWED_HOST_SUFFIXES)


def upscale_music_art(url: str, size: int) -> str:
    """YouTube Music art URLs end in `=w120-h120-...`; ask for a bigger square."""
    return re.sub(r"=w\d+-h\d+(-[^/?#]*)?$", f"=w{size}-h{size}-l90-rj", url)


def candidate_urls(info: dict, override: str | None, max_px: int) -> list[str]:
    urls: list[str] = []
    if override and is_allowed_url(override):
        urls.append(upscale_music_art(override, max_px) if "googleusercontent" in override else override)
    thumbs = sorted(
        (t for t in info.get("thumbnails") or [] if t.get("url")),
        key=lambda t: (t.get("preference") or 0, (t.get("width") or 0) * (t.get("height") or 0)),
        reverse=True,
    )
    urls.extend(t["url"] for t in thumbs[:4] if is_allowed_url(t["url"]))
    if vid := info.get("id"):
        urls.append(f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg")
    seen: set[str] = set()
    return [u for u in urls if not (u in seen or seen.add(u))]


def fetch_square_jpeg(urls: list[str], max_px: int) -> tuple[bytes, int] | None:
    """Returns (jpeg bytes, side length) for the first URL that works, or None."""
    with httpx.Client(timeout=5.0, follow_redirects=True) as client:
        for url in urls:
            try:
                resp = client.get(url)
                if resp.status_code != 200 or len(resp.content) > MAX_BYTES:
                    continue
                if not resp.headers.get("content-type", "image/").startswith("image/"):
                    continue
                return square_jpeg(resp.content, max_px)
            except Exception as exc:  # artwork is best-effort
                log.info("Artwork fetch failed for %s: %s", url, exc)
    return None


def square_jpeg(data: bytes, max_px: int) -> tuple[bytes, int]:
    with Image.open(io.BytesIO(data)) as img:
        img = img.convert("RGB")
        w, h = img.size
        side = min(w, h)
        left, top = (w - side) // 2, (h - side) // 2
        img = img.crop((left, top, left + side, top + side))
        if side > max_px:
            img = img.resize((max_px, max_px), Image.Resampling.LANCZOS)
            side = max_px
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=90, optimize=True)
        return buf.getvalue(), side
