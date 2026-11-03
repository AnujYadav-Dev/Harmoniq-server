"""FFmpeg remux/transcode and ffprobe verification (run from the pipeline's worker thread)."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from app.domain.errors import AppError, JobCancelled

# Output format → (codec ffprobe reports, mime type, codec of a source we can copy without re-encoding)
OUTPUTS = {
    "m4a": ("aac", "audio/mp4", "aac"),
    "opus": ("opus", "audio/ogg", "opus"),
    "mp3": ("mp3", "audio/mpeg", None),
}

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def build_command(ffmpeg: str, src: Path, dst: Path, fmt: str, source_codec: str | None,
                  quality: str, mp3_standard_bitrate: str) -> tuple[list[str], bool]:
    """Returns (argv, transcoded)."""
    base = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
            "-map", "0:a:0", "-vn", "-map_metadata", "-1", "-map_chapters", "-1"]
    copyable = OUTPUTS[fmt][2]
    if copyable and source_codec == copyable:
        codec_args, transcoded = ["-c:a", "copy"], False
    elif fmt == "m4a":
        codec_args, transcoded = ["-c:a", "aac", "-b:a", "192k"], True
    elif fmt == "opus":
        codec_args, transcoded = ["-c:a", "libopus", "-b:a", "160k"], True
    else:
        rate = ["-q:a", "0"] if quality == "best" else ["-b:a", mp3_standard_bitrate]
        codec_args, transcoded = ["-c:a", "libmp3lame", *rate], True
    container = {"m4a": ["-movflags", "+faststart", "-f", "mp4"], "opus": ["-f", "ogg"], "mp3": ["-f", "mp3"]}[fmt]
    return [*base, *codec_args, *container, "-progress", "pipe:1", "-nostats", str(dst)], transcoded


def run_ffmpeg(argv: list[str], *, duration: float | None, timeout: float, cancel: threading.Event,
               on_progress: Callable[[float], None] | None = None) -> None:
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", creationflags=_NO_WINDOW)
    stderr_chunks: list[str] = []
    reader = threading.Thread(target=lambda: stderr_chunks.append(proc.stderr.read()), daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    try:
        for line in proc.stdout:  # "-progress pipe:1" emits key=value lines
            if cancel.is_set():
                raise JobCancelled()
            if time.monotonic() > deadline:
                raise AppError("processing_failed", "FFmpeg took too long.")
            if on_progress and duration and line.startswith("out_time_us="):
                try:
                    on_progress(min(1.0, int(line.split("=", 1)[1]) / 1_000_000 / duration))
                except ValueError:
                    pass
        proc.wait(timeout=max(1.0, deadline - time.monotonic()))
    except (JobCancelled, AppError, subprocess.TimeoutExpired) as exc:
        proc.kill()
        proc.wait()
        if isinstance(exc, subprocess.TimeoutExpired):
            raise AppError("processing_failed", "FFmpeg took too long.") from exc
        raise
    reader.join(timeout=5)
    if proc.returncode != 0:
        raise AppError("processing_failed", f"FFmpeg failed: {''.join(stderr_chunks).strip()[-400:]}")


def probe(ffprobe: str, path: Path) -> dict:
    out = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries",
         "format=duration,bit_rate:stream=codec_type,codec_name,sample_rate,channels",
         "-of", "json", str(path)],
        capture_output=True, text=True, timeout=60, creationflags=_NO_WINDOW,
    )
    if out.returncode != 0:
        raise AppError("processing_failed", f"ffprobe failed: {out.stderr.strip()[-300:]}")
    data = json.loads(out.stdout or "{}")
    audio = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    fmt = data.get("format", {})
    return {
        "audio_streams": len(audio),
        "codec": audio[0].get("codec_name") if audio else None,
        "sample_rate": int(audio[0]["sample_rate"]) if audio and audio[0].get("sample_rate") else None,
        "channels": audio[0].get("channels") if audio else None,
        "duration": float(fmt["duration"]) if fmt.get("duration") else None,
        "bit_rate": int(fmt["bit_rate"]) if fmt.get("bit_rate") else None,
    }


def tool_version(binary: str | None, args: tuple[str, ...] = ("-version",)) -> str | None:
    if not binary:
        return None
    try:
        out = subprocess.run([binary, *args], capture_output=True, text=True, timeout=15, creationflags=_NO_WINDOW)
    except OSError:
        return None
    first = (out.stdout or out.stderr).splitlines()[:1]
    return first[0].strip() if first else None
