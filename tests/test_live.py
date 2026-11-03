"""End-to-end against real YouTube / YouTube Music / MusicBrainz / LRCLIB. Opt-in: `pytest -m live`.

Needs network, FFmpeg and Deno. Tags are read back with TagLib, the same reader Harmoniq uses.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import HEADERS, make_settings, wait_for

pytestmark = pytest.mark.live
SONG = "4D7u5KF7SP8"   # Daft Punk – Get Lucky (album audio, Random Access Memories, track 8/13)
UPLOAD = "jNQXAC9IVRw"  # "Me at the zoo", 19 s, no album


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    app = create_app(make_settings(tmp_path_factory.mktemp("live"), log_level="INFO", yt_min_interval_seconds=1))
    with TestClient(app) as c:
        yield c


def _download(live, video_id, fmt):
    r = live.post("/v1/downloads", json={"video_id": video_id, "format": fmt}, headers=HEADERS)
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    view = {}

    def done():
        view.update(live.get(f"/v1/downloads/{job_id}", headers=HEADERS).json())
        return view["status"] in ("completed", "failed", "cancelled")
    assert wait_for(done, timeout=240), view
    assert view["status"] == "completed", view
    services = live.app.state.services
    path = services.store.get(services.registry.get(job_id).file_key).audio
    return job_id, view, path


def test_live_song_search(live):
    r = live.get("/v1/search", params={"q": "daft punk get lucky", "limit": 3}, headers=HEADERS)
    assert r.status_code == 200, r.text
    assert any(x["video_id"] == SONG for x in r.json()["results"])


def test_live_track_resolution(live):
    r = live.get(f"/v1/videos/{SONG}/track", headers=HEADERS)
    assert r.status_code == 200, r.text
    t = r.json()
    print("\ntrack:", {k: t[k] for k in ("title", "artists", "album", "track_number", "track_total", "disc_number",
                                         "date", "genres", "isrc", "sources")})
    assert t["album"] == "Random Access Memories" and t["album_artist"] == "Daft Punk"
    assert (t["track_number"], t["track_total"]) == (8, 13)
    assert t["date"] == "2013-05-17", "MusicBrainz full release date"
    assert "Electronic" in t["genres"] and t["isrc"] and t["lyrics"]["available"]


@pytest.mark.parametrize("fmt", ["opus", "m4a"])
def test_live_song_download_full_metadata(live, fmt):
    taglib = pytest.importorskip("taglib")
    job_id, view, path = _download(live, SONG, fmt)
    m, f = view["metadata"], view["file"]
    print(f"\n{fmt}: {f['filename']} {f['size_bytes']} B, {f['quality']['bitrate_kbps']} kbps "
          f"{f['quality']['sample_rate']} Hz transcoded={f['transcoded']} peak={f['quality']['max_volume_db']} dB "
          f"lyrics={m['lyrics']} art={m['artwork']} sources={m['sources']}")
    assert f["transcoded"] is False and f["quality"]["bitrate_kbps"] >= 96
    with taglib.File(str(path)) as tf:
        tags = dict(tf.tags)
    print("TagLib view:", {k: (v if k != "LYRICS" else [v[0][:60] + "…"]) for k, v in tags.items()})
    assert tags["ALBUM"] == ["Random Access Memories"] and tags["ALBUMARTIST"] == ["Daft Punk"]
    assert tags["ARTIST"][0].startswith("Daft Punk; Pharrell Williams")
    assert tags["DATE"] == ["2013-05-17"] and tags["TRACKNUMBER"][0].startswith("8")
    assert "Electronic" in tags["GENRE"][0] and tags.get("ISRC")
    assert m["artwork"]["embedded"] and m["artwork"]["width"] >= 544
    assert m["lyrics"]["available"] and tags["LYRICS"][0].startswith("[00:")
    lyrics = live.get(f"/v1/downloads/{job_id}/lyrics", headers=HEADERS)
    assert lyrics.status_code == 200 and lyrics.text == tags["LYRICS"][0]


def test_live_upload_without_album(live):
    taglib = pytest.importorskip("taglib")
    _, view, path = _download(live, UPLOAD, "mp3")
    with taglib.File(str(path)) as tf:
        tags = dict(tf.tags)
    print(f"\nupload: {view['file']['filename']} {tags}")
    assert tags["TITLE"] and tags["ARTIST"] and view["file"]["transcoded"] is True


def test_live_playlist_search_and_detail(live):
    r = live.get("/v1/search", params={"q": "lofi beats", "type": "all"}, headers=HEADERS)
    assert r.status_code == 200, r.text
    sections = r.json()["sections"]
    assert sections["songs"] and sections["videos"] and sections["playlists"], sections["errors"]
    assert all(len(sections[k]) <= 10 for k in ("songs", "videos", "playlists"))
    playlist_id = sections["playlists"][0]["playlist_id"]
    r = live.get(f"/v1/playlists/{playlist_id}", params={"limit": 20}, headers=HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tracks"] and all(len(t["video_id"]) == 11 for t in body["tracks"])
    assert len(body["tracks"]) <= 20
