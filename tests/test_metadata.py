"""Metadata layer: text matching, merge rules, providers (with recorded-shape fakes) and the resolver."""

from __future__ import annotations

import types

import pytest

from app.domain.models import MetadataOverrides
from app.metadata.model import MetadataContext, TrackMetadata
from app.metadata.providers.lyrics import LyricsProvider, normalize_lrc
from app.metadata.providers.musicbrainz import MusicBrainzProvider
from app.metadata.providers.ytmusic import YTMusicProvider, parse_length
from app.metadata.resolver import MetadataResolver
from app.metadata.text import genre_case, is_non_original, normalize_date, title_key, titles_match

# -- text ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("a, b, same", [
    ("Get Lucky (feat. Pharrell Williams and Nile Rodgers)", "Get Lucky", True),
    ("Get Lucky (Official Audio) (feat. Pharrell Williams & Nile Rodgers)", "Get Lucky", True),
    ("Yellow - Remastered 2011", "Yellow", True),
    ("Café del Mar", "Cafe Del Mar", True),
    ("Get Lucky (Radio Edit)", "Get Lucky", False),
    ("Yellow (Live)", "Yellow", False),
    ("Get Lucky (Daft Punk Remix)", "Get Lucky", False),
])
def test_titles_match(a, b, same):
    assert titles_match(a, b) is same


def test_title_key_and_versions():
    assert title_key("Song ft. Someone") == "song"
    assert is_non_original("Yellow (Karaoke Version)", "Yellow")
    assert not is_non_original("Instrumental Song", "Instrumental Song")  # the wanted title says so itself
    assert genre_case("alternative rock") == "Alternative Rock"
    assert genre_case("drum and bass") == "Drum and Bass" and genre_case("R&B") == "R&B"
    assert normalize_date("20130517") == "2013-05-17" and normalize_date(2013) == "2013"
    assert normalize_date("May 2013") is None
    assert parse_length("6:10") == 370 and parse_length("1:02:03") == 3723 and parse_length(None) is None


# -- merge rules ---------------------------------------------------------------------------------------


def test_fill_only_empty_fields_but_upgrades_date():
    md = TrackMetadata(title="A", date="2013")
    md.fill(TrackMetadata(title="B", album="X", date="2013-05-17"), "mb")
    assert md.title == "A" and md.album == "X" and md.date == "2013-05-17"
    assert md.sources == {"album": "mb", "date": "mb"}
    md.fill(TrackMetadata(date="2014-01-01"), "other")  # different year never replaces
    assert md.date == "2013-05-17"


def test_forced_fields_override():
    md = TrackMetadata(track_number=13, track_total=20)
    other = TrackMetadata(track_number=3, track_total=10, disc_number=2)
    other.extras["force"] = ("track_number", "track_total")
    md.fill(other, "musicbrainz")
    assert (md.track_number, md.track_total, md.disc_number) == (3, 10, 2) and "force" not in md.extras


def test_public_json_hides_lyrics_text():
    md = TrackMetadata(title="T", date="2001-02-03", lyrics="[00:01.000]a\n[00:02.000]b", lyrics_synced=True,
                       lyrics_source="x")
    data = md.to_public()
    assert data["year"] == 2001 and data["lyrics"] == {"available": True, "synced": True, "source": "x", "lines": 2}
    assert "extras" not in data


# -- YouTube Music provider ----------------------------------------------------------------------------

RAM_ALBUM = {
    "title": "Random Access Memories", "type": "Album", "year": "2013", "trackCount": 13,
    "artists": [{"name": "Daft Punk", "id": "UC1"}],
    "thumbnails": [{"url": "https://lh3.googleusercontent.com/x=w60-h60-l90-rj", "width": 60},
                   {"url": "https://lh3.googleusercontent.com/x=w544-h544-l90-rj", "width": 544}],
    "tracks": [  # the album lists the *music video* ids, not the audio id the user picked
        {"videoId": "0Gkhol2Q1og", "title": "Touch (feat. Paul Williams)", "trackNumber": 7, "duration_seconds": 499},
        {"videoId": "5NV6Rdv1a3I", "title": "Get Lucky (feat. Pharrell Williams and Nile Rodgers)", "trackNumber": 8,
         "duration_seconds": 370, "isExplicit": False, "artists": [{"name": "Daft Punk"}]},
    ],
}


