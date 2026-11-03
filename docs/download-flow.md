# Download flow: search → preview → download → audio processing

[← Docs index](README.md)

This document traces one song from the user's search to a tagged file saved on the phone. It covers every server
step, the exact yt-dlp/FFmpeg behaviour, timings, retries, and what happens when each step fails.
Endpoint shapes are in [api.md](api.md); components are in [architecture.md](architecture.md).

---

## 0. Overview

```text
 USER            ANDROID APP                     API                    WORKER                  YOUTUBE
  │ types query ─▶│                               │                        │                        │
  │               │── GET /v1/search ────────────▶│── ytmusicapi/yt-dlp ──────────────────────────▶│
  │               │◀──────── results ─────────────│◀───────────────────────────────────────────────│
  │ taps result ─▶│── GET /v1/videos/{id}/preview▶│── extract_info (cached) ──────────────────────▶│
  │               │◀── embeddable, embed_url ─────│                        │                        │
  │               │══ IFrame player (direct) ═══════════════════════════════════════════════════════▶│
  │ "Download" ──▶│── POST /v1/downloads ────────▶│── enqueue ────────────▶│                        │
  │               │◀── 202 job_id ────────────────│                        │── resolve / download ─▶│
  │               │── GET /v1/downloads/{id} ⟳ ──▶│◀── progress (memory) ──│── ffmpeg, tag, verify  │
  │               │◀── completed + metadata ──────│                        │                        │
  │               │── GET …/file (Range) ────────▶│── stream data/files ───│                        │
  │               │   verify sha256, write to SAF music folder, rescan library                     │
  │               │── DELETE /v1/downloads/{id} ─▶│                        │                        │
  │ sees song ◀───│                               │                        │                        │
```

Typical timings for a 4-minute song on a healthy residential connection:

| Step | Cold | Warm (cached) |
|------|------|---------------|
| Search | 0.6–1.5 s | < 50 ms |
| Preview info | 1–4 s | < 50 ms |
| Download job, m4a | 4–12 s | < 100 ms (dedupe hit) |
| Download job, mp3 | 8–20 s | < 100 ms |
| File transfer to phone (~4 MB) | network-bound | network-bound |

---

## 1. Search

1. App calls `GET /v1/search?q=…&type=song`.
2. API normalises the query (trim, Unicode NFC, collapse whitespace). The cache key is
   `search:{type}:{limit}:{sha1(lowercased query)}`.
3. On a cache miss, the provider runs in a threadpool with a 10 s timeout:
   - **`type=song`**: `YTMusic().search(q, filter="songs", limit=limit)`. Map `videoId`, `title`, `artists[]`,
     `album`, `duration_seconds`, `isExplicit`, `thumbnails`.
   - **`type=video`**: yt-dlp with `{"extract_flat": "in_playlist", "skip_download": True, "quiet": True}` on
     `ytsearch{limit}:{q}`. Map each entry's `id`, `title`, `channel`, `channel_id`, `duration`, `view_count`,
     `thumbnails`. Drop entries whose `live_status` is `is_live`/`is_upcoming`, and anything that isn't a video
     (channels, playlists).
4. If the `song` provider raises, the API logs it, falls back to `video`, and sets `source: "youtube"`.
5. The result is cached for `SEARCH_CACHE_TTL` (6 h) and returned.

**Failures:** an upstream exception → `502 upstream_error`; a timeout → `504 upstream_timeout`. Search never
creates jobs.

## 2. Preview / verification

1. When the user taps a result, the app calls `GET /v1/videos/{id}/preview`.
2. The API calls `extract_info(id, download=False)` (cached `METADATA_CACHE_TTL` = 1 h) and computes:
   - `embeddable`: from yt-dlp's `playable_in_embed`, when present
   - `age_restricted`: `age_limit >= 18`
   - `is_live`: `live_status in {is_live, is_upcoming}`
3. The app plays `embed_url` in the official IFrame player. **No media passes through the server.**
4. If `embeddable` is `false`, or the player reports error 101/150/153, the app shows the thumbnail card and an
   "Open in YouTube" link. The user can still download.

Preview is optional. The user can download straight from the result list.

## 3. Submitting the download

`POST /v1/downloads {video_id, format, metadata?}`:

