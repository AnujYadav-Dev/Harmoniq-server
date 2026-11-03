from __future__ import annotations

import time

import pytest

from app.config import Settings
from app.domain.errors import AppError
from app.domain.models import MetadataOverrides
from app.jobs.pipeline import dedupe_key, overrides_json
from app.jobs.registry import Job, JobRegistry, new_job_id
from app.media.artwork import candidate_urls, is_allowed_url, square_jpeg, upscale_music_art
from app.media.ffmpeg import build_command
from app.media.tags import display_filename
from app.metadata.model import MetadataContext, TrackMetadata
from app.metadata.providers.youtube import YouTubeProvider
from app.metadata.text import clean_channel, split_title
from app.providers import mapping
from app.providers.ytdlp_client import YouTubeHealth, map_ytdlp_error
from app.storage.local import LocalFileStore
from tests.conftest import MUSIC_RESULT, VIDEO_INFO, make_settings


# -- titles and tags -------------------------------------------------------------------------------
@pytest.mark.parametrize("title, expected", [
    ("Daft Punk - One More Time (Official Video)", ("Daft Punk", "One More Time")),
    ("Adele – Hello [Official Music Video] | XL Recordings", ("Adele", "Hello")),
    ("Song Title (Lyrics)", (None, "Song Title")),
    ("Artist - Track (feat. Someone)", ("Artist", "Track (feat. Someone)")),
    ('Artist - "Quoted" (Audio)', ("Artist", "Quoted")),
    ("Remix - Edit - Thing", ("Remix", "Edit - Thing")),
])
def test_split_title(title, expected):
    assert split_title(title) == expected


def test_clean_channel():
    assert clean_channel("Daft Punk - Topic") == "Daft Punk"
    assert clean_channel("AdeleVEVO") == "Adele"
    assert clean_channel(None) is None


def _youtube(info, overrides=None):
    ctx = MetadataContext(video_id=info["id"], overrides=overrides)
    ctx.set_info(info)
    from app.metadata.model import from_overrides
    md = from_overrides(overrides)
    md.fill(YouTubeProvider().enrich(ctx, md), "youtube")
    return md


def test_youtube_provider_uses_music_fields_then_heuristics():
    info = {**VIDEO_INFO, "track": "One More Time", "artists": ["Daft Punk"], "album": "Discovery",
            "release_year": 2001}
    md = _youtube(info)
    assert (md.title, md.artists, md.album, md.date) == ("One More Time", ["Daft Punk"], "Discovery", "2001")

    md = _youtube(VIDEO_INFO, MetadataOverrides(title="Custom", album="Mine"))
    assert (md.title, md.artists, md.album) == ("Custom", ["Daft Punk"], "Mine")

    md = _youtube({"id": "x" * 11, "title": "Just A Title", "channel": "Somebody - Topic"})
    assert (md.title, md.artists) == ("Just A Title", ["Somebody"])


def test_display_filename_is_safe():
    md = TrackMetadata(title='Back: In "Black"?', artists=["AC/DC"])
    assert display_filename(md, "m4a") == "ACDC - Back In Black.m4a"
    assert display_filename(TrackMetadata(title="x" * 400), "mp3").__len__() <= 154
    assert display_filename(md, "m4a", "{title} - {artist}") == "Back In Black - ACDC.m4a"
    assert display_filename(TrackMetadata(), "opus", fallback="abcdefghijk") == "abcdefghijk.opus"


# -- mapping ---------------------------------------------------------------------------------------
def test_music_result_mapping():
    r = mapping.music_result(MUSIC_RESULT)
    assert r.video_id == "FGBhQbmPwH8" and r.result_type == "song"
    assert r.artists[0].name == "Daft Punk" and r.album.name == "Discovery"
    assert r.duration == 320 and r.duration_text == "5:20"


def test_video_result_filters_non_videos_and_live():
    assert mapping.video_result({"id": "UCabc", "ie_key": "YoutubeTab"}) is None
    assert mapping.video_result({"id": "FGBhQbmPwH8", "ie_key": "Youtube", "live_status": "is_live"}) is None
    r = mapping.video_result({"id": "FGBhQbmPwH8", "ie_key": "Youtube", "title": "t", "duration": 61.0})
    assert r.duration_text == "1:01"


def test_video_metadata_flags_and_formats(tmp_path):
    s = make_settings(tmp_path)
    meta = mapping.video_metadata(VIDEO_INFO, s)
    assert meta.flags.downloadable and meta.flags.embeddable and not meta.flags.age_restricted
    assert [f.format_id for f in meta.audio_formats] == ["251", "140"]
    m4a = next(o for o in meta.output_options if o.format == "m4a")
    assert m4a.default and not m4a.transcode
    assert meta.upload_date == "2009-02-26"

    long = mapping.video_metadata({**VIDEO_INFO, "duration": 5000}, s)
    assert long.flags.reason == "duration_limit_exceeded" and not long.flags.downloadable
    live = mapping.video_metadata({**VIDEO_INFO, "live_status": "is_live"}, s)
    assert live.flags.reason == "live_not_supported"


