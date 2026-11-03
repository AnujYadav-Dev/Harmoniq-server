"""Quality validation of the finished file: stream layout, codec, duration, bitrate, sample rate, silence."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from app.domain.errors import AppError
from app.media.ffmpeg import OUTPUTS, probe

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_MAX_VOLUME = re.compile(r"max_volume:\s*(-?[\d.]+|-inf)\s*dB")
_MEAN_VOLUME = re.compile(r"mean_volume:\s*(-?[\d.]+|-inf)\s*dB")

MIN_BITRATE_KBPS = 48
MIN_SAMPLE_RATE = 32000
SILENCE_DB = -50.0
DURATION_TOLERANCE = 2.0


def loudness(ffmpeg: str, path: Path, timeout: float = 180) -> tuple[float | None, float | None]:
    out = subprocess.run([ffmpeg, "-nostdin", "-hide_banner", "-i", str(path), "-map", "0:a:0", "-vn",
                          "-af", "volumedetect", "-f", "null", "-"],
                         capture_output=True, text=True, timeout=timeout, creationflags=_NO_WINDOW)
    def grab(rx):
        m = rx.search(out.stderr or "")
        if not m:
            return None
        return float("-inf") if m.group(1) == "-inf" else float(m.group(1))
    return grab(_MAX_VOLUME), grab(_MEAN_VOLUME)


def validate(ffmpeg: str, ffprobe: str, path: Path, fmt: str, expected_duration: float | None) -> dict:
    """Raises AppError('processing_failed') when the file isn't good enough; returns the quality report."""
    p = probe(ffprobe, path)
    expected_codec = OUTPUTS[fmt][0]
    problems = []
    if p["audio_streams"] != 1:
        problems.append(f"{p['audio_streams']} audio streams")
    if p["codec"] != expected_codec:
        problems.append(f"codec {p['codec']} (expected {expected_codec})")
    if expected_duration and p["duration"] and abs(p["duration"] - expected_duration) > DURATION_TOLERANCE:
        problems.append(f"duration {p['duration']:.1f} s but the video is {expected_duration} s (truncated download?)")
    kbps = round(p["bit_rate"] / 1000) if p["bit_rate"] else None
    if kbps is not None and kbps < MIN_BITRATE_KBPS:
        problems.append(f"bitrate {kbps} kbps is below {MIN_BITRATE_KBPS}")
    if p["sample_rate"] and p["sample_rate"] < MIN_SAMPLE_RATE:
        problems.append(f"sample rate {p['sample_rate']} Hz")
    max_db, mean_db = loudness(ffmpeg, path) if p["audio_streams"] else (None, None)
    if max_db is not None and max_db < SILENCE_DB:
        problems.append(f"the audio is silent (peak {max_db} dB)")
    if problems:
        raise AppError("processing_failed", "Quality check failed: " + "; ".join(problems) + ".")
    return {
        "codec": p["codec"], "bitrate_kbps": kbps, "sample_rate": p["sample_rate"], "channels": p["channels"],
        "duration": round(p["duration"], 2) if p["duration"] else None,
        "max_volume_db": max_db if max_db not in (None, float("-inf")) else None,
        "mean_volume_db": mean_db if mean_db not in (None, float("-inf")) else None,
    }