class FakeMusic:
    def __init__(self, tracks: dict, albums: dict | None = None, search: list | None = None, lyrics=None):
        self.tracks, self.albums, self.search, self._lyrics = tracks, albums or {}, search or [], lyrics
        self.album_calls = 0

    def watch_track(self, video_id):
        return self.tracks.get(video_id, (None, None))

    def album(self, browse_id):
        self.album_calls += 1
        return self.albums[browse_id]

    def search_songs(self, query, limit):
        return self.search

    def lyrics(self, browse_id):
        return self._lyrics


def test_ytmusic_album_track_matched_by_title_and_duration():
    music = FakeMusic({"4D7u5KF7SP8": ({
        "videoId": "4D7u5KF7SP8", "title": "Get Lucky (feat. Pharrell Williams and Nile Rodgers)", "length": "6:10",
        "videoType": "MUSIC_VIDEO_TYPE_ATV", "album": {"name": "Random Access Memories", "id": "MPREb_RAM"},
        "year": "2013", "artists": [{"name": "Daft Punk"}, {"name": "Pharrell Williams"}, {"name": "Nile Rodgers"}],
    }, "MPLYt_x")}, {"MPREb_RAM": RAM_ALBUM})
    provider = YTMusicProvider(music)
    md = provider.enrich(MetadataContext(video_id="4D7u5KF7SP8"), TrackMetadata())
    assert md.artists == ["Daft Punk", "Pharrell Williams", "Nile Rodgers"] and md.album_artist == "Daft Punk"
    assert (md.track_number, md.track_total, md.date, md.duration) == (8, 13, "2013", 370)
    assert md.artwork_url.endswith("=w1200-h1200-l90-rj") and md.explicit is False
    assert md.extras["lyrics_id"] == "MPLYt_x"
    provider.enrich(MetadataContext(video_id="4D7u5KF7SP8"), TrackMetadata())
    assert music.album_calls == 1  # album cached


def test_ytmusic_music_video_borrows_album_from_catalogue():
    music = FakeMusic(
        {"5NV6Rdv1a3I": ({"videoId": "5NV6Rdv1a3I", "length": "4:09", "videoType": "MUSIC_VIDEO_TYPE_OMV",
                          "title": "Daft Punk - Get Lucky (Official Audio) (feat. Pharrell Williams & Nile Rodgers)",
                          "album": None, "artists": [{"name": "Daft Punk"}]}, None),
         "4D7u5KF7SP8": ({"videoId": "4D7u5KF7SP8"}, "MPLYt_cat")},
        {"MPREb_RAM": {**RAM_ALBUM, "tracks": []}},
        search=[
            {"videoId": "kara0000000", "title": "Get Lucky (Karaoke Version)", "duration_seconds": 250,
             "artists": [{"name": "Daft Punk"}], "album": {"name": "Karaoke Hits", "id": "MPREb_K"}},
            {"videoId": "4D7u5KF7SP8", "title": "Get Lucky (feat. Pharrell Williams and Nile Rodgers)",
             "duration_seconds": 250, "artists": [{"name": "Daft Punk"}],
             "album": {"name": "Random Access Memories", "id": "MPREb_RAM"}},
        ])
    md = YTMusicProvider(music).enrich(MetadataContext(video_id="5NV6Rdv1a3I"), TrackMetadata())
    assert md.title == "Get Lucky (feat. Pharrell Williams & Nile Rodgers)" and md.artists == ["Daft Punk"]
    assert md.album == "Random Access Memories" and md.extras["catalog_video_id"] == "4D7u5KF7SP8"
    assert md.extras["lyrics_id"] == "MPLYt_cat"


def test_ytmusic_upload_takes_artist_from_title():
    music = FakeMusic({"uploader001": ({"videoId": "uploader001", "title": "Some Artist - Great Song (Lyrics)",
                                        "videoType": "MUSIC_VIDEO_TYPE_UGC", "artists": [{"name": "Lyrics Channel"}]},
                                       None)})
    md = YTMusicProvider(music).enrich(MetadataContext(video_id="uploader001"), TrackMetadata())
    assert (md.title, md.artists, md.album) == ("Great Song", ["Some Artist"], None)


