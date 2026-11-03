"""Real FFmpeg + tagging tests on generated audio (skipped when FFmpeg isn't installed).

Tags are read back with TagLib itself (pytaglib): the same library and property-map keys that Harmoniq's
`:metaphony` module uses, so these assertions are exactly what the app will see.
"""

from __future__ import annotations

import subprocess
import threading
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from app.domain.errors import AppError
from app.jobs.pipeline import DownloadPipeline
from app.jobs.registry import Job
from app.media import artwork, quality
from app.media.ffmpeg import build_command, run_ffmpeg
from app.media.tags import write_tags
from app.metadata.model import TrackMetadata
from app.storage.local import LocalFileStore
from tests.conftest import VIDEO_INFO, make_settings

taglib = pytest.importorskip("taglib", reason="pytaglib not installed (pip install pytaglib)")

SETTINGS = make_settings(Path("."))
FFMPEG, FFPROBE = SETTINGS.tool("ffmpeg"), SETTINGS.tool("ffprobe")
pytestmark = pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="FFmpeg not installed (run the setup script)")

SOURCES = {  # what YouTube serves: AAC in .m4a (itag 140) and Opus in .webm (itag 251)
    "aac": ("source.m4a", ["-c:a", "aac", "-b:a", "128k"]),
    "opus": ("source.webm", ["-c:a", "libopus", "-b:a", "128k"]),
}
LRC = "[00:00.000]♪\n[00:12.340]Paper lanterns drifting down the river\n[00:16.500]Every light a wish we never said"

FULL = TrackMetadata(
    title="Get Lucky (feat. Pharrell Williams and Nile Rodgers)",
    artists=["Daft Punk", "Pharrell Williams", "Nile Rodgers"], album_artist="Daft Punk",
    album="Random Access Memories", track_number=8, track_total=13, disc_number=1, disc_total=1,
    date="2013-05-17", genres=["Electronic", "Disco", "Funk"], isrc="USQX91300108", explicit=False,
    lyrics=LRC, lyrics_synced=True, lyrics_source="test",
    musicbrainz={"release_group_id": "aa997ea0-2936-40bd-884d-3af8a0e064dc"},
)


def make_source(tmp_path: Path, codec: str, seconds: int = 5, silent: bool = False) -> Path:
    name, args = SOURCES[codec]
    out = tmp_path / name
    src = "anullsrc=r=48000:cl=stereo" if silent else f"sine=frequency=440:duration={seconds}"
    extra = ["-t", str(seconds)] if silent else ["-ac", "2"]
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", src, *extra, *args,
                    str(out)], check=True)
    return out


def cover_bytes() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (64, 64), "blue").save(buf, "JPEG")
    return buf.getvalue()


def convert(tmp_path: Path, source_codec: str, fmt: str, **kw) -> Path:
    src = make_source(tmp_path, source_codec, **kw)
    dst = tmp_path / f"out.{fmt}"
    argv, _ = build_command(FFMPEG, src, dst, fmt, source_codec, "best", "192k")
    run_ffmpeg(argv, duration=5, timeout=60, cancel=threading.Event())
    return dst


def harmoniq_view(path: Path) -> dict[str, list[str]]:
    """TagLib property map, i.e. what AudioMetadataParser.putTag() receives in the app."""
    with taglib.File(str(path)) as f:
        return dict(f.tags)


@pytest.mark.parametrize("source_codec", ["aac", "opus"])
@pytest.mark.parametrize("fmt", ["m4a", "opus", "mp3"])
def test_tags_as_harmoniq_sees_them(tmp_path, source_codec, fmt):
    dst = convert(tmp_path, source_codec, fmt)
    write_tags(dst, fmt, FULL, cover_bytes(), video_id="4D7u5KF7SP8", encoder="Harmoniq Server test")
    tags = harmoniq_view(dst)

    assert tags["TITLE"] == ["Get Lucky (feat. Pharrell Williams and Nile Rodgers)"]
    assert tags["ARTIST"] == ["Daft Punk; Pharrell Williams; Nile Rodgers"]  # app splits on ";"
    assert tags["ALBUMARTIST"] == ["Daft Punk"]
    assert tags["ALBUM"] == ["Random Access Memories"]
    assert tags["DATE"] == ["2013-05-17"]  # full date: the only form Harmoniq's parseDate accepts
    assert tags["GENRE"] == ["Electronic; Disco; Funk"]
    assert tags["ISRC"] == ["USQX91300108"]
    assert tags["LYRICS"] == [LRC]
    track = tags["TRACKNUMBER"][0] + ("/" + tags["TRACKTOTAL"][0] if "TRACKTOTAL" in tags else "")
    assert track == "8/13"
    assert tags["DISCNUMBER"][0].split("/")[0] == "1"
    assert tags["MUSICBRAINZ_RELEASEGROUPID"] == ["aa997ea0-2936-40bd-884d-3af8a0e064dc"]
    assert set(tags.get("ARTISTS", [])) == {"Daft Punk", "Pharrell Williams", "Nile Rodgers"}
    with taglib.File(str(dst)) as f:  # embedded front cover
        assert f.length >= 4


