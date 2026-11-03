from __future__ import annotations

from fastapi.testclient import TestClient

from app.domain.errors import AppError
from app.main import create_app
from tests.conftest import DEVICE, HEADERS, FakeMusic, FakePipeline, FakeYtDlp, make_settings, wait_for

OTHER_DEVICE = {**HEADERS, "X-Device-Id": "99999999-8888-7777-6666-555555555555"}


def _download(client, **body):
    return client.post("/v1/downloads", json={"video_id": "FGBhQbmPwH8", **body}, headers=HEADERS)


def _wait_status(client, job_id, statuses=("completed", "failed", "cancelled"), headers=HEADERS):
    seen = {}

    def done():
        seen["view"] = client.get(f"/v1/downloads/{job_id}", headers=headers).json()
        return seen["view"]["status"] in statuses
    assert wait_for(done), seen
    return seen["view"]


# -- auth / system ---------------------------------------------------------------------------------
def test_health_needs_no_token(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_requests_without_valid_token_are_rejected(client):
    r = client.get("/v1/search?q=x")
    assert r.status_code == 401
    body = r.json()
    assert r.headers["content-type"].startswith("application/problem+json")
    assert body["code"] == "unauthorized" and body["request_id"] == r.headers["x-request-id"]
    assert client.get("/v1/search?q=x", headers={"X-App-Token": "wrong"}).status_code == 401


def test_request_id_is_echoed(client):
    r = client.get("/v1/meta", headers={**HEADERS, "X-Request-ID": "my-req-1"})
    assert r.headers["x-request-id"] == "my-req-1"
    meta = r.json()
    assert meta["default_format"] == "m4a" and meta["components"]["yt_dlp"]
    assert meta["status"]["youtube"] == "ok"


def test_unknown_route_is_problem_json(client):
    r = client.get("/v1/nope", headers=HEADERS)
    assert r.status_code == 404 and r.json()["code"] == "not_found"


# -- search ----------------------------------------------------------------------------------------
def test_song_search_and_cache(client):
    r = client.get("/v1/search", params={"q": "  daft   punk  ", "limit": 5}, headers=HEADERS)
    assert r.status_code == 200 and r.headers["x-cache"] == "MISS"
    body = r.json()
    assert body["query"] == "daft punk" and body["source"] == "youtube_music"
    assert body["results"][0]["artists"][0]["name"] == "Daft Punk"
    assert "ratelimit-remaining" in r.headers
    r2 = client.get("/v1/search", params={"q": "Daft Punk", "limit": 5}, headers=HEADERS)
    assert r2.headers["x-cache"] == "HIT" and r2.json()["cached"] is True
    assert client.services.music.calls == 1


def test_song_search_falls_back_to_video_search(client):
    client.services.music.fail = True
    body = client.get("/v1/search", params={"q": "fallback"}, headers=HEADERS).json()
    assert body["source"] == "youtube" and body["type"] == "song"
    assert [r["video_id"] for r in body["results"]] == ["FGBhQbmPwH8"]  # channel entry filtered out


def test_video_search(client):
    body = client.get("/v1/search", params={"q": "x", "type": "video"}, headers=HEADERS).json()
    assert body["results"][0]["result_type"] == "video" and body["results"][0]["channel"]["name"] == "Daft Punk"


def test_search_validation(client):
    for params in ({"q": ""}, {"q": "x" * 201}, {"q": "x", "type": "album"}, {"q": "x", "limit": 0}):
        r = client.get("/v1/search", params=params, headers=HEADERS)
        assert r.status_code == 400 and r.json()["code"] == "invalid_query", params


def test_search_rate_limit(tmp_path):
    app = create_app(make_settings(tmp_path, rate_limit_search_per_min=2))
    with TestClient(app) as c:
        app.state.services.music = FakeMusic()
        codes = [c.get("/v1/search", params={"q": f"q{i}"}, headers=HEADERS).status_code for i in range(3)]
        assert codes == [200, 200, 429]
        r = c.get("/v1/search", params={"q": "again"}, headers=HEADERS)
        assert r.json()["code"] == "rate_limited" and int(r.headers["retry-after"]) > 0
        # another device has its own bucket
        assert c.get("/v1/search", params={"q": "z"}, headers=OTHER_DEVICE).status_code == 200


# -- videos ----------------------------------------------------------------------------------------
def test_video_metadata_and_preview(client):
    r = client.get("/v1/videos/FGBhQbmPwH8", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["flags"]["downloadable"] is True
    p = client.get("/v1/videos/FGBhQbmPwH8/preview", headers=HEADERS).json()
    assert p["embed_url"].startswith("https://www.youtube-nocookie.com/embed/FGBhQbmPwH8")
    assert client.services.ytdlp.extract_calls == 1  # second call served from cache


def test_video_errors(client):
    r = client.get("/v1/videos/bad", headers=HEADERS)
    assert r.status_code == 422 and r.json()["code"] == "invalid_video_id"
    r = client.get("/v1/videos/unavailabl1", headers=HEADERS)
    assert r.status_code == 422 and r.json()["code"] == "video_unavailable"


# -- downloads -------------------------------------------------------------------------------------
def test_download_happy_path(client):
    r = _download(client, format="m4a")
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    assert r.headers["location"] == f"/v1/downloads/{job_id}" and r.json()["cache_hit"] is False

    view = _wait_status(client, job_id)
    assert view["status"] == "completed" and view["progress"] == 1.0
    assert view["file"]["size_bytes"] == 50_000 and view["file"]["expires_at"]
    assert view["metadata"]["artists"] == ["Daft Punk"]

    f = client.get(f"/v1/downloads/{job_id}/file", headers=HEADERS)
    assert f.status_code == 200 and len(f.content) == 50_000
    assert f.headers["content-type"] == "audio/mp4" and f.headers["x-content-sha256"] == "abc123"
    assert "attachment" in f.headers["content-disposition"]

    part = client.get(f"/v1/downloads/{job_id}/file", headers={**HEADERS, "Range": "bytes=100-199"})
    assert part.status_code == 206 and len(part.content) == 100

    art = client.get(f"/v1/downloads/{job_id}/artwork", headers=HEADERS)
    assert art.status_code == 200 and art.headers["content-type"] == "image/jpeg"

    assert client.delete(f"/v1/downloads/{job_id}", headers=HEADERS).status_code == 204
    assert client.get(f"/v1/downloads/{job_id}", headers=HEADERS).json()["code"] == "job_not_found"
    assert not any(client.services.store.files_dir.iterdir())  # file released


def test_second_request_is_a_cache_hit(client):
    first = _download(client).json()["job_id"]
    _wait_status(client, first)
    r = _download(client)
    assert r.json()["cache_hit"] is True and r.json()["status"] == "completed"
    assert client.services.pipeline.run.__self__.runs == 1


def test_download_validation(client):
    assert _download(client, video_id="nope").json()["code"] == "invalid_video_id"
    assert _download(client, format="flac").json()["code"] == "invalid_format"
    assert _download(client, quality="ultra").json()["code"] == "invalid_format"
    r = _download(client, metadata={"artwork_url": "https://evil.example/x.jpg"})
    assert r.status_code == 422 and r.json()["code"] == "invalid_request"
    r = client.post("/v1/downloads", json={"format": "m4a"}, headers=HEADERS)
    assert r.status_code == 422 and r.json()["code"] == "invalid_request"


def test_jobs_are_private_to_their_device(client):
    job_id = _download(client).json()["job_id"]
    r = client.get(f"/v1/downloads/{job_id}", headers=OTHER_DEVICE)
    assert r.status_code == 403 and r.json()["code"] == "forbidden"


def test_file_before_completion_is_409_and_cancel(client):
    fake = FakePipeline(client.services, delay=10)
    client.services.pipeline.run = fake.run
    job_id = _download(client).json()["job_id"]
    assert fake.started.wait(5)
    r = client.get(f"/v1/downloads/{job_id}/file", headers=HEADERS)
    assert r.status_code == 409 and r.json()["code"] == "job_not_ready"
    assert client.delete(f"/v1/downloads/{job_id}", headers=HEADERS).json()["status"] == "cancelled"
    assert _wait_status(client, job_id)["status"] == "cancelled"


def test_identical_inflight_jobs_are_coalesced(client):
    fake = FakePipeline(client.services, delay=0.5)
    client.services.pipeline.run = fake.run
    a = _download(client).json()["job_id"]
    b = client.post("/v1/downloads", json={"video_id": "FGBhQbmPwH8"}, headers=OTHER_DEVICE).json()["job_id"]
    assert a == b
    _wait_status(client, a)
    assert _wait_status(client, a, headers=OTHER_DEVICE)["status"] == "completed"
    assert fake.runs == 1


def test_idempotency_key(client):
    h = {**HEADERS, "Idempotency-Key": "tap-1"}
    a = client.post("/v1/downloads", json={"video_id": "FGBhQbmPwH8", "format": "mp3"}, headers=h).json()
    b = client.post("/v1/downloads", json={"video_id": "FGBhQbmPwH8", "format": "mp3"}, headers=h).json()
    assert a["job_id"] == b["job_id"]


def test_failed_job_reports_error(client):
    client.services.pipeline.run = FakePipeline(client.services, error=AppError("video_unavailable", "Private")).run
    view = _wait_status(client, _download(client).json()["job_id"])
    assert view["status"] == "failed" and view["error"]["code"] == "video_unavailable"
    assert view["error"]["retryable"] is False and view["attempts"] == 1


def test_active_job_limit(tmp_path):
    app = create_app(make_settings(tmp_path, max_active_jobs_per_device=1))
    with TestClient(app) as c:
        services = app.state.services
        services.ytdlp = FakeYtDlp()
        services.pipeline.run = FakePipeline(services, delay=5).run
        assert c.post("/v1/downloads", json={"video_id": "FGBhQbmPwH8"}, headers=HEADERS).status_code == 202
        r = c.post("/v1/downloads", json={"video_id": "AAAAAAAAAAA"}, headers=HEADERS)
        assert r.status_code == 429 and r.json()["code"] == "too_many_active_jobs"
        for job in services.registry.active_jobs():
            services.jobs.cancel(job)


def test_queue_full(tmp_path):
    app = create_app(make_settings(tmp_path, max_concurrent_downloads=1, queue_max_depth=1,
                                   max_active_jobs_per_device=10))
    with TestClient(app) as c:
        services = app.state.services
        fake = FakePipeline(services, delay=5)
        services.pipeline.run = fake.run
        ids = ["AAAAAAAAAA1", "AAAAAAAAAA2", "AAAAAAAAAA3"]
        codes = []
        for vid in ids:
            codes.append(c.post("/v1/downloads", json={"video_id": vid}, headers=HEADERS).status_code)
            if vid == ids[0]:
                assert fake.started.wait(5)  # first one is running, so it has left the queue
        assert codes == [202, 202, 503]
        for job in services.registry.active_jobs():
            services.jobs.cancel(job)


def test_sse_stream_ends_with_terminal_event(client):
    job_id = _download(client).json()["job_id"]
    with client.stream("GET", f"/v1/downloads/{job_id}/events", headers=HEADERS) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        text = "".join(r.iter_text())
    assert "event: completed" in text


def test_device_fallback_to_ip(client):
    r = client.post("/v1/downloads", json={"video_id": "FGBhQbmPwH8"}, headers={"X-App-Token": HEADERS["X-App-Token"]})
    job_id = r.json()["job_id"]
    assert client.services.registry.get(job_id).owners == {"ip:testclient"}
    assert DEVICE not in str(client.services.registry.get(job_id).owners)


def test_polling_does_not_hit_global_limit(tmp_path):
    app = create_app(make_settings(tmp_path, rate_limit_global_per_min=3))
    with TestClient(app) as c:
        services = app.state.services
        services.ytdlp = FakeYtDlp()
        services.pipeline.run = FakePipeline(services).run
        job_id = c.post("/v1/downloads", json={"video_id": "FGBhQbmPwH8"}, headers=HEADERS).json()["job_id"]
        codes = {c.get(f"/v1/downloads/{job_id}", headers=HEADERS).status_code for _ in range(20)}
        assert codes == {200}
        services.music = FakeMusic()
        codes = [c.get("/v1/search", params={"q": f"g{i}"}, headers=HEADERS).status_code for i in range(3)]
        assert codes == [200, 200, 429]  # 1 download + 2 searches = 3


# -- metadata endpoints ----------------------------------------------------------------------------
def test_track_endpoint_resolves_metadata(client):
    r = client.get("/v1/videos/FGBhQbmPwH8/track", headers=HEADERS)
    assert r.status_code == 200, r.text
    t = r.json()
    assert (t["album"], t["track_number"], t["track_total"], t["year"]) == ("Discovery", 1, 14, 2001)
    assert t["genres"] == ["House"] and t["lyrics"]["available"] is True
    assert t["sources"]["album"] == "catalog" and t["video_type"] == "MUSIC_VIDEO_TYPE_ATV"
    assert t["suggested_filename"]["opus"] == "Daft Punk - One More Time.opus"
    from tests.conftest import FakeCatalogProvider
    calls = FakeCatalogProvider.calls
    client.get("/v1/videos/FGBhQbmPwH8/track", headers=HEADERS)
    assert FakeCatalogProvider.calls == calls  # cached
    assert client.get("/v1/videos/bad/track", headers=HEADERS).json()["code"] == "invalid_video_id"


def test_lyrics_endpoint(client):
    job_id = _download(client).json()["job_id"]
    view = _wait_status(client, job_id)
    assert view["links"]["lyrics"] == f"/v1/downloads/{job_id}/lyrics"
    r = client.get(f"/v1/downloads/{job_id}/lyrics", headers=HEADERS)
    assert r.status_code == 200 and r.text == "[00:01.000]One more time"
    assert r.headers["x-lyrics-synced"] == "true" and ".lrc" in r.headers["content-disposition"]


def test_new_download_options_change_the_cache_key(client):
    a = _download(client).json()["job_id"]
    _wait_status(client, a)
    b = _download(client, embed_lyrics=False).json()
    assert b["cache_hit"] is False and b["job_id"] != a
    r = _download(client, metadata={"artists": ["A", "B"], "date": "2001-03", "genres": ["House"], "disc_number": 1})
    assert r.status_code == 202
    assert _download(client, metadata={"date": "March 2001"}).json()["code"] == "invalid_request"


def test_meta_lists_features(client):
    f = client.get("/v1/meta", headers=HEADERS).json()["features"]
    assert f["metadata_providers"] == ["catalog"] and "filename_template" in f
