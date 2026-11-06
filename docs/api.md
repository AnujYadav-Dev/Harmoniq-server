# API reference (v1)

[← Docs index](README.md)

Base URL: `https://<your-host>/` · All JSON is UTF-8 · Timestamps are RFC 3339 UTC · Durations are in **seconds**.
Once the server is implemented, FastAPI serves the live, machine-readable contract at `/openapi.json`. If this
document and that schema disagree, the schema wins, and this document should be fixed.

> Examples use real-looking but **illustrative** values (IDs, channel IDs, sizes, hashes). Don't use them as
> test fixtures.

| Method | Path | Purpose | Sync? |
|--------|------|---------|-------|
| `GET` | [`/v1/search`](#get-v1search) | Search songs, videos, playlists, or all three | Sync |
| `GET` | [`/v1/playlists/{playlist_id}`](#get-v1playlistsplaylist_id) | Playlist info and its tracks | Sync |
| `GET` | [`/v1/videos/{video_id}`](#get-v1videosvideo_id) | Full metadata for one video | Sync |
| `GET` | [`/v1/videos/{video_id}/preview`](#get-v1videosvideo_idpreview) | Embed/preview information | Sync |
| `GET` | [`/v1/videos/{video_id}/track`](#get-v1videosvideo_idtrack) | Resolved song metadata ("Find Music") | Sync |
| `POST` | [`/v1/downloads`](#post-v1downloads) | Start an audio download job | Async (202) |
| `GET` | [`/v1/downloads/{job_id}`](#get-v1downloadsjob_id) | Job status, progress, metadata | Sync |
| `GET` | [`/v1/downloads/{job_id}/events`](#get-v1downloadsjob_idevents) | Progress as Server-Sent Events | Stream |
| `GET` | [`/v1/downloads/{job_id}/file`](#get-v1downloadsjob_idfile) | Download the finished audio | Stream |
| `GET` | [`/v1/downloads/{job_id}/artwork`](#get-v1downloadsjob_idartwork) | Square cover art (JPEG) | Stream |
| `GET` | [`/v1/downloads/{job_id}/lyrics`](#get-v1downloadsjob_idlyrics) | Lyrics as LRC or plain text | Sync |
| `DELETE` | [`/v1/downloads/{job_id}`](#delete-v1downloadsjob_id) | Cancel a job / release its file | Sync |
| `GET` | [`/v1/meta`](#get-v1meta) | Server capabilities and versions | Sync |
| `GET` | [`/healthz`](#health), [`/readyz`](#health) | Liveness / readiness | Sync |

---

## Conventions

### Authentication

Every `/v1/*` request must carry the **app token** that is built into the Harmoniq APK, plus a per-install
device ID:

```http
X-App-Token: hq_3f9c…            # secret from the server's APP_TOKENS
X-Device-Id: 6f1c2d4e-…          # random UUID the app generates once per install
```

- Missing or wrong `X-App-Token` → `401 unauthorized`. Tokens are compared in constant time.
- `X-Device-Id` (UUID) isn't a secret. It's used for rate limits, job ownership and logs, so testers don't share
  one bucket and can't see each other's jobs. If it's missing, the client IP is used instead.
- `/healthz` and `/readyz` need no token.
- The check is a single FastAPI dependency, so it can later be swapped for per-user login (JWT) without changing
  any route.

### Request IDs

Clients may send `X-Request-ID` (≤ 64 chars). Otherwise the server generates one. It is echoed in the response
header and included in every error body and log line. Include it in bug reports.

### Rate-limit headers

Responses to rate-limited routes include:

```http
RateLimit-Limit: 30
RateLimit-Remaining: 27
RateLimit-Reset: 41
```

On `429`, `Retry-After` (seconds) is also set.

### Video IDs

A `video_id` is exactly 11 characters from `[A-Za-z0-9_-]`. Anything else → `422 invalid_video_id`. The server
**never** accepts arbitrary URLs. That rules out SSRF and stops other yt-dlp extractors from being reached.

### Errors

All errors use [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) `application/problem+json`, with a stable
machine-readable `code`:

```json
{
  "type": "https://harmoniq.dev/errors/video_unavailable",
  "title": "Video unavailable",
  "status": 422,
  "code": "video_unavailable",
  "detail": "This video is private or has been removed.",
  "request_id": "01JBX6T8V8H3K2Q9ZP4N7M5C1D",
  "retryable": false
}
```

| HTTP | `code` | Meaning | `retryable` | Suggested app behaviour |
|------|--------|---------|-------------|-------------------------|
| 400 | `invalid_query` | Empty or > 200-char query | no | Show validation message |
| 401 | `unauthorized` | Missing/invalid app token | no | "This app version is no longer supported" (token rotated): install the latest APK |
| 403 | `forbidden` | Job belongs to another device | no | — |
| 404 | `not_found` | No such endpoint | no | — |
| 404 | `artwork_not_available` | The job finished without cover art | no | Use the search thumbnail |
| 404 | `lyrics_not_available` | No lyrics were found for this song | no | Hide the lyrics option |
| 404 | `job_not_found` | Unknown or expired job ID, or the server restarted (job records are in memory) | no | Submit the download again (instant if the file was already finished) |
| 404 | `video_not_found` | ID doesn't exist | no | Remove from list |
| 404 | `playlist_not_found` | Private, deleted, or a playlist YouTube Music can't open (e.g. some `RD…` mixes) | no | Offer "Open in YouTube" |
| 409 | `job_not_ready` | `/file` requested before `completed` | yes | Keep polling |
| 410 | `job_expired` | File already evicted (TTL) | no | `POST` again (often a cache-cold re-download) |
| 422 | `invalid_video_id` | Malformed ID | no | — |
| 422 | `invalid_playlist_id` | Malformed playlist ID | no | — |
| 422 | `invalid_format` | Unsupported `format`/`quality` | no | — |
| 422 | `invalid_request` | Malformed JSON body, missing field, disallowed `artwork_url` | no | Fix the request |
| 422 | `video_unavailable` | Private, removed, region-blocked, premium-only | no | Show reason |
| 422 | `age_restricted` | Needs sign-in and the server has no cookies | no | Show reason |
| 422 | `live_not_supported` | Live or upcoming stream | no | Show reason |
| 422 | `duration_limit_exceeded` | Longer than `MAX_DURATION_SECONDS` | no | Show limit |
| 429 | `rate_limited` | Per-device or global limit hit | yes | Wait `Retry-After` |
| 429 | `too_many_active_jobs` | `MAX_ACTIVE_JOBS_PER_DEVICE` reached | yes | Wait for one to finish |
| 502 | `upstream_blocked` | YouTube bot check / IP flagged | yes (later) | "Server temporarily blocked by YouTube" |
| 502 | `upstream_error` | Unexpected YouTube response / extractor broken | yes | Retry later |
| 502 | `processing_failed` | FFmpeg/tagging/verification failed | yes | Retry once |
| 503 | `queue_full` | Back-pressure | yes | Wait `Retry-After` |
| 503 | `service_unavailable` | Data folder not writable / disk full / tools missing | yes | Retry later |
| 504 | `upstream_timeout` | Search/metadata took too long | yes | Retry |
| 500 | `internal_error` | Unexpected server bug (logged) | yes | Retry; report the `request_id` |

For async jobs, errors don't come back as HTTP errors from the status endpoint. They appear inside the job
(`status: "failed"`, `error: { code, detail, retryable }`) using the same codes.

---

## GET /v1/search

Search YouTube Music songs, YouTube videos, YouTube Music playlists, or all three at once.

| Query param | Type | Default | Notes |
|-------------|------|---------|-------|
| `q` | string | **required** | 1–200 chars |
| `type` | `song` \| `video` \| `playlist` \| `all` | `song` | `song` → YouTube Music catalogue (ytmusicapi); `video` → YouTube (yt-dlp); `playlist` and `all` since v0.3.0 (check `features.search_types` in [`/v1/meta`](#get-v1meta)) |
| `limit` | int | `20` (`all`: `SEARCH_ALL_SECTION_LIMIT`, 10) | 1–50; for `all`, per section |

Pagination is intentionally left out of v1. Both upstreams paginate with continuation tokens that expire quickly.
A `cursor` param is reserved for later.

**Example**

```bash
curl -s "https://harmoniq.example.com/v1/search?q=daft%20punk%20one%20more%20time&type=song&limit=2" \
  -H "X-App-Token: $HQ_TOKEN" -H "X-Device-Id: $(uuidgen)"
```

```json
{
  "query": "daft punk one more time",
  "type": "song",
  "source": "youtube_music",
  "cached": false,
  "results": [
    {
      "video_id": "FGBhQbmPwH8",
      "result_type": "song",
      "title": "One More Time",
      "artists": [{ "name": "Daft Punk", "channel_id": "UC_kRDKYrUlrbtrSiyu5Tflg" }],
      "album": { "name": "Discovery", "browse_id": "MPREb_..." },
      "duration": 320,
      "duration_text": "5:20",
      "is_explicit": false,
      "thumbnails": [
        { "url": "https://lh3.googleusercontent.com/...=w120-h120", "width": 120, "height": 120 },
        { "url": "https://lh3.googleusercontent.com/...=w544-h544", "width": 544, "height": 544 }
      ],
      "channel": null,
      "view_count": null,
      "watch_url": "https://music.youtube.com/watch?v=FGBhQbmPwH8"
    }
  ]
}
```

For `type=video`, `artists`/`album` are `null` or best-effort, and `channel` (`{name, id, url}`),
`view_count` and 16:9 `thumbnails` are filled in. If the Music provider fails, the server falls back to video
search and sets `"source": "youtube"`, so the response shape doesn't change.

**`type=playlist`** (v0.3.0): `results` is `[]` (so older clients never see a new shape) and the hits are in
`playlists`:

```json
{
  "query": "lofi beats", "type": "playlist", "source": "youtube_music", "cached": false, "results": [],
  "playlists": [
    {
      "result_type": "playlist",
      "playlist_id": "PLVppO9BnnvNXywdH5MdT_nl3sYY12YmeU",
      "title": "Dark Lofi Type Beats",
      "author": "warhol avenue.",
      "item_count": null,
      "thumbnails": [{ "url": "https://i.ytimg.com/vi/.../hqdefault.jpg", "width": 400, "height": 225 }],
      "watch_url": "https://music.youtube.com/playlist?list=PLVppO9BnnvNXywdH5MdT_nl3sYY12YmeU"
    }
  ]
}
```

`playlist_id` is the plain ID (YouTube Music's `VL` browse prefix is removed). `item_count` is often `null`
(YouTube Music doesn't always say); the playlist endpoint returns the real count.

**`type=all`** (v0.3.0): songs, videos and playlists are fetched **concurrently** and returned in `sections`.
Each section is cached separately (sharing the cache of the single-type searches). A failing section comes back
empty and is named in `sections.errors` (section → error `code`); the request fails only when all three fail
(with the first section's error). It counts once against the per-device search limit and three times against
the global upstream budget.

```json
{
  "query": "daft punk", "type": "all", "source": "mixed", "cached": false, "results": [],
  "sections": {
    "songs":     [ { "video_id": "FGBhQbmPwH8", "result_type": "song", "...": "..." } ],
    "videos":    [ { "video_id": "FGBhQbmPwH8", "result_type": "video", "...": "..." } ],
    "playlists": [ { "playlist_id": "PL...", "result_type": "playlist", "...": "..." } ],
    "errors": { }
  }
}
```

For `song` and `video` responses, `playlists` and `sections` are `null`.

---

## GET /v1/playlists/{playlist_id}

Playlist info plus its playable tracks, in the **same shape as search results** (v0.3.0).

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `playlist_id` | path | **required** | `^(VL)?[A-Za-z0-9_-]{10,64}$`; a `VL` prefix is stripped. Anything else → `422 invalid_playlist_id` |
| `limit` | int | `MAX_PLAYLIST_TRACKS` (200) | 1–`MAX_PLAYLIST_TRACKS` |

```json
{
  "playlist_id": "PL0BKTsqcDLNFhlmyCUzRyYEF6ox9PI1z5",
  "title": "Daft Punk: Best Of",
  "author": "Daft Punk",
  "description": null,
  "track_count": 19,
  "duration": 5478,
  "thumbnails": [{ "url": "https://yt3.ggpht.com/...=s576", "width": 576, "height": 576 }],
  "watch_url": "https://music.youtube.com/playlist?list=PL0BKTsqcDLNFhlmyCUzRyYEF6ox9PI1z5",
  "tracks": [
    { "video_id": "qe8Q7mjxjig", "result_type": "song", "title": "Veridis Quo", "...": "..." }
  ],
  "truncated": true
}
```

- A track's `result_type` is `song` for YouTube Music catalogue audio (`MUSIC_VIDEO_TYPE_ATV`, `watch_url` on
  `music.youtube.com`), otherwise `video` (`www.youtube.com`).
- Unavailable entries and entries without a video ID are dropped, so `tracks` can be shorter than `track_count`.
- `truncated` is `true` when the playlist has more than `limit` tracks.
- Private, deleted or unopenable playlists → `404 playlist_not_found`. Cached for `METADATA_CACHE_TTL`.

---

## GET /v1/videos/{video_id}

Full metadata for one video, plus the audio formats on offer. Cached for about 1 hour.

```json
{
  "video_id": "FGBhQbmPwH8",
  "title": "Daft Punk - One More Time (Official Video)",
  "track": "One More Time",
  "artist": "Daft Punk",
  "album": "Discovery",
  "release_year": 2001,
  "channel": { "name": "Daft Punk", "id": "UC_kRDKYrUlrbtrSiyu5Tflg", "url": "https://www.youtube.com/channel/UC_kRDKYrUlrbtrSiyu5Tflg" },
  "duration": 320,
  "upload_date": "2009-02-26",
  "description": "…",
  "view_count": 512340012,
  "like_count": 4100022,
  "categories": ["Music"],
  "tags": ["daft punk", "one more time"],
  "chapters": [],
  "thumbnails": [{ "url": "https://i.ytimg.com/vi/FGBhQbmPwH8/maxresdefault.jpg", "width": 1280, "height": 720 }],
  "flags": {
    "embeddable": true,
    "age_restricted": false,
    "is_live": false,
    "downloadable": true,
    "reason": null
  },
  "audio_formats": [
    { "format_id": "251", "codec": "opus", "container": "webm", "abr_kbps": 135, "sample_rate": 48000, "filesize_approx": 5401234 },
    { "format_id": "140", "codec": "aac",  "container": "m4a",  "abr_kbps": 129, "sample_rate": 44100, "filesize_approx": 5180000 }
  ],
  "output_options": [
    { "format": "m4a",  "transcode": false, "est_size_bytes": 5180000, "default": true },
    { "format": "opus", "transcode": false, "est_size_bytes": 5401234, "min_android_api": 29 },
    { "format": "mp3",  "transcode": true,  "est_size_bytes": 9600000 }
  ]
}
```

`flags.downloadable` is `false` when a later download would definitely fail (live, too long, unavailable).
`flags.reason` then holds the matching error `code`.

---

## GET /v1/videos/{video_id}/preview

A lightweight subset for the preview sheet. It's cheap to call when the user taps a result.

```json
{
  "video_id": "FGBhQbmPwH8",
  "embeddable": true,
  "age_restricted": false,
  "is_live": false,
  "embed_url": "https://www.youtube-nocookie.com/embed/FGBhQbmPwH8?playsinline=1&rel=0",
  "watch_url": "https://www.youtube.com/watch?v=FGBhQbmPwH8",
  "thumbnail": "https://i.ytimg.com/vi/FGBhQbmPwH8/hqdefault.jpg",
  "title": "Daft Punk - One More Time (Official Video)",
  "duration": 320
}
```

> **Client note (for the later Android work):** play the preview with the official IFrame player, ideally
> through `android-youtube-player`. If you build the WebView yourself, load the HTML with
> `loadDataWithBaseURL("https://<your-app-domain>/", …)` so a `Referer` is sent; otherwise YouTube shows
> error 153. When `embeddable` is `false`, show the thumbnail and an "Open in YouTube" action instead.

---

## GET /v1/videos/{video_id}/track

"Find Music": what the file **will** be tagged with, resolved before downloading from YouTube Music, MusicBrainz
and the video itself (see [server-implementation-plan.md](server-implementation-plan.md)). It's cached for about
1 hour and usually takes 1–5 s the first time. Pass `?enrich=false` to skip MusicBrainz / Last.fm.

```json
{
  "video_id": "4D7u5KF7SP8",
  "title": "Get Lucky (feat. Pharrell Williams and Nile Rodgers)",
  "artists": ["Daft Punk", "Pharrell Williams", "Nile Rodgers"],
  "album_artist": "Daft Punk",
  "album": "Random Access Memories",
  "track_number": 8, "track_total": 13,
  "disc_number": 1, "disc_total": 1,
  "date": "2013-05-17", "year": 2013,
  "genres": ["Electronic", "Disco", "Funk"],
  "isrc": "USQX91300108",
  "explicit": false,
  "duration": 370,
  "artwork_url": "https://lh3.googleusercontent.com/…=w1200-h1200-l90-rj",
  "lyrics": { "available": true, "synced": false, "source": "YouTube Music", "lines": 0 },
  "musicbrainz": { "release_group_id": "aa997ea0-…", "release_id": "158f94bf-…", "recording_id": "833f00e1-…" },
  "sources": { "title": "ytmusic", "album": "ytmusic", "track_number": "ytmusic", "date": "musicbrainz",
               "genres": "musicbrainz", "isrc": "musicbrainz" },
  "video_type": "MUSIC_VIDEO_TYPE_ATV",
  "suggested_filename": { "m4a": "Daft Punk - Get Lucky (feat. Pharrell Williams and Nile Rodgers).m4a",
                          "opus": "…opus", "mp3": "…mp3" }
}
```

`lyrics.available` here only means YouTube Music has lyrics for the song. Whether they're synced is known after
the download (LRCLIB may still provide synced lyrics). `video_type` is `ATV` for album audio, `OMV` for an official
music video, `UGC` for an upload. Fields the server couldn't find are `null` / empty.

---

## POST /v1/downloads

Start processing a video into a tagged audio file. Returns immediately.

**Request**

```json
{
  "video_id": "FGBhQbmPwH8",
  "format": "m4a",
  "quality": "best",
  "metadata": {
    "title": "One More Time",
    "artist": "Daft Punk",
    "album": "Discovery",
    "year": 2001
  }
}
```

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `video_id` | string | **required** | 11-char ID |
| `format` | `m4a` \| `opus` \| `mp3` | `m4a` | See [research.md §4.4](research.md#44-output-format-decision) |
| `quality` | `best` \| `standard` | `best` | Only affects `mp3` (`best` = VBR V0, `standard` = 192 kbps CBR). Remuxed formats always use the source quality |
| `metadata` | object | `null` | Optional overrides, e.g. the song-search result the user tapped. Merged over extracted metadata. Fields: `title`, `artist` or `artists[]`, `album`, `album_artist`, `year` or `date`, `track_number`, `disc_number`, `genre` or `genres[]`, and `artwork_url` (e.g. the square art from a song search result; https on googleusercontent.com / ytimg.com only) |
| `embed_artwork` | bool | `true` | Embed the square cover in the file |
| `embed_lyrics` | bool | `true` | Embed lyrics (synced LRC when available) |
| `enrich` | bool | `true` | Use MusicBrainz / Last.fm for release date, genres, disc number and ISRC |

Clients may also send an **`Idempotency-Key`** header (UUID). Repeating the same key within 24 h returns the
original job instead of creating a new one, which protects against double taps and mobile retries.

**Response `202 Accepted`**

```http
Location: /v1/downloads/dl_01JBX7A2M4R8YQ6W0E3H5T9KCV
```

```json
{
  "job_id": "dl_01JBX7A2M4R8YQ6W0E3H5T9KCV",
  "status": "queued",
  "cache_hit": false,
  "created_at": "2026-11-02T18:04:11Z",
  "links": {
    "self": "/v1/downloads/dl_01JBX7A2M4R8YQ6W0E3H5T9KCV",
    "events": "/v1/downloads/dl_01JBX7A2M4R8YQ6W0E3H5T9KCV/events",
    "file": "/v1/downloads/dl_01JBX7A2M4R8YQ6W0E3H5T9KCV/file",
    "artwork": "/v1/downloads/dl_01JBX7A2M4R8YQ6W0E3H5T9KCV/artwork"
  }
}
```

If an identical, already-processed file is cached, the job is created as `completed` with `"cache_hit": true`.
The client handles both cases the same way.

Errors at submit time: `401`, `422 invalid_video_id|invalid_format`, `429 rate_limited|too_many_active_jobs`,
`503 queue_full`. Video-level problems (unavailable, too long, …) are reported **through the job**, because
detecting them requires extraction.

---

## GET /v1/downloads/{job_id}

**While running**

```json
{
  "job_id": "dl_01JBX7A2M4R8YQ6W0E3H5T9KCV",
  "status": "downloading",
  "progress": 0.42,
  "stage": { "name": "downloading", "progress": 0.61, "downloaded_bytes": 3170000, "total_bytes": 5180000, "speed_bps": 1450000, "eta_seconds": 2 },
  "video_id": "FGBhQbmPwH8",
  "format": "m4a",
  "created_at": "2026-11-02T18:04:11Z",
  "updated_at": "2026-11-02T18:04:15Z"
}
```

`status` ∈ `queued | resolving | downloading | processing | tagging | verifying | completed | failed | cancelled | expired`.
`progress` is overall (0–1) and only ever increases. Poll every 1–2 s, or use `/events`.

**When completed** (real output for Daft Punk – Get Lucky, `format=opus`)

```json
{
  "job_id": "dl_01M6EZ…",
  "status": "completed",
  "progress": 1.0,
  "video_id": "4D7u5KF7SP8",
  "format": "opus",
  "cache_hit": false,
  "file": {
    "filename": "Daft Punk - Get Lucky (feat. Pharrell Williams and Nile Rodgers).opus",
    "mime_type": "audio/ogg",
    "size_bytes": 6286631,
    "sha256": "…",
    "duration": 369.9,
    "codec": "opus",
    "bitrate_kbps": 136,
    "sample_rate": 48000,
    "channels": 2,
    "transcoded": false,
    "quality": { "codec": "opus", "bitrate_kbps": 136, "sample_rate": 48000, "channels": 2, "duration": 369.9,
                 "max_volume_db": 0.0, "mean_volume_db": -9.8, "transcoded": false,
                 "source_format_id": "251", "source_codec": "opus" },
    "expires_at": "2026-11-04T04:20:00Z"
  },
  "metadata": {
    "title": "Get Lucky (feat. Pharrell Williams and Nile Rodgers)",
    "artists": ["Daft Punk", "Pharrell Williams", "Nile Rodgers"],
    "album_artist": "Daft Punk",
    "album": "Random Access Memories",
    "track_number": 8, "track_total": 13, "disc_number": 1, "disc_total": 1,
    "date": "2013-05-17", "year": 2013,
    "genres": ["Electronic", "Disco", "Funk"],
    "isrc": "USQX91300108",
    "explicit": false,
    "duration": 370,
    "artwork_url": "https://lh3.googleusercontent.com/…=w1200-h1200-l90-rj",
    "lyrics": { "available": true, "synced": true, "source": "YouTube Music (LyricFind)", "lines": 109 },
    "musicbrainz": { "release_group_id": "aa997ea0-…", "release_id": "158f94bf-…", "recording_id": "833f00e1-…" },
    "sources": { "title": "ytmusic", "artists": "ytmusic", "album": "ytmusic", "track_number": "ytmusic",
                 "date": "musicbrainz", "genres": "musicbrainz", "isrc": "musicbrainz", "lyrics": "lyrics" },
    "video_type": "MUSIC_VIDEO_TYPE_ATV",
    "source": { "provider": "youtube", "video_id": "4D7u5KF7SP8", "url": "https://www.youtube.com/watch?v=4D7u5KF7SP8",
                "channel": "Daft Punk", "upload_date": "2018-02-23", "format_id": "251" },
    "artwork": { "embedded": true, "width": 1200, "height": 1200, "mime_type": "image/jpeg" }
  },
  "links": { "file": "/v1/downloads/dl_01M6EZ…/file", "artwork": "/v1/downloads/dl_01M6EZ…/artwork",
             "lyrics": "/v1/downloads/dl_01M6EZ…/lyrics" }
}
```

Everything under `metadata` is also **embedded in the file** (the mapping per format is in
[server-implementation-plan.md §3.6](server-implementation-plan.md#36-tag-writer-mediatagspy-rewritten)), so the
app doesn't have to write tags itself. `metadata.warnings` appears when an optional provider failed; the job still
succeeds.

**When failed**

```json
{
  "job_id": "dl_01JBX7A2M4R8YQ6W0E3H5T9KCV",
  "status": "failed",
  "progress": 0.1,
  "error": { "code": "upstream_blocked", "detail": "YouTube requested sign-in to confirm this is not a bot.", "retryable": true },
  "attempts": 3
}
```

---

## GET /v1/downloads/{job_id}/events

`text/event-stream`. Emits one `progress` event per state or progress change (at most 4 per second), then one
terminal `completed` | `failed` | `cancelled` event, then closes. Each event's `data` is the same JSON as the
status endpoint.

```text
event: progress
data: {"job_id":"dl_01J…","status":"downloading","progress":0.42,…}

event: completed
data: {"job_id":"dl_01J…","status":"completed",…}
```

Polling is always supported. SSE is optional and is there to make the UI more responsive.

---

## GET /v1/downloads/{job_id}/file

Streams the tagged audio file.

| Response header | Example |
|-----------------|---------|
| `Content-Type` | `audio/mp4` (m4a) · `audio/ogg` (opus) · `audio/mpeg` (mp3) |
| `Content-Length` | `5214332` |
| `Content-Disposition` | `attachment; filename="Daft Punk - One More Time.m4a"; filename*=UTF-8''Daft%20Punk%20-%20One%20More%20Time.m4a` |
| `ETag` | `"9b1d5c0f6c1e…"` (the sha256) |
| `Accept-Ranges` | `bytes` |
| `X-Content-SHA256` | `9b1d5c0f6c1e…` |

- `Range: bytes=N-` is supported (`206 Partial Content`), so interrupted mobile downloads can resume.
- `409 job_not_ready` if the job isn't completed, `410 job_expired` if the file was evicted.
- Fetching the file refreshes its TTL. The app should verify the SHA-256 before saving it.

## GET /v1/downloads/{job_id}/artwork

`image/jpeg`, square, ≤ 1200 px, the same image that is embedded in the file. Useful when the app wants to
cache artwork separately.

## GET /v1/downloads/{job_id}/lyrics

`text/plain; charset=utf-8`. The same lyrics that are embedded in the file's `LYRICS` tag:

- synced: one `[mm:ss.mmm]text` line per lyric line, no header tags (the format Harmoniq's LRC parser expects);
- plain: the lyric text.

The `X-Lyrics-Synced: true|false` header says which. `Content-Disposition` suggests `<filename>.lrc` or `.txt`.
`404 lyrics_not_available` if none were found. The app doesn't need this endpoint (lyrics are embedded), but
it can save a `.lrc` next to the song if wanted.

## DELETE /v1/downloads/{job_id}

- Non-terminal job → cancelled (the worker aborts at its next checkpoint), `200 {"status":"cancelled"}`.
- Completed job → releases this job's reference. The file is removed once no other job references it, or when
  the TTL runs out. Clients should call this after they have saved the file. `204`.

---

## GET /v1/meta

```json
{
  "api_version": "1.0.0",
  "server_version": "0.3.0",
  "formats": ["m4a", "opus", "mp3"],
  "default_format": "m4a",
  "max_duration_seconds": 900,
  "limits": { "search_per_minute": 30, "downloads_per_hour": 60, "max_active_jobs": 3, "max_concurrent_downloads": 2 },
  "features": { "metadata_providers": ["ytmusic", "youtube", "musicbrainz", "lyrics"],
                "lyrics_providers": ["ytmusic", "lrclib"], "musicbrainz": true, "lastfm": false,
                "filename_template": "{artist} - {title}",
                "search_types": ["song", "video", "playlist", "all"],
                "max_playlist_tracks": 200, "search_all_section_limit": 10 },
  "components": { "yt_dlp": "2026.10.26", "yt_dlp_ejs": "0.8.0", "ytmusicapi": "1.12.3",
                  "ffmpeg": "ffmpeg version N-128280-…", "deno": "deno 2.9.8 (…)", "python": "3.13.3" },
  "queue": { "waiting": 0, "active": 1 },
  "status": { "youtube": "ok", "last_canary_at": "2026-11-02T17:00:03Z", "last_canary_ok": true }
}
```

`features.search_types` (v0.3.0) lists the `type` values `/v1/search` accepts. Older servers don't send it;
clients should then assume `["song", "video"]`.

`status.youtube` is `blocked` for 10 minutes after any bot check, `degraded` if the latest canary failed, otherwise
`ok`. Without `CANARY_VIDEO_ID` nothing tests YouTube in the background, so it stays `ok` until a real job is blocked.
The app can show a banner instead of letting users start downloads that will fail.

## Health

| Endpoint | Auth | `200` when | Used by |
|----------|------|------------|---------|
| `GET /healthz` | none | The process is serving | Docker `HEALTHCHECK`, restart policy |
| `GET /readyz` | none | Data folder writable and enough free disk, `ffmpeg`/`ffprobe`/`deno` found | Docker healthcheck, uptime monitors |

---

## End-to-end example (shell)

```bash
H="X-App-Token: $HQ_TOKEN"; BASE=https://harmoniq.example.com
ID=$(curl -s "$BASE/v1/search?q=one+more+time&limit=1" -H "$H" | jq -r '.results[0].video_id')
JOB=$(curl -s -X POST "$BASE/v1/downloads" -H "$H" -H 'Content-Type: application/json' \
  -d "{\"video_id\":\"$ID\",\"format\":\"m4a\"}" | jq -r .job_id)
until [ "$(curl -s "$BASE/v1/downloads/$JOB" -H "$H" | jq -r .status)" = completed ]; do sleep 2; done
curl -s -OJ "$BASE/v1/downloads/$JOB/file" -H "$H"
curl -s -X DELETE "$BASE/v1/downloads/$JOB" -H "$H"
```

## Versioning

Everything is under `/v1`. Adding fields is non-breaking, so clients must ignore unknown fields. Removing or
renaming fields, or changing their meaning, requires `/v2`. Error `code` values are part of the contract.