1. **Auth and limits:** check the app token, the per-device hourly download count, and the active-jobs count. Return
   `503 queue_full` if the queue is deeper than `QUEUE_MAX_DEPTH`.
2. **Idempotency:** if `Idempotency-Key` was seen in the last 24 h (kept in memory), return the stored job.
3. **Dedupe:** `dedupe_key = sha1(f"{video_id}|{format}|{quality}|{TAGGER_VERSION}|{sha1(metadata_overrides)}")`.
   - If `data/files/{dedupe_key}.json` exists, create a job that is already `completed` (`cache_hit: true`).
   - If a job with the same key is queued or running, return that job's ID to this caller as well. The job keeps
     a set of owner device IDs.
4. Create the job (ULID with `dl_` prefix) as `queued` in the in-memory `JobRegistry`, then
   `await job_manager.queue.put(job_id)`. One of the `MAX_CONCURRENT_DOWNLOADS` worker tasks picks it up.
5. Respond `202` with `Location`.

## 4. Worker pipeline

`jobs/pipeline.py::process_download(job)` runs these stages in order. Each one updates the in-memory job
(status, stage progress, `updated_at`) and checks the job's **cancel event** (`asyncio.Event`) before starting.

Overall progress weights: resolving 0–0.10, downloading 0.10–0.75, processing 0.75–0.88, tagging 0.88–0.95,
verifying 0.95–1.0.

### 4.1 `resolving`

```python
info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
```

Reject early with a precise code:

| Condition | `error.code` |
|-----------|--------------|
| `live_status in (is_live, is_upcoming, post_live)` | `live_not_supported` |
| `duration > MAX_DURATION_SECONDS` (default 900) | `duration_limit_exceeded` |
| `availability in (private, premium_only, subscriber_only, needs_auth)` | `video_unavailable` / `age_restricted` |
| No audio-only format at all | `upstream_error` |

**Format selection** (yt-dlp `format` string):

| Requested | Format selector | Why |
|-----------|-----------------|-----|
| `m4a` | `bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio` | Prefer AAC itag 140 so we can remux. Fall back to anything and transcode to AAC |
| `opus` | `bestaudio[acodec=opus]/bestaudio` | Prefer itag 251, remux WebM → Ogg |
| `mp3` | `bestaudio` | Transcoding anyway, so take the highest-quality source |

If the fallback branch is used (no native AAC/Opus), `processing` transcodes. The job records `"transcoded": true`.

### 4.2 `downloading`

yt-dlp options (built in `providers/ytdlp_options.py`):

```python
{
  "format": selector,
  "outtmpl": f"{DATA_DIR}/tmp/{job_id}/source.%(ext)s",
  "noplaylist": True,
  "quiet": True, "no_warnings": False,
  "retries": 3, "fragment_retries": 3, "extractor_retries": 2,
  "socket_timeout": 20,
  "concurrent_fragment_downloads": 1,
  "progress_hooks": [hook],            # updates the in-memory Job; raises if cancelled
  "allowed_extractors": ["youtube"],   # never reach other sites
  "js_runtimes": {"deno": {"path": settings.deno_path}},   # tools/deno(.exe) locally, PATH in Docker
  "ffmpeg_location": settings.ffmpeg_dir,                  # tools/ locally, PATH in Docker
  "cookiefile": settings.YTDLP_COOKIES_FILE or None,
  "proxy": settings.YTDLP_PROXY or None,
  # only when POT_PROVIDER_URL is set:
  "extractor_args": {"youtubepot-bgutilhttp": {"base_url": [settings.POT_PROVIDER_URL]}},
  "sleep_interval_requests": settings.YTDLP_SLEEP_REQUESTS,   # be gentle with upstream
  "ratelimit": settings.YTDLP_RATE_LIMIT_BPS or None,
}
```

> The option names for the JS runtime and the PO-token extractor args change between yt-dlp/plugin versions.
> Check them against the installed versions' README when implementing ([research.md §4.2](research.md#42-required-companions-for-yt-dlp-on-youtube-2026)).