def test_minimal_metadata_writes_only_known_fields(tmp_path):
    dst = convert(tmp_path, "aac", "m4a")
    write_tags(dst, "m4a", TrackMetadata(title="Only Title"), None, video_id="abcdefghijk", encoder="t")
    tags = harmoniq_view(dst)
    assert tags["TITLE"] == ["Only Title"]
    assert not {"ARTIST", "ALBUM", "DATE", "GENRE", "LYRICS", "TRACKNUMBER"} & set(tags)


def test_quality_report_and_silence_rejection(tmp_path):
    good = convert(tmp_path, "aac", "m4a")
    report = quality.validate(FFMPEG, FFPROBE, good, "m4a", 5)
    assert report["codec"] == "aac" and report["sample_rate"] in (44100, 48000) and report["max_volume_db"] > -30  # lavfi sine peaks at about -21 dB

    silent_dir = tmp_path / "silent"
    silent_dir.mkdir()
    silent = convert(silent_dir, "aac", "m4a", silent=True)
    with pytest.raises(AppError) as exc:
        quality.validate(FFMPEG, FFPROBE, silent, "m4a", 5)
    assert "silent" in exc.value.detail

    with pytest.raises(AppError) as exc:
        quality.validate(FFMPEG, FFPROBE, good, "m4a", 60)
    assert "truncated" in exc.value.detail


def test_quality_check_handles_non_latin_tags(tmp_path):
    """FFmpeg prints the file's UTF-8 tags to stderr; on Windows the default cp1252 decoding used to crash the
    reader thread, so the silence check was skipped without any error."""
    dst = convert(tmp_path, "opus", "opus")
    md = TrackMetadata(title="चाँद आ चल के तुझे", artists=["तलत महमूद"], lyrics="[00:01.000]चाँद तारे ♪ 夜空")
    write_tags(dst, "opus", md, None, video_id="abcdefghijk", encoder="t")
    report = quality.validate(FFMPEG, FFPROBE, dst, "opus", 5)
    assert report["max_volume_db"] is not None  # loudness was actually measured


def test_ffmpeg_failure_is_processing_failed(tmp_path):
    bad = tmp_path / "bad.m4a"
    bad.write_bytes(b"not audio")
    argv, _ = build_command(FFMPEG, bad, tmp_path / "o.m4a", "m4a", "aac", "best", "192k")
    with pytest.raises(AppError) as exc:
        run_ffmpeg(argv, duration=5, timeout=30, cancel=threading.Event())
    assert exc.value.code == "processing_failed"


# -- full pipeline with fake YouTube + fake metadata ----------------------------------------------
class _FakeDownloader:
    def __init__(self, seconds: int = 5, claimed_duration: int = 5):
        self.seconds, self.claimed = seconds, claimed_duration

    def download(self, video_id, selector, out_dir, *, check, on_progress, cancel):
        assert "drc" in selector  # DRC streams are excluded
        info = {**VIDEO_INFO, "id": video_id, "duration": self.claimed, "acodec": "opus", "ext": "webm",
                "format_id": "251"}
        check(info)
        on_progress({"status": "downloading", "downloaded_bytes": 10, "total_bytes": 10})
        return info, make_source(out_dir, "opus", self.seconds)