# -- MusicBrainz provider ------------------------------------------------------------------------------


class FakeHttp:
    def __init__(self, responses: dict):
        self.responses, self.calls = responses, []

    def pacer(self, host, interval):
        return types.SimpleNamespace(wait=lambda: None)

    def get_json_status(self, url, params=None, *, pace=None, retries=1, timeout=None):
        self.calls.append(url)
        for key, value in self.responses.items():
            if url.endswith(key):
                return 200, value
        return 404, None

    def get_json(self, url, params=None, *, pace=None, retries=1, timeout=None):
        return self.get_json_status(url, params)[1]


def _mb_responses(media):
    return {
        "/release-group": {"release-groups": [
            {"id": "rg-drumless", "score": 100, "title": "Random Access Memories (Drumless Edition)",
             "primary-type": "Album", "secondary-types": ["Remix"], "artist-credit": [{"name": "Daft Punk"}]},
            {"id": "rg-interview", "score": 95, "title": "Random Access Memories", "primary-type": "Album",
             "secondary-types": ["Interview"], "artist-credit": [{"name": "Daft Punk"}]},
            {"id": "rg-ram", "score": 90, "title": "Random Access Memories", "primary-type": "Album",
             "first-release-date": "2013-05-17", "artist-credit": [{"name": "Daft Punk"}]},
        ]},
        "/release-group/rg-ram": {"first-release-date": "2013-05-17", "genres": [
            {"name": "disco", "count": 13}, {"name": "electronic", "count": 15}, {"name": "funk", "count": 9},
            {"name": "electro", "count": 4}, {"name": "club", "count": 1}],
            "releases": [{"id": "rel-late", "status": "Official", "date": "2023-11-17"},
                         {"id": "rel-bootleg", "status": "Bootleg", "date": "2012-01-01"},
                         {"id": "rel-first", "status": "Official", "date": "2013-05-17"}]},
        "/release/rel-first": {"id": "rel-first", "media": media},
    }


def _ram_media(discs=1):
    tracks = [{"position": 8, "title": "Get Lucky", "length": 367000,
               "recording": {"id": "rec-1", "isrcs": ["USQX91300108"]}}]
    return [{"position": d, "track-count": 13, "tracks": tracks if d == discs else []} for d in range(1, discs + 1)]


def test_musicbrainz_album_level_match():
    http = FakeHttp(_mb_responses(_ram_media()))
    provider = MusicBrainzProvider(http)
    current = TrackMetadata(title="Get Lucky (feat. Pharrell Williams and Nile Rodgers)", artists=["Daft Punk"],
                            album="Random Access Memories", duration=370)
    md = provider.enrich(MetadataContext(video_id="4D7u5KF7SP8"), current)
    assert md.date == "2013-05-17" and md.genres == ["Electronic", "Disco", "Funk"]
    assert (md.disc_number, md.disc_total, md.track_number, md.track_total) == (1, 1, 8, 13)
    assert md.isrc == "USQX91300108"
    assert md.musicbrainz == {"release_group_id": "rg-ram", "release_id": "rel-first", "recording_id": "rec-1"}
    assert md.extras["force"] == ("date",)  # first release date beats the date YouTube shows
    calls = len(http.calls)
    provider.enrich(MetadataContext(video_id="x"), current)
    assert len(http.calls) == calls  # cached per album


def test_musicbrainz_multi_disc_forces_per_disc_numbering():
    md = MusicBrainzProvider(FakeHttp(_mb_responses(_ram_media(discs=2)))).enrich(
        MetadataContext(video_id="v"),
        TrackMetadata(title="Get Lucky", artists=["Daft Punk"], album="Random Access Memories"))
    assert (md.disc_number, md.disc_total) == (2, 2) and md.extras["force"] == ("date", "track_number", "track_total")


