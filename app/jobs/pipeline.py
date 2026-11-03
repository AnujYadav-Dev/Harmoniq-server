"""Find → download → process → metadata & artwork → validate → publish. Runs synchronously in a worker thread.

Metadata resolution only needs the video id, so it starts together with the download in a helper thread and is
usually finished by the time FFmpeg is.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path

from app import __version__
from app.config import Settings
from app.domain.errors import AppError, JobCancelled
from app.jobs.registry import Job
from app.media import artwork, quality
from app.media.ffmpeg import OUTPUTS, build_command, run_ffmpeg
from app.media.tags import TAGGER_VERSION, display_filename, read_back, write_tags
from app.metadata.model import MetadataContext, TrackMetadata
from app.metadata.resolver import MetadataResolver, snapshot
from app.providers.ytdlp_client import YtDlpClient
from app.storage.local import LocalFileStore

log = logging.getLogger("harmoniq.pipeline")

# Best audio-only stream, never the dynamic-range-compressed ("-drc") variants. bestaudio[ext=m4a] picks itag 141
# (256 kbps AAC) over 140 (128 kbps) automatically when the cookies' account offers it.
_NO_DRC = "[format_id!*=drc]"
SELECTORS = {
    "m4a": f"bestaudio[ext=m4a]{_NO_DRC}/bestaudio[acodec^=mp4a]{_NO_DRC}/bestaudio{_NO_DRC}/bestaudio",
    "opus": f"bestaudio[acodec=opus]{_NO_DRC}/bestaudio{_NO_DRC}/bestaudio",
    "mp3": f"bestaudio{_NO_DRC}/bestaudio",
}
_CODEC_ALIASES = {"mp4a": "aac", "opus": "opus", "mp3": "mp3", "vorbis": "vorbis"}


def normalize_quality(fmt: str, quality_name: str) -> str:
    return quality_name if fmt == "mp3" else "best"


def dedupe_key(video_id: str, fmt: str, quality_name: str, overrides_json: str, embed_artwork: bool,
               options: str = "") -> str:
    raw = "|".join([video_id, fmt, normalize_quality(fmt, quality_name), TAGGER_VERSION,
                    hashlib.sha1(overrides_json.encode()).hexdigest(), str(embed_artwork), options])
    return hashlib.sha1(raw.encode()).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def overrides_json(overrides) -> str:
    if overrides is None:
        return "{}"
    return json.dumps(overrides.model_dump(exclude_none=True), sort_keys=True)


class DownloadPipeline:
    def __init__(self, settings: Settings, ytdlp: YtDlpClient, store: LocalFileStore,
                 resolver: MetadataResolver) -> None:
        self.settings = settings
        self.ytdlp = ytdlp
        self.store = store
        self.resolver = resolver
        self._metadata_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="metadata")

    def shutdown(self) -> None:
        self._metadata_pool.shutdown(wait=False, cancel_futures=True)

    def run(self, job: Job, *, publish: bool = True) -> tuple[str, dict]:
        """Returns (file key, sidecar metadata). Raises AppError / JobCancelled."""
        ffmpeg, ffprobe = self.settings.tool("ffmpeg"), self.settings.tool("ffprobe")
        if not ffmpeg or not ffprobe:
            raise AppError("service_unavailable", "FFmpeg/ffprobe not found. Run the setup script.")

        ctx = MetadataContext(video_id=job.video_id, overrides=job.overrides, fetch_lyrics=job.embed_lyrics,
                              enrich=job.enrich)
        partial: list[TrackMetadata] = []
        future = self._metadata_pool.submit(self.resolver.resolve, ctx, partial=partial)
        tmp = self.store.new_tmp(job.id)
        try:
            return self._run(job, tmp, ffmpeg, ffprobe, publish, ctx, future, partial)
        finally:
            ctx.cancel.set()
            ctx.set_info(ctx.info)  # unblock providers still waiting for the yt-dlp info
            shutil.rmtree(tmp, ignore_errors=True)

    def _check_cancel(self, job: Job) -> None:
        if job.cancel.is_set():
            raise JobCancelled()

    def _metadata(self, ctx: MetadataContext, future: Future, partial: list, started: float) -> TrackMetadata:
        budget = max(2.0, self.settings.metadata_timeout_seconds - (time.monotonic() - started))
        try:
            return future.result(timeout=budget)
        except FutureTimeout:
            ctx.cancel.set()
            log.warning("Metadata for %s not complete after %.0f s; using what was found", ctx.video_id,
                        self.settings.metadata_timeout_seconds)
            return snapshot(partial[0]) if partial else TrackMetadata()
        except Exception:
            log.exception("Metadata resolver crashed for %s", ctx.video_id)
            return snapshot(partial[0]) if partial else TrackMetadata()

    def _run(self, job: Job, tmp: Path, ffmpeg: str, ffprobe: str, publish: bool, ctx: MetadataContext,
             future: Future, partial: list) -> tuple[str, dict]:
        s = self.settings
        fmt = job.format
        started = time.monotonic()

        # 1. find + download ---------------------------------------------------------------------------
        job.set_status("resolving")

        def check(info: dict) -> None:
            ctx.set_info(info)
            if info.get("live_status") in ("is_live", "is_upcoming", "post_live") or info.get("is_live"):
                raise AppError("live_not_supported", "Live and upcoming streams can't be downloaded.")
            duration = info.get("duration") or 0
            if duration > s.max_duration_seconds:
                raise AppError("duration_limit_exceeded",
                               f"Video is {int(duration)} s long; the limit is {s.max_duration_seconds} s.")
            if info.get("acodec") in (None, "none"):
                raise AppError("upstream_error", "No audio-only format was offered for this video.")
            job.set_status("downloading")

        def on_progress(d: dict) -> None:
            if d.get("status") != "downloading":
                return
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            job.set_stage_progress(done / total if total else 0.0, downloaded_bytes=done, total_bytes=total,
                                   speed_bps=int(d["speed"]) if d.get("speed") else None,
                                   eta_seconds=d.get("eta"))

        info, source = self.ytdlp.download(job.video_id, SELECTORS[fmt], tmp,
                                           check=check, on_progress=on_progress, cancel=job.cancel)
        self._check_cancel(job)
        source_codec = _CODEC_ALIASES.get((info.get("acodec") or "").split(".")[0])
        duration = info.get("duration")

        # 2. process (remux first) ---------------------------------------------------------------------
        job.set_status("processing")
        out = tmp / f"out.{fmt}"
        argv, transcoded = build_command(ffmpeg, source, out, fmt, source_codec, job.quality, s.mp3_standard_bitrate)
        run_ffmpeg(argv, duration=duration, timeout=s.job_timeout_seconds, cancel=job.cancel,
                   on_progress=job.set_stage_progress)
        source.unlink(missing_ok=True)

        # 3. metadata + artwork ------------------------------------------------------------------------
        job.set_status("tagging")
        md = self._metadata(ctx, future, partial, started)
        md.title = md.title or info.get("title") or job.video_id
        md.duration = md.duration or (int(duration) if duration else None)
        self._check_cancel(job)
        job.set_stage_progress(0.3)
        cover = side = None
        if job.embed_artwork:
            got = artwork.fetch_square_jpeg(artwork.candidate_urls(info, md.artwork_url, s.artwork_max_px),
                                            s.artwork_max_px)
            if got:
                cover, side = got
        if not job.embed_lyrics:
            md.lyrics, md.lyrics_synced, md.lyrics_source = None, False, None
        self._check_cancel(job)
        job.set_stage_progress(0.7)
        write_tags(out, fmt, md, cover, video_id=job.video_id, encoder=f"Harmoniq Server {__version__}")

        # 4. validate quality + tags -------------------------------------------------------------------
        job.set_status("verifying")
        report = quality.validate(ffmpeg, ffprobe, out, fmt, duration)
        seen = read_back(out, fmt)
        if seen["title"] != md.title or (md.artists and not seen["artist"]) or (cover and not seen["cover"]):
            raise AppError("processing_failed", f"Tags didn't survive writing: {seen}")
        size = out.stat().st_size
        if size < 32 * 1024:
            raise AppError("processing_failed", "Output file is suspiciously small.")
        digest = sha256_file(out)

        upload = info.get("upload_date")
        public = md.to_public()
        public.update({
            "video_type": md.extras.get("video_type"),
            "source": {
                "provider": "youtube", "video_id": job.video_id,
                "url": f"https://www.youtube.com/watch?v={job.video_id}",
                "channel": info.get("channel") or info.get("uploader"),
                "upload_date": f"{upload[:4]}-{upload[4:6]}-{upload[6:8]}" if upload and len(upload) == 8 else None,
                "format_id": info.get("format_id"),
            },
            "artwork": {"embedded": cover is not None, "width": side, "height": side,
                        "mime_type": "image/jpeg" if cover else None},
        })
        if ctx.warnings:
            public["warnings"] = ctx.warnings
        meta = {
            "video_id": job.video_id,
            "format": fmt,
            "filename": display_filename(md, fmt, s.filename_template, job.video_id),
            "mime_type": OUTPUTS[fmt][1],
            "size_bytes": size,
            "sha256": digest,
            "duration": report["duration"] or duration,
            "codec": report["codec"],
            "bitrate_kbps": report["bitrate_kbps"],
            "sample_rate": report["sample_rate"],
            "channels": report["channels"],
            "transcoded": transcoded,
            "quality": {**report, "transcoded": transcoded, "source_format_id": info.get("format_id"),
                        "source_codec": source_codec},
            "metadata": public,
            "lyrics_text": md.lyrics,
        }
        job.set_stage_progress(1.0)
        self._check_cancel(job)
        if publish:
            self.store.publish(job.dedupe_key, out, fmt, cover, meta)
        log.info("Job %s finished: %s (%s, %d kbps, %d bytes, transcoded=%s, sources=%s)", job.id, meta["filename"],
                 fmt, report["bitrate_kbps"] or 0, size, transcoded, sorted(set(md.sources.values())))
        return job.dedupe_key, meta
