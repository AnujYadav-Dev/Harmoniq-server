from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import PROJECT_ROOT, Settings
from app.domain.errors import AppError, JobCancelled
from app.main import create_app

TOKEN = "hq_test_token_0123456789"
DEVICE = "11111111-2222-3333-4444-555555555555"
HEADERS = {"X-App-Token": TOKEN, "X-Device-Id": DEVICE}


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = dict(
        app_tokens=f"app-v1:{TOKEN}",
        data_dir=tmp_path / "data",
        tools_dir=str(PROJECT_ROOT / "tools"),
        yt_min_interval_seconds=0,
        ytdlp_sleep_requests=0,
        log_level="WARNING",
        _env_file=None,
    )
    values.update(overrides)
    return Settings(**values)


MUSIC_RESULT = {
    "videoId": "FGBhQbmPwH8", "title": "One More Time", "resultType": "song",
    "artists": [{"name": "Daft Punk", "id": "UC_kRDKYrUlrbtrSiyu5Tflg"}],
    "album": {"name": "Discovery", "id": "MPREb_x"}, "duration": "5:20", "duration_seconds": 320,
    "isExplicit": False,
    "thumbnails": [{"url": "https://lh3.googleusercontent.com/abc=w60-h60-l90-rj", "width": 60, "height": 60}],
}

VIDEO_INFO = {
    "id": "FGBhQbmPwH8", "title": "Daft Punk - One More Time (Official Video)", "channel": "Daft Punk",
    "channel_id": "UC_kRDKYrUlrbtrSiyu5Tflg", "duration": 320, "upload_date": "20090226",
    "live_status": "not_live", "playable_in_embed": True, "age_limit": 0,
    "thumbnails": [{"url": "https://i.ytimg.com/vi/FGBhQbmPwH8/maxresdefault.jpg", "width": 1280, "height": 720}],
    "formats": [
        {"format_id": "251", "vcodec": "none", "acodec": "opus", "ext": "webm", "abr": 135.2, "asr": 48000,
         "filesize": 5401234},
        {"format_id": "140", "vcodec": "none", "acodec": "mp4a.40.2", "ext": "m4a", "abr": 129.5, "asr": 44100,
         "filesize": 5180000},
        {"format_id": "18", "vcodec": "avc1", "acodec": "mp4a.40.2", "ext": "mp4"},
    ],
}


PLAYLIST_SEARCH_RESULT = {
    "category": "Community playlists", "resultType": "playlist", "title": "Lofi Beats", "itemCount": None,
    "author": "Lofi Girl", "browseId": "VLPLVppO9BnnvNXywdH5MdT_nl3sYY12YmeU",
    "thumbnails": [{"url": "https://i.ytimg.com/vi/rj7MEQbSPKQ/hqdefault.jpg", "width": 400, "height": 225}],
}