def test_musicbrainz_no_match_returns_nothing():
    http = FakeHttp({"/release-group": {"release-groups": [
        {"id": "x", "score": 100, "title": "Another Album", "primary-type": "Album",
         "artist-credit": [{"name": "Daft Punk"}]}]}})
    assert MusicBrainzProvider(http).enrich(MetadataContext(video_id="v"), TrackMetadata(
        title="Get Lucky", artists=["Daft Punk"], album="Random Access Memories")) is None
    assert MusicBrainzProvider(http).enrich(MetadataContext(video_id="v"), TrackMetadata(title="x")) is None


# -- lyrics --------------------------------------------------------------------------------------------


def test_normalize_lrc_for_harmoniq():
    raw = "[ar:Daft Punk]\n[ti:Get Lucky]\n[length:06:10]\n\n[00:12.34]Paper lanterns\n[01:05.5][02:10.00]Chorus\n"
    assert normalize_lrc(raw) == "[00:12.340]Paper lanterns\n[01:05.500]Chorus\n[02:10.000]Chorus"


def test_lyrics_prefers_synced_ytmusic():
    line = lambda t, ms: types.SimpleNamespace(text=t, start_time=ms)  # noqa: E731
    music = FakeMusic({}, lyrics={"hasTimestamps": True, "source": "Source: LyricFind",
                                  "lyrics": [line("♪", 0), line("Paper lanterns", 12340)]})
    md = LyricsProvider(music, FakeHttp({}), ["ytmusic", "lrclib"]).enrich(
        MetadataContext(video_id="v"), TrackMetadata(title="T", artists=["A"], extras={"lyrics_id": "MPLYt"}))
    assert md.lyrics == "[00:00.000]♪\n[00:12.340]Paper lanterns"
    assert md.lyrics_synced and md.lyrics_source == "YouTube Music (LyricFind)"


def test_lyrics_falls_back_to_lrclib_synced_then_plain():
    music = FakeMusic({}, lyrics={"hasTimestamps": False, "lyrics": "Plain line one\nPlain line two\n"})
    current = TrackMetadata(title="T", artists=["A"], album="B", duration=200, extras={"lyrics_id": "MPLYt"})
    synced = FakeHttp({"/api/get": {"syncedLyrics": "[00:01.00]Synced", "plainLyrics": "Synced"}})
    md = LyricsProvider(music, synced, ["ytmusic", "lrclib"]).enrich(MetadataContext(video_id="v"), current)
    assert (md.lyrics, md.lyrics_synced, md.lyrics_source) == ("[00:01.000]Synced", True, "LRCLIB")

    md = LyricsProvider(music, FakeHttp({}), ["ytmusic", "lrclib"]).enrich(MetadataContext(video_id="v"), current)
    assert (md.lyrics, md.lyrics_synced) == ("Plain line one\nPlain line two", False)
    assert LyricsProvider(music, FakeHttp({}), []).enrich(MetadataContext(video_id="v"), current) is None
    no_fetch = MetadataContext(video_id="v", fetch_lyrics=False)
    assert LyricsProvider(music, synced, ["lrclib"]).enrich(no_fetch, current) is None


# -- resolver ------------------------------------------------------------------------------------------


class _P:
    def __init__(self, name, result=None, error=None, needs_enrich=False):
        self.name, self.result, self.error, self.needs_enrich = name, result, error, needs_enrich

    def enrich(self, ctx, current):
        if self.error:
            raise self.error
        return self.result


def test_resolver_order_overrides_and_isolation():
    resolver = MetadataResolver([
        _P("broken", error=RuntimeError("boom")),
        _P("catalog", TrackMetadata(title="Catalog", artists=["X"], album="Alb")),
        _P("mb", TrackMetadata(genres=["Rock"], date="2001-01-01"), needs_enrich=True),
    ])
    ctx = MetadataContext(video_id="v", overrides=MetadataOverrides(title="Mine", year=2001))
    md = resolver.resolve(ctx)
    assert md.title == "Mine" and md.album == "Alb" and md.genres == ["Rock"] and md.date == "2001-01-01"
    assert md.sources["title"] == "request" and md.sources["album"] == "catalog" and md.sources["date"] == "mb"
    assert ctx.warnings == ["broken: RuntimeError"]

    md = resolver.resolve(MetadataContext(video_id="v", enrich=False))
    assert md.genres == [] and md.title == "Catalog"