class _FakeResolver:
    names = ["fake"]

    def __init__(self, md: TrackMetadata):
        self.md = md
        self.seen_ctx = None

    def resolve(self, ctx, partial=None):
        self.seen_ctx = ctx
        assert ctx.wait_info(5) is not None  # the pipeline hands over the yt-dlp info
        return self.md


def _pipeline(tmp_path, resolver, **downloader):
    settings = make_settings(tmp_path)
    store = LocalFileStore(settings.data_dir)
    return DownloadPipeline(settings, _FakeDownloader(**downloader), store, resolver), store


def test_full_pipeline_publishes_fully_tagged_file(tmp_path, monkeypatch):
    md = TrackMetadata(**{**FULL.__dict__, "sources": {}, "extras": {"video_type": "MUSIC_VIDEO_TYPE_ATV"}})
    pipeline, store = _pipeline(tmp_path, _FakeResolver(md))
    monkeypatch.setattr(artwork, "fetch_square_jpeg", lambda urls, px: (cover_bytes(), 64))
    job = Job(video_id="4D7u5KF7SP8", format="opus", quality="best", dedupe_key="k1")
    key, meta = pipeline.run(job)

    stored = store.get(key)
    assert stored is not None and stored.audio.suffix == ".opus" and stored.artwork is not None
    assert meta["filename"] == "Daft Punk - Get Lucky (feat. Pharrell Williams and Nile Rodgers).opus"
    assert meta["transcoded"] is False and meta["quality"]["source_format_id"] == "251"
    assert meta["metadata"]["track_number"] == 8 and meta["metadata"]["genres"] == ["Electronic", "Disco", "Funk"]
    assert meta["metadata"]["lyrics"] == {"available": True, "synced": True, "source": "test", "lines": 3}
    assert meta["lyrics_text"] == LRC and meta["metadata"]["video_type"] == "MUSIC_VIDEO_TYPE_ATV"
    tags = harmoniq_view(stored.audio)
    assert tags["ALBUM"] == ["Random Access Memories"] and tags["DATE"] == ["2013-05-17"]
    assert not any(store.tmp_dir.iterdir())


def test_pipeline_can_skip_lyrics(tmp_path, monkeypatch):
    monkeypatch.setattr(artwork, "fetch_square_jpeg", lambda urls, px: None)
    resolver = _FakeResolver(TrackMetadata(title="T", artists=["A"], lyrics=LRC, lyrics_synced=True))
    pipeline, store = _pipeline(tmp_path, resolver)
    job = Job(video_id="4D7u5KF7SP8", format="m4a", quality="best", dedupe_key="k4", embed_lyrics=False)
    key, meta = pipeline.run(job)
    assert resolver.seen_ctx.fetch_lyrics is False
    assert "LYRICS" not in harmoniq_view(store.get(key).audio) and meta["lyrics_text"] is None


def test_pipeline_falls_back_to_video_title_without_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(artwork, "fetch_square_jpeg", lambda urls, px: None)
    pipeline, store = _pipeline(tmp_path, _FakeResolver(TrackMetadata()))
    job = Job(video_id="4D7u5KF7SP8", format="m4a", quality="best", dedupe_key="k5")
    _, meta = pipeline.run(job)
    assert meta["metadata"]["title"] == VIDEO_INFO["title"]


def test_pipeline_rejects_truncated_download(tmp_path):
    pipeline, store = _pipeline(tmp_path, _FakeResolver(TrackMetadata(title="T")), seconds=3, claimed_duration=60)
    job = Job(video_id="FGBhQbmPwH8", format="opus", quality="best", dedupe_key="k2", embed_artwork=False)
    with pytest.raises(AppError) as exc:
        pipeline.run(job)
    assert exc.value.code == "processing_failed" and "truncated" in exc.value.detail
    assert store.get("k2") is None


def test_pipeline_rejects_too_long_video(tmp_path):
    settings = make_settings(tmp_path, max_duration_seconds=10)
    pipeline = DownloadPipeline(settings, _FakeDownloader(claimed_duration=11), LocalFileStore(settings.data_dir),
                                _FakeResolver(TrackMetadata()))
    job = Job(video_id="FGBhQbmPwH8", format="m4a", quality="best", dedupe_key="k3")
    with pytest.raises(AppError) as exc:
        pipeline.run(job)
    assert exc.value.code == "duration_limit_exceeded"