# -- error mapping ---------------------------------------------------------------------------------
@pytest.mark.parametrize("message, code", [
    ("ERROR: [youtube] abc: Sign in to confirm you’re not a bot. Use --cookies", "upstream_blocked"),
    ("ERROR: unable to download video data: HTTP Error 429: Too Many Requests", "upstream_blocked"),
    ("ERROR: [youtube] abc: Sign in to confirm your age.", "age_restricted"),
    ("ERROR: [youtube] abc: Private video. Sign in if you've been granted access", "video_unavailable"),
    ("ERROR: [youtube] abc: Video unavailable", "video_unavailable"),
    ("ERROR: [youtube] abc: This live event will begin in 3 hours.", "live_not_supported"),
    ("ERROR: something entirely new", "upstream_error"),
])
def test_map_ytdlp_error(message, code):
    health = YouTubeHealth()
    err = map_ytdlp_error(Exception(message), health)
    assert err.code == code
    assert (health.status == "blocked") == (code == "upstream_blocked")


# -- registry --------------------------------------------------------------------------------------
def test_job_ids_are_unique_and_sortable():
    ids = [new_job_id() for _ in range(50)]
    assert len(set(ids)) == 50 and all(i.startswith("dl_") and len(i) == 29 for i in ids)


def test_job_progress_is_monotonic():
    job = Job(video_id="a" * 11, format="m4a", quality="best", dedupe_key="k")
    job.set_status("downloading")
    job.set_stage_progress(0.5)
    assert job.progress == pytest.approx(0.425)
    job.set_stage_progress(0.2)  # never goes backwards
    assert job.progress == pytest.approx(0.425)
    job.set_status("processing")
    assert job.progress == pytest.approx(0.75)


def test_registry_inflight_and_cleanup():
    reg = JobRegistry()
    job = Job(video_id="a" * 11, format="m4a", quality="best", dedupe_key="k", owners={"d1"})
    reg.add(job)
    assert reg.find_inflight("k") is job and reg.active_count("d1") == 1
    job.complete("k", {})
    reg.release_inflight(job)
    assert reg.find_inflight("k") is None and reg.active_count("d1") == 0
    job.finished_at = time.time() - 100
    reg.cleanup(job_ttl=10)
    assert reg.get(job.id) is None


def test_dedupe_key_ignores_quality_for_remux_formats():
    a = dedupe_key("a" * 11, "m4a", "best", "{}", True)
    assert a == dedupe_key("a" * 11, "m4a", "standard", "{}", True)
    assert dedupe_key("a" * 11, "mp3", "best", "{}", True) != dedupe_key("a" * 11, "mp3", "standard", "{}", True)
    assert a != dedupe_key("a" * 11, "m4a", "best", overrides_json(MetadataOverrides(title="x")), True)


# -- storage ---------------------------------------------------------------------------------------
def _publish(store: LocalFileStore, key: str, size: int = 1000) -> None:
    tmp = store.new_tmp(f"t-{key}")
    src = tmp / "out.m4a"
    src.write_bytes(b"x" * size)
    store.publish(key, src, "m4a", b"jpg", {"size_bytes": size})


def test_store_publish_get_delete(tmp_path):
    store = LocalFileStore(tmp_path)
    _publish(store, "k1")
    got = store.get("k1")
    assert got and got.audio.read_bytes() == b"x" * 1000 and got.artwork.read_bytes() == b"jpg"
    assert LocalFileStore(tmp_path).get("k1") is not None  # survives a "restart"
    store.delete("k1")
    assert store.get("k1") is None and not any(store.files_dir.iterdir())


def test_store_sweep_ttl_and_lru(tmp_path):
    store = LocalFileStore(tmp_path)
    for k in ("old", "mid", "new"):
        _publish(store, k, 10_000)
    for k, age in (("old", 300), ("mid", 200), ("new", 100)):
        meta = store.get(k).meta | {"last_access": time.time() - age}
        store._write_json(store._sidecar(k), meta)
    store.sweep(ttl_seconds=250, max_bytes=10**9)
    assert store.get("old") is None and store.get("mid") is not None
    store.sweep(ttl_seconds=10**6, max_bytes=15_000)  # LRU: "mid" goes first
    assert store.get("mid") is None and store.get("new") is not None
    (store.files_dir / "orphan.m4a").write_bytes(b"x")
    store.sweep(ttl_seconds=10**6, max_bytes=10**9)
    assert not (store.files_dir / "orphan.m4a").exists()