def playlist_track(video_id, video_type="MUSIC_VIDEO_TYPE_ATV", **extra):
    return {"videoId": video_id, "title": f"Track {video_id}", "artists": [{"name": "Daft Punk", "id": "UC_x"}],
            "album": {"name": "Discovery", "id": "MPREb_x"}, "isAvailable": True, "isExplicit": False,
            "videoType": video_type, "duration": "3:02", "duration_seconds": 182,
            "thumbnails": [{"url": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg", "width": 400, "height": 225}],
            **extra}


# shaped like ytmusicapi 1.12 get_playlist()
PLAYLIST = {
    "owned": False, "id": "PLVppO9BnnvNXywdH5MdT_nl3sYY12YmeU", "privacy": "PUBLIC", "description": None,
    "views": 46, "duration": "28 minutes", "trackCount": 4, "title": "Lofi Beats",
    "thumbnails": [{"url": "https://i.ytimg.com/vi/rj7MEQbSPKQ/hqdefault.jpg", "width": 400, "height": 225}],
    "author": {"name": "Lofi Girl", "id": "UCn8sXUd_Fm2Ka_Z69cLIuJw"}, "year": "2026", "duration_seconds": 1700,
    "tracks": [
        playlist_track("FGBhQbmPwH8"),
        playlist_track("rj7MEQbSPKQ", "MUSIC_VIDEO_TYPE_UGC", album=None),
        playlist_track("unavailabl1", isAvailable=False),
        {**playlist_track("x"), "videoId": None},
    ],
}


class FakeMusic:
    def __init__(self):
        self.calls = 0
        self.fail = False
        self.playlist_search_fail = False
        self.playlist_calls = 0
        self.playlist_data = PLAYLIST

    def search_songs(self, query, limit):
        self.calls += 1
        if self.fail:
            raise RuntimeError("ytmusic broke")
        return [MUSIC_RESULT][:limit]

    def search_playlists(self, query, limit):
        self.calls += 1
        if self.playlist_search_fail:
            raise AppError("upstream_error", "playlists broke")
        # ytmusicapi ignores the limit; the real provider cuts the list
        return [{**PLAYLIST_SEARCH_RESULT, "title": f"Lofi {i}"} for i in range(20)][:limit]

    def playlist(self, playlist_id, limit):
        self.playlist_calls += 1
        if playlist_id == "PLmissing000000":
            raise KeyError("contents")
        return self.playlist_data


class FakeYtDlp:
    def __init__(self):
        self.extract_calls = 0
        self.search_fail = False

    def search(self, query, limit):
        if self.search_fail:
            raise AppError("upstream_blocked", "bot check")
        return [{"id": "FGBhQbmPwH8", "title": "Daft Punk - One More Time", "ie_key": "Youtube", "duration": 320,
                 "channel": "Daft Punk", "view_count": 5}, {"id": "UCabc", "ie_key": "YoutubeTab", "title": "ch"}]

    def extract(self, video_id):
        self.extract_calls += 1
        if video_id == "unavailabl1":
            raise AppError("video_unavailable", "Private video")
        return {**VIDEO_INFO, "id": video_id}


class FakePipeline:
    """Stands in for DownloadPipeline.run: writes a small fake file and publishes it."""

    def __init__(self, services, delay: float = 0.0, error: AppError | None = None):
        self.services = services
        self.delay = delay
        self.error = error
        self.runs = 0
        self.started = threading.Event()

    def run(self, job, publish=True):
        self.runs += 1
        job.set_status("resolving")
        self.started.set()
        job.set_status("downloading")
        end = time.monotonic() + self.delay
        while time.monotonic() < end:
            if job.cancel.wait(0.02):
                raise JobCancelled()
            job.set_stage_progress(0.5)
        if self.error:
            raise self.error
        tmp = self.services.store.new_tmp(job.id)
        out = tmp / f"out.{job.format}"
        out.write_bytes(b"\x00" * 50_000)
        meta = {"video_id": job.video_id, "format": job.format, "filename": "Daft Punk - One More Time.m4a",
                "mime_type": "audio/mp4", "size_bytes": 50_000, "sha256": "abc123", "duration": 320.0,
                "metadata": {"title": "One More Time", "artists": ["Daft Punk"],
                             "lyrics": {"available": True, "synced": True, "source": "test", "lines": 1}},
                "lyrics_text": "[00:01.000]One more time"}
        self.services.store.publish(job.dedupe_key, out, job.format, b"\xff\xd8jpeg", meta)
        return job.dedupe_key, meta


class FakeCatalogProvider:
    name = "catalog"
    needs_enrich = False
    calls = 0

    def enrich(self, ctx, current):
        from app.metadata.model import TrackMetadata
        FakeCatalogProvider.calls += 1
        md = TrackMetadata(title="One More Time", artists=["Daft Punk"], album_artist="Daft Punk", album="Discovery",
                           track_number=1, track_total=14, date="2001-03-12", genres=["House"],
                           artwork_url="https://lh3.googleusercontent.com/a=w1200-h1200-l90-rj")
        md.extras.update(lyrics_id="MPLYt_x", video_type="MUSIC_VIDEO_TYPE_ATV")
        return md


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def client(settings):
    app = create_app(settings)
    with TestClient(app) as c:
        services = app.state.services
        services.music = FakeMusic()
        services.ytdlp = FakeYtDlp()
        services.pipeline.run = FakePipeline(services).run
        services.resolver.providers[:] = [FakeCatalogProvider()]  # no network in API tests
        c.services = services
        yield c


def wait_for(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False