yt-dlp is synchronous, so the worker task runs it with `asyncio.to_thread`. The whole job is wrapped in
`asyncio.wait_for(…, JOB_TIMEOUT_SECONDS)` (default 600 s). A cancelled or timed-out download is stopped by
raising from the progress hook (a thread can't be killed from outside, so the hook checks the flag).

### 4.3 `processing` (FFmpeg)

Inputs: `source.webm` or `source.m4a`. Output: `data/tmp/{job_id}/out.{ext}`. All commands run with
`-nostdin -hide_banner -loglevel error -y`, a subprocess timeout, and `-map 0:a:0 -vn -map_metadata -1` (drop
video and upstream tags; we write our own).

| Output | Source | Command (essential flags) |
|--------|--------|---------------------------|
| m4a | AAC | `ffmpeg -i source.m4a -c:a copy -movflags +faststart out.m4a` |
| m4a | Opus (fallback) | `ffmpeg -i source.webm -c:a aac -b:a 192k -movflags +faststart out.m4a` |
| opus | Opus | `ffmpeg -i source.webm -c:a copy out.opus` (Ogg container) |
| opus | AAC (fallback) | `ffmpeg -i source.m4a -c:a libopus -b:a 160k out.opus` |
| mp3 `best` | any | `ffmpeg -i source.* -c:a libmp3lame -q:a 0 out.mp3` |
| mp3 `standard` | any | `ffmpeg -i source.* -c:a libmp3lame -b:a 192k out.mp3` |

Notes:
- `+faststart` moves the `moov` atom to the front so Android can read duration and tags immediately.
- Loudness normalisation (`loudnorm`) is deliberately **not** applied. That would always force a transcode.
  It can be added later as an opt-in.
- Progress during transcode comes from parsing `-progress pipe:1` (`out_time_ms` ÷ duration).

### 4.4 `tagging` (metadata + artwork)

Metadata is resolved **in parallel with the download** (it only needs the video ID) by the providers in
`METADATA_PROVIDERS`. Each provider only fills fields that are still empty, so the order is the precedence:

1. request `metadata` overrides
2. **YouTube Music catalogue**: artists, album, album artist, year, track n/total (matched by title + duration),
   square album art, lyrics ID. Music videos and uploads borrow the album of the matching catalogue song.
3. **yt-dlp info**: YouTube's own music fields, else "Artist - Title" heuristics and the channel name
4. **MusicBrainz** (album-level match): full first-release date (this one *replaces* a YouTube date),
   genres (top 3), disc n/total, ISRC, MBIDs
5. **Last.fm** genres (only with `LASTFM_API_KEY`)
6. **Lyrics**: YouTube Music timed lyrics → LRCLIB synced → plain, written as `[mm:ss.mmm]` LRC with no headers

When the audio is ready, the job waits for metadata until `METADATA_TIMEOUT_SECONDS` after the start (default 25 s),
then uses whatever was found. Optional providers never fail a job.

**Artwork:** the album art URL from YouTube Music (upscaled to 1200 px), else the request's `artwork_url`, else the
best video thumbnail. It's square-cropped, resized to ≤ 1200 px and stored as JPEG.

**Tags:** one writer per container, all targeting the TagLib property map Harmoniq reads. The full field
mapping is in [server-implementation-plan.md §3.6](server-implementation-plan.md#36-tag-writer-mediatagspy-rewritten).

### 4.5 `verifying`

`media/quality.py` runs ffprobe and FFmpeg `volumedetect`, and fails the job (`processing_failed`, retried once) on:

- not exactly one audio stream, or an unexpected codec
- duration more than 2 s off the video's (truncated download)
- average bitrate below 48 kbps, or sample rate below 32 kHz
- silence (peak below −50 dB)

Then the tags are read back (title, artist, cover) to make sure they survived writing, and the SHA-256 is computed.
The results are returned to the app as `file.quality`.

### 4.6 `publishing`

1. Atomically rename `out.{ext}` → `data/files/{dedupe_key}.{ext}` (same filesystem), and the cover →
   `{dedupe_key}.jpg`.
2. Write the sidecar `data/files/{dedupe_key}.json` = `{metadata, quality, lyrics_text, size, sha256, mime, ext,
   created_at, last_access}` (written to a temp name, then renamed, so it's never half-written).
3. Build the display filename `"{artist} - {title}.{ext}"`, sanitised: strip `\/:*?"<>|` and control chars,
   collapse whitespace, cap at 150 chars.
4. Update the job to `completed` with `file` + `metadata`. Remove `data/tmp/{job_id}`.

## 5. Delivering to the phone

1. The app sees `completed` (via polling or SSE).
2. `GET /v1/downloads/{job_id}/file`, resuming with `Range` after network drops.
3. The app checks `sha256`, writes the bytes to the user's music folder, then `DELETE`s the job. (Later Android
   work: write through the SAF tree URI the user already granted, via `DocumentFile.createFile(mime, filename)`,
   then trigger a Groove/`MediaExposer` rescan. Run it all inside a `WorkManager` job so it survives the app
   going to the background.)
4. The server's copy expires after `FILE_TTL_SECONDS` regardless.

## 6. Failure handling and retries

| Failure | Detected in | Retry policy | Final `error.code` |
|---------|------------|--------------|--------------------|
| Network blip / HTTP 5xx from googlevideo | downloading | yt-dlp internal retries (3); then the pipeline retries the job up to 2× with backoff 10 s, 30 s | `upstream_error` |
| HTTP 403 on stream (expired URL / missing PO token) | downloading | 1 pipeline retry with fresh extraction | `upstream_error` |
| "Sign in to confirm you're not a bot" / `LOGIN_REQUIRED` | resolving | **No immediate retry.** Increments the `yt_blocked` counter and flips `/v1/meta` `status.youtube` to `blocked` for 10 min | `upstream_blocked` |
| HTTP 429 from YouTube | any | Back off 60 s, 1 retry; global extraction bucket halves for 10 min | `upstream_blocked` |
| Private / removed / region / premium | resolving | none | `video_unavailable` |
| Age-gate without cookies | resolving | none | `age_restricted` |
| FFmpeg non-zero exit | processing | 1 retry | `processing_failed` |
| Verification mismatch | verifying | 1 retry (full re-download) | `processing_failed` |
| Job exceeds `JOB_TIMEOUT_SECONDS` | `asyncio.wait_for` | none | `upstream_timeout` |
| Server restart / crash mid-job | — | Job is lost; the app gets `404 job_not_found` and resubmits. Leftover tmp dirs are removed at startup | — |
| Disk full | publishing | none; `/readyz` goes red | `service_unavailable` |

Error mapping lives in one place (`providers/ytdlp_client.py::map_ytdlp_error`). It matches yt-dlp's
`DownloadError`/`ExtractorError` messages and `.exc_info`, and every unmatched message is logged in full so
new patterns can be added.

## 7. Cancellation and cleanup

- `DELETE` on a running job sets the job's cancel event. The progress hook raises, FFmpeg subprocesses are
  killed, the tmp dir is removed, and the status becomes `cancelled`. If other owners share the job (dedupe), only this
  owner's reference is removed and the job keeps running.
- **Sweeper** (background asyncio task, at startup and every 10 min):
  - deletes files in `data/files/` whose sidecar `last_access` is older than `FILE_TTL_SECONDS`, or that have no sidecar
  - deletes `data/tmp/*` older than 1 h (everything, at startup)
  - deletes the oldest files when usage is above `STORAGE_MAX_BYTES`
  - marks jobs whose file disappeared as `expired`, and forgets job records older than `JOB_TTL_SECONDS`
- **Canary** (background task, hourly, only if `CANARY_VIDEO_ID` is set): runs a full m4a pipeline on that
  video (pick a stable Creative Commons track) into a scratch dir, and records the result and yt-dlp version for
  `/v1/meta`.

## 8. Concurrency and throughput

- `MAX_CONCURRENT_DOWNLOADS` (default **2**) jobs run at the same time; the rest wait in the queue (up to
  `QUEUE_MAX_DEPTH`, default 20). Remux jobs are mostly waiting on the network. mp3 jobs use about one CPU core
  each while encoding.
- Rough capacity at the defaults on any modern PC: **10-20 m4a downloads per minute**. That's far more than a few
  testers need. The real limit is YouTube's tolerance for one IP, which is why `YT_MIN_INTERVAL_SECONDS` exists and
  why the file cache matters.
- Search and metadata requests don't wait behind downloads. They run in FastAPI's threadpool, separate from the
  download workers.