# -- artwork / ffmpeg command ----------------------------------------------------------------------
def test_artwork_url_rules():
    assert is_allowed_url("https://lh3.googleusercontent.com/a=w60-h60")
    assert is_allowed_url("https://i.ytimg.com/vi/x/hqdefault.jpg")
    assert not is_allowed_url("http://i.ytimg.com/x.jpg")
    assert not is_allowed_url("https://evil.com/i.ytimg.com.jpg")
    assert not is_allowed_url("https://ytimg.com.evil.com/x.jpg")
    assert upscale_music_art("https://lh3.googleusercontent.com/abc=w60-h60-l90-rj", 1200).endswith("=w1200-h1200-l90-rj")
    urls = candidate_urls(VIDEO_INFO, "https://evil.com/x.jpg", 1200)
    assert urls[0].startswith("https://i.ytimg.com/") and not any("evil" in u for u in urls)


def test_square_jpeg_crops_letterbox():
    from io import BytesIO

    from PIL import Image
    buf = BytesIO()
    Image.new("RGB", (1280, 720), "red").save(buf, "PNG")
    data, side = square_jpeg(buf.getvalue(), 600)
    assert side == 600
    with Image.open(BytesIO(data)) as img:
        assert img.size == (600, 600) and img.format == "JPEG"


def test_build_command_remux_vs_transcode(tmp_path):
    argv, transcoded = build_command("ffmpeg", tmp_path / "s.m4a", tmp_path / "o.m4a", "m4a", "aac", "best", "192k")
    assert not transcoded and argv[argv.index("-c:a") + 1] == "copy" and "+faststart" in argv
    argv, transcoded = build_command("ffmpeg", tmp_path / "s.webm", tmp_path / "o.m4a", "m4a", "opus", "best", "192k")
    assert transcoded and argv[argv.index("-c:a") + 1] == "aac"
    argv, _ = build_command("ffmpeg", tmp_path / "s.webm", tmp_path / "o.mp3", "mp3", "opus", "standard", "192k")
    assert argv[argv.index("-b:a") + 1] == "192k"


def test_settings_token_parsing():
    s = Settings(app_tokens="app-v1:aaa, app-v2:bbb", _env_file=None)
    assert s.tokens == {"app-v1": "aaa", "app-v2": "bbb"}
    assert Settings(app_tokens="justsecret", _env_file=None).tokens == {"app": "justsecret"}


def test_app_error_codes():
    with pytest.raises(ValueError):
        AppError("nope")
    err = AppError("queue_full", retry_after=5)
    assert err.status == 503 and err.retryable and err.as_job_error()["code"] == "queue_full"


# -- IPv4/IPv6 fallback ----------------------------------------------------------------------------
from app.providers.ytdlp_client import YtDlpClient  # noqa: E402


def _client(tmp_path, mode="auto"):
    return YtDlpClient(make_settings(tmp_path, ytdlp_ip_version=mode), YouTubeHealth())


def test_fallback_to_ipv6_on_bot_check_and_remember(tmp_path):
    client = _client(tmp_path)
    calls = []

    def call(route):
        calls.append(route)
        if route != YtDlpClient.IPV6:
            raise AppError("upstream_blocked")
        return "ok"
    assert client._with_fallback(call) == "ok"
    assert calls == [None, "::"]
    calls.clear()
    assert client._with_fallback(call) == "ok"
    assert calls == ["::"]  # remembered
    assert client.health.status == "ok"


def test_fallback_gives_up_when_ipv6_unavailable(tmp_path):
    client = _client(tmp_path)

    def call(route):
        if route is None:
            raise AppError("upstream_blocked")
        raise AppError("upstream_error", "Network is unreachable")
    with pytest.raises(AppError) as exc:
        client._with_fallback(call)
    assert exc.value.code == "upstream_blocked" and client.health.status == "blocked"


def test_real_video_errors_are_not_retried(tmp_path):
    client = _client(tmp_path)
    calls = []

    def call(route):
        calls.append(route)
        raise AppError("video_unavailable")
    with pytest.raises(AppError) as exc:
        client._with_fallback(call)
    assert exc.value.code == "video_unavailable" and calls == [None]


def test_forced_ip_version(tmp_path):
    assert _client(tmp_path, "6")._routes() == ["::"]
    assert _client(tmp_path, "4")._routes() == ["0.0.0.0"]
    with pytest.raises(ValueError):
        make_settings(tmp_path, ytdlp_ip_version="5")


def test_filename_uses_main_artist():
    md = TrackMetadata(title="Get Lucky (feat. Pharrell Williams and Nile Rodgers)", album_artist="Daft Punk",
                       artists=["Daft Punk", "Pharrell Williams", "Nile Rodgers"])
    assert display_filename(md, "m4a") == "Daft Punk - Get Lucky (feat. Pharrell Williams and Nile Rodgers).m4a"
