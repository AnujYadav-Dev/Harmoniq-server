from __future__ import annotations

import pytest

from app import alerts, cookie_check
from app.alerts import BlockAlerter
from app.cookie_check import check_cookies, has_sign_in_cookie
from app.domain.errors import AppError
from tests.conftest import HEADERS, FakePipeline, make_settings, wait_for

SMTP = dict(smtp_host="smtp.example.com", smtp_user="sender@example.com", smtp_password="pw",
            alert_email_to="me@example.com")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def sent():
    return []


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def alerter(tmp_path, sent, clock):
    return BlockAlerter(make_settings(tmp_path, **SMTP), send=lambda s, subject, body: sent.append(body), clock=clock)


def test_alert_needs_three_blocks(alerter, sent):
    assert not alerter.record_blocked("one") and not alerter.record_blocked("two")
    assert alerter.record_blocked("three")
    assert wait_for(lambda: len(sent) == 1)
    assert "3 downloads" in sent[0] and "three" in sent[0]


def test_old_blocks_fall_out_of_the_window(alerter, clock):
    alerter.record_blocked("a")
    alerter.record_blocked("b")
    clock.now += BlockAlerter.WINDOW + 1
    assert not alerter.record_blocked("c")


def test_cooldown_limits_emails(alerter, clock):
    for _ in range(3):
        alerter.record_blocked("x")
    assert not alerter.record_blocked("x")
    clock.now += BlockAlerter.COOLDOWN + 1
    assert not alerter.record_blocked("x") and not alerter.record_blocked("x")  # old blocks expired: count again
    assert alerter.record_blocked("x")


def test_no_email_unless_fully_configured(tmp_path, sent):
    alerter = BlockAlerter(make_settings(tmp_path, smtp_host="smtp.example.com"),
                           send=lambda s, subject, body: sent.append(body))
    for _ in range(3):
        alerter.record_blocked("x")
    assert sent == []


def test_failed_email_is_only_logged(tmp_path, caplog):
    def broken(settings, subject, body):
        raise OSError("no route")

    alerter = BlockAlerter(make_settings(tmp_path, **SMTP), send=broken)
    for _ in range(3):
        alerter.record_blocked("x")
    assert wait_for(lambda: "Could not send the alert email" in caplog.text)


def test_send_email_uses_starttls_and_login(tmp_path, monkeypatch):
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            calls.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self, context):
            calls.append("starttls")

        def login(self, user, password):
            calls.append(("login", user, password))

        def send_message(self, message):
            calls.append(("send", message["From"], message["To"]))

    monkeypatch.setattr(alerts.smtplib, "SMTP", FakeSMTP)
    alerts.send_email(make_settings(tmp_path, **SMTP), "s", "b")
    assert calls == [("connect", "smtp.example.com", 587), "starttls", ("login", "sender@example.com", "pw"),
                     ("send", "sender@example.com", "me@example.com")]


def test_only_blocked_downloads_are_counted(client):
    client.services.jobs.alerter.THRESHOLD = 2
    seen = []
    client.services.jobs.alerter.record_blocked = seen.append

    def fail_with(code, video_id):
        client.services.pipeline.run = FakePipeline(client.services, error=AppError(code, "boom")).run
        job = client.post("/v1/downloads", json={"video_id": video_id}, headers=HEADERS).json()["job_id"]
        wait_for(lambda: client.get(f"/v1/downloads/{job}", headers=HEADERS).json()["status"] == "failed")

    fail_with("video_unavailable", "aaaaaaaaaaa")
    assert seen == []
    fail_with("upstream_blocked", "bbbbbbbbbbb")
    assert len(seen) == 1


# -- cookie check ----------------------------------------------------------------------------------
SID = ".youtube.com\tTRUE\t/\tTRUE\t1893456000\tSID\tvalue\n"
OTHER = ".youtube.com\tTRUE\t/\tTRUE\t1893456000\tPREF\tvalue\n"
HEADER = "# Netscape HTTP Cookie File\n"


def _fake_ydl(monkeypatch, warning=None, rewrite=None, error=None):
    class FakeYdl:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            if rewrite is not None:
                with open(self.options["cookiefile"], "w", encoding="utf-8") as f:
                    f.write(rewrite)
            return False

        def extract_info(self, url, download):
            if error:
                raise error
            if warning:
                self.options["logger"].warning(warning)

    monkeypatch.setattr(cookie_check.yt_dlp, "YoutubeDL", FakeYdl)


@pytest.fixture
def cookies(tmp_path):
    path = tmp_path / "cookies.txt"
    path.write_text(HEADER + OTHER + SID, encoding="utf-8")
    return path


def test_valid_cookies_pass(tmp_path, cookies, monkeypatch):
    _fake_ydl(monkeypatch)
    assert check_cookies(cookies, make_settings(tmp_path)) is None


def test_httponly_sign_in_cookie_counts(tmp_path):
    path = tmp_path / "c.txt"
    path.write_text(HEADER + "#HttpOnly_" + SID, encoding="utf-8")
    assert has_sign_in_cookie(path)


def test_file_without_sign_in_cookie_fails_before_any_request(tmp_path, monkeypatch):
    _fake_ydl(monkeypatch, error=AssertionError("must not run"))
    path = tmp_path / "c.txt"
    path.write_text(HEADER + OTHER, encoding="utf-8")
    assert "signed-in" in check_cookies(path, make_settings(tmp_path))


def test_youtube_warning_fails(tmp_path, cookies, monkeypatch):
    _fake_ydl(monkeypatch, warning="[youtube:search] The provided YouTube account cookies are no longer valid.")
    assert "no longer valid" in check_cookies(cookies, make_settings(tmp_path))


def test_dropped_sign_in_cookie_fails(tmp_path, cookies, monkeypatch):
    _fake_ydl(monkeypatch, rewrite=HEADER + OTHER)
    assert "rejected" in check_cookies(cookies, make_settings(tmp_path))


def test_network_error_fails(tmp_path, cookies, monkeypatch):
    _fake_ydl(monkeypatch, error=OSError("down"))
    assert "failed" in check_cookies(cookies, make_settings(tmp_path))
