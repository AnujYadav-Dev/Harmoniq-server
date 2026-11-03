"""Playlist search, `type=all` sections and playlist details (v0.3.0)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.v1.playlists import normalize_playlist_id
from app.domain.errors import AppError
from app.main import create_app
from app.providers import mapping
from app.providers.ytmusic_search import YTMusicSearch
from tests.conftest import HEADERS, PLAYLIST, FakeMusic, FakeYtDlp, make_settings, playlist_track

PL_ID = "PLVppO9BnnvNXywdH5MdT_nl3sYY12YmeU"


# -- playlist search -------------------------------------------------------------------------------
def test_playlist_search_shape(client):
    r = client.get("/v1/search", params={"q": "lofi", "type": "playlist", "limit": 5}, headers=HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["type"] == "playlist" and body["source"] == "youtube_music"
    assert body["results"] == []  # old clients only read `results`
    assert len(body["playlists"]) == 5
    p = body["playlists"][0]
    assert p["result_type"] == "playlist" and p["playlist_id"] == PL_ID  # "VL" stripped
    assert p["author"] == "Lofi Girl" and p["item_count"] is None
    assert p["watch_url"] == f"https://music.youtube.com/playlist?list={PL_ID}"
    assert p["thumbnails"][0]["url"].startswith("https://i.ytimg.com/")


def test_playlist_search_provider_cuts_to_limit():
    class FakeYT:
        def search(self, query, filter, limit):
            assert filter == "playlists"
            return [{"browseId": f"VLPL{i:010d}", "title": str(i)} for i in range(20)] + [{"title": "no id"}]

    music = YTMusicSearch()
    music._client = FakeYT()
    assert len(music.search_playlists("x", 10)) == 10


def test_playlist_search_is_cached(client):
    params = {"q": "lofi", "type": "playlist"}
    assert client.get("/v1/search", params=params, headers=HEADERS).headers["x-cache"] == "MISS"
    r = client.get("/v1/search", params=params, headers=HEADERS)
    assert r.headers["x-cache"] == "HIT" and r.json()["cached"] is True
    assert client.services.music.calls == 1


def test_old_search_types_unchanged(client):
    for type_ in ("song", "video"):
        body = client.get("/v1/search", params={"q": "x", "type": type_}, headers=HEADERS).json()
        assert body["type"] == type_ and body["results"] and body["playlists"] is None and body["sections"] is None


# -- type=all --------------------------------------------------------------------------------------
def test_all_returns_three_sections(client):
    r = client.get("/v1/search", params={"q": "daft punk", "type": "all"}, headers=HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["type"] == "all" and body["source"] == "mixed" and body["results"] == []
    sec = body["sections"]
    assert sec["songs"][0]["result_type"] == "song"
    assert sec["videos"][0]["result_type"] == "video"
    assert len(sec["playlists"]) == 10  # SEARCH_ALL_SECTION_LIMIT default
    assert sec["errors"] == {}
    assert r.headers["x-cache"] == "MISS"
    # sections share the single-type cache
    assert client.get("/v1/search", params={"q": "daft punk", "type": "all"},
                      headers=HEADERS).headers["x-cache"] == "HIT"
    song = client.get("/v1/search", params={"q": "daft punk", "type": "song", "limit": 10}, headers=HEADERS)
    assert song.headers["x-cache"] == "HIT"


def test_all_section_limit_is_configurable(tmp_path):
    app = create_app(make_settings(tmp_path, search_all_section_limit=3))
    with TestClient(app) as c:
        app.state.services.music = FakeMusic()
        app.state.services.ytdlp = FakeYtDlp()
        sec = c.get("/v1/search", params={"q": "x", "type": "all"}, headers=HEADERS).json()["sections"]
        assert len(sec["playlists"]) == 3
        assert c.get("/v1/meta", headers=HEADERS).json()["features"]["search_all_section_limit"] == 3


def test_all_with_one_failing_section_returns_the_others(client):
    client.services.music.playlist_search_fail = True
    r = client.get("/v1/search", params={"q": "x", "type": "all"}, headers=HEADERS)
    assert r.status_code == 200
    sec = r.json()["sections"]
    assert sec["songs"] and sec["videos"] and sec["playlists"] == []
    assert sec["errors"] == {"playlists": "upstream_error"}


def test_all_with_every_section_failing_is_an_error(client):
    client.services.music.fail = True  # songs fall back to video search…
    client.services.music.playlist_search_fail = True
    client.services.ytdlp.search_fail = True  # …which fails too, like videos
    r = client.get("/v1/search", params={"q": "x", "type": "all"}, headers=HEADERS)
    assert r.status_code == 502 and r.json()["code"] == "upstream_blocked"


def test_all_counts_once_per_device_and_three_times_globally(tmp_path):
    app = create_app(make_settings(tmp_path, rate_limit_search_per_min=2, rate_limit_global_per_min=4))
    with TestClient(app) as c:
        app.state.services.music = FakeMusic()
        app.state.services.ytdlp = FakeYtDlp()
        r = c.get("/v1/search", params={"q": "a", "type": "all"}, headers=HEADERS)
        assert r.status_code == 200 and r.headers["ratelimit-remaining"] == "1"
        # 3 of 4 global units used: one more single search fits, then the budget is spent
        assert c.get("/v1/search", params={"q": "b"}, headers=HEADERS).status_code == 200
        r = c.get("/v1/search", params={"q": "c"}, headers={**HEADERS, "X-Device-Id": "a" * 16})
        assert r.status_code == 429 and r.json()["detail"].startswith("The server")


def test_search_type_validation(client):
    r = client.get("/v1/search", params={"q": "x", "type": "album"}, headers=HEADERS)
    assert r.status_code == 400 and "playlist" in r.json()["detail"]


# -- playlist detail -------------------------------------------------------------------------------
def test_playlist_detail(client):
    r = client.get(f"/v1/playlists/VL{PL_ID}", headers=HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["playlist_id"] == PL_ID and body["title"] == "Lofi Beats" and body["author"] == "Lofi Girl"
    assert body["track_count"] == 4 and body["duration"] == 1700 and body["truncated"] is False
    tracks = body["tracks"]
    assert [t["video_id"] for t in tracks] == ["FGBhQbmPwH8", "rj7MEQbSPKQ"]  # unavailable + id-less dropped
    assert tracks[0]["result_type"] == "song" and tracks[0]["watch_url"].startswith("https://music.youtube.com/")
    assert tracks[0]["album"]["name"] == "Discovery" and tracks[0]["duration"] == 182
    assert tracks[1]["result_type"] == "video" and tracks[1]["watch_url"].startswith("https://www.youtube.com/")
    assert tracks[1]["album"] is None


def test_playlist_detail_is_cached(client):
    client.get(f"/v1/playlists/{PL_ID}", headers=HEADERS)
    client.get(f"/v1/playlists/VL{PL_ID}", headers=HEADERS)  # same playlist, other id form
    assert client.services.music.playlist_calls == 1


def test_playlist_truncation(tmp_path):
    app = create_app(make_settings(tmp_path, max_playlist_tracks=2))
    with TestClient(app) as c:
        music = FakeMusic()
        music.playlist_data = {**PLAYLIST, "trackCount": 3,
                               "tracks": [playlist_track(f"abcdefghij{i}") for i in range(3)]}
        app.state.services.music = music
        body = c.get(f"/v1/playlists/{PL_ID}", headers=HEADERS).json()
        assert len(body["tracks"]) == 2 and body["truncated"] is True
        assert c.get(f"/v1/playlists/{PL_ID}", params={"limit": 3}, headers=HEADERS).json()["code"] == "invalid_query"


def test_playlist_not_found(client):
    r = client.get("/v1/playlists/PLmissing000000", headers=HEADERS)
    assert r.status_code == 404 and r.json()["code"] == "playlist_not_found"


@pytest.mark.parametrize("bad", ["short", "PL" + "x" * 70, "PL bad chars!", "VL"])
def test_playlist_id_validation(client, bad):
    r = client.get(f"/v1/playlists/{bad}", headers=HEADERS)
    assert r.status_code == 422 and r.json()["code"] == "invalid_playlist_id"


def test_normalize_playlist_id():
    assert normalize_playlist_id(f"VL{PL_ID}") == PL_ID
    assert normalize_playlist_id(PL_ID) == PL_ID
    assert normalize_playlist_id("RDCLAK5uy_kmPRjHDECIcuVwnKsx2Ng7fyNgFKWNJFs") == "RDCLAK5uy_kmPRjHDECIcuVwnKsx2Ng7fyNgFKWNJFs"
    with pytest.raises(AppError):
        normalize_playlist_id("../etc")


def test_playlist_needs_token(client):
    assert client.get(f"/v1/playlists/{PL_ID}").status_code == 401


def test_playlist_track_mapping_edge_cases():
    assert mapping.playlist_track({"videoId": "short"}) is None
    t = mapping.playlist_track({"videoId": "FGBhQbmPwH8", "title": "x", "duration_seconds": 61})
    assert t.result_type == "video" and t.duration_text == "1:01" and t.artists is None


# -- meta ------------------------------------------------------------------------------------------
def test_meta_lists_search_types(client):
    f = client.get("/v1/meta", headers=HEADERS).json()["features"]
    assert f["search_types"] == ["song", "video", "playlist", "all"]
    assert f["max_playlist_tracks"] == 200
