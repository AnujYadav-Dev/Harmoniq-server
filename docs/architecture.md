# Architecture

[← Docs index](README.md)

This document describes the design of **harmoniq-server**: the components, how data flows between them, and
how it can grow later. The reasons behind each dependency are in [research.md](research.md).

> **Status:** implemented (v0.2.0). This document describes the code as built. The metadata pipeline added in
> v0.2.0 is specified in [server-implementation-plan.md](server-implementation-plan.md).
>
> **Scope:** personal project. One owner plus a few testers. The server runs on a single machine, as **one
> process**, either from a local Python virtual environment or as one Docker container. No Redis, no message
> broker, no database.

---

## 1. Design principles

1. **One process, zero infrastructure.** FastAPI serves the API and also runs the download jobs in background
   tasks. You don't need Redis, a queue broker or a database: one command starts everything.
2. **Keep the request path fast and the slow work in the background.** Search and metadata answer within a few
   seconds. Downloads are *jobs*: the API accepts them immediately (`202`) and processes them in the background
   with bounded concurrency.
3. **The filesystem is the only persistent state.** Finished audio files and their metadata sidecars live in one
   `data/` folder. Everything else (job progress, search cache, rate-limit counters) is in memory, and it's fine
   to lose it on restart.
4. **Everything in one folder.** Code, Python packages (`.venv/`), tool binaries (`tools/`: FFmpeg, ffprobe, Deno)
   and data (`data/`) all live inside `Harmoniq-server/`. Nothing is installed system-wide. Deleting the folder
   removes everything.
5. **Isolate YouTube-specific code.** Everything that talks to YouTube lives in `app/providers/`
   (`YtDlpClient` for search/metadata/download, `YTMusicSearch` for songs, `mapping.py` for turning their output
   into API models). When YouTube changes, only those files and the yt-dlp version change.
6. **Remux first, transcode only when asked.** This keeps quality and CPU cost in the best place by default.
7. **Fail loudly with specific errors.** Every upstream failure maps to a stable error `code` the app can react to
   (see [api.md §Errors](api.md#errors)).
8. **Swappable internals.** Job runner, caches and file store are behind small interfaces. If this ever needs to
   serve many users, they can be replaced (for example with a Redis-backed queue) without changing the API.

---

## 2. System context

```text
 ┌────────────────────────┐   HTTPS + X-App-Token (JSON + audio), via tunnel ┌─────────────────────────┐
 │  Harmoniq Android app  │ ──────────────────────────────────────────────▶ │     harmoniq-server     │
 │  (later integration)   │ ◀────────────────────────────────────────────── │  (one process)          │
 │                        │                                                 └───────────┬─────────────┘
 │  ┌──────────────────┐  │                                                             │ InnerTube / googlevideo
 │  │ IFrame preview   │──┼──── https://www.youtube.com/embed/{id} ──┐                  ▼
 │  └──────────────────┘  │                                         │          ┌───────────────────┐
 └────────────────────────┘                                         └─────────▶│      YouTube      │
                                                                               └───────────────────┘
```

- The **app** calls the server for search, metadata and downloads.
- **Preview goes straight from the app to YouTube** through the official embedded player. The server only
  says whether the video is embeddable (stream URLs are bound to the IP that extracted them, so the server
  can't hand them to the phone; see [research.md §3](research.md#3-preview--verification)).

---

## 3. Process view

```text
 ┌──────────────────────────── harmoniq-server (single Uvicorn process) ─────────────────────────────┐
 │                                                                                                    │
 │   HTTP routes (FastAPI)                       In-memory state                                      │
 │   ├─ /v1/search ──────────┐                   ├─ JobRegistry   dict[job_id → Job]                  │
 │   ├─ /v1/videos/...       │ threadpool        ├─ TTL caches    search / metadata (cachetools)      │
 │   ├─ /v1/downloads ───┐   ▼                   └─ RateLimiter   per-key counters                    │
 │   └─ /healthz, /meta  │  ytmusicapi / yt-dlp                                                       │
 │                       │                                                                            │
 │                       ▼ submit                                                                     │
 │   JobManager ── asyncio.Queue ──▶ N download workers (asyncio tasks, N = MAX_CONCURRENT_DOWNLOADS)  │
 │                                    each job: yt-dlp (in thread) → FFmpeg (subprocess) → mutagen    │
 │                                                                                                    │
 │   Background tasks: sweeper (every 10 min) · canary (hourly, optional)                             │
 └───────────────┬────────────────────────────────────────────────────────────────────────────────────┘
                 │ read/write
        ┌────────▼─────────────────────────┐          optional, Docker only / when needed
        │ data/                            │          ┌──────────────────────────────┐
        │  ├─ tmp/{job_id}/   work files   │          │ pot-provider (bgutil :4416)  │
        │  └─ files/{key}.m4a|.jpg|.json   │          │ PO tokens for yt-dlp         │
        └──────────────────────────────────┘          └──────────────────────────────┘
```

| Part | Responsibility |
|------|----------------|
| **HTTP routes** | Validation, app-token auth, rate limiting, search, metadata, job submission/status, serving finished files |
| **JobManager** | An `asyncio.Queue` plus `MAX_CONCURRENT_DOWNLOADS` (default **2**) worker coroutines. Blocking yt-dlp calls run in `asyncio.to_thread`; FFmpeg runs as an async subprocess. Owns cancellation and timeouts |
| **JobRegistry** | In-memory `dict` of jobs with their status/progress. Finished jobs are forgotten after `JOB_TTL_SECONDS` |
| **TTL caches** | `cachetools.TTLCache` for search results and video metadata |
| **FileStore** | `data/files/`: finished audio, cover art and a `.json` metadata sidecar per file. It doubles as the **dedupe cache** and survives restarts |
| **Sweeper** | Deletes expired files and orphaned temp folders; enforces the storage cap |
| **pot-provider** (optional) | Separate container that mints YouTube PO tokens. Only needed if YouTube starts bot-checking your IP ([deployment.md §6](deployment.md#6-youtube-access-hardening)) |

> **Important constraint:** state lives in memory, so the server **must run as a single process**
> (`uvicorn --workers 1`, the default). Running several workers would give each one a separate job list.
> For a few users, a single process with 2 concurrent downloads is plenty.

### What happens on restart

| Thing | After restart |
|-------|---------------|
| Finished files in `data/files/` | **Kept.** Rebuilt into the dedupe index by scanning the `.json` sidecars at startup |
| Running/queued jobs | **Lost.** `GET /v1/downloads/{id}` returns `404 job_not_found`; the app simply submits again. If the file had already finished, the resubmission is an instant cache hit |
| Search/metadata caches, rate-limit counters | Reset (harmless) |
| Temp folders | Cleaned by the sweeper on startup |

---

## 4. Folder layout

Everything lives inside `Harmoniq-server/`. Git-ignored folders are marked ✗.

```text
Harmoniq-server/
├── app/
│   ├── main.py                 # FastAPI factory, logging, lifespan (starts JobManager, sweeper, canary)
│   ├── __main__.py             # `python -m app [--dev]` → runs Uvicorn (single process) with settings from .env
│   ├── config.py               # pydantic-settings: every env var in deployment.md; tool lookup (tools/ then PATH)
│   ├── services.py             # Services container built at startup; in-memory RateLimiter; TTL caches
│   ├── api/
│   │   ├── deps.py             # app-token auth, device id, per-device and global rate limits
│   │   ├── errors.py           # AppError → RFC 9457 problem+json; request-id middleware
│   │   └── v1/
│   │       ├── search.py       # GET /v1/search
│   │       ├── videos.py       # GET /v1/videos/{id}, /preview
│   │       ├── downloads.py    # POST/GET/DELETE /v1/downloads..., /file, /artwork, /events
│   │       └── system.py       # /healthz, /readyz, /v1/meta
│   ├── domain/
│   │   ├── models.py           # Pydantic API models (SearchResult, VideoMetadata, DownloadRequest, …)
│   │   └── errors.py           # the error-code table + AppError
│   ├── providers/              # ← the only code that knows about YouTube
│   │   ├── ytmusic_search.py   # ytmusicapi song search (type=song)
│   │   ├── ytdlp_client.py     # yt-dlp: video search, metadata, download; error mapping; throttle; YouTubeHealth
│   │   ├── ytdlp_options.py    # shared YoutubeDL options: tools dir, cookies, proxy, PO token, cache dir
│   │   └── mapping.py          # raw ytmusicapi / yt-dlp dicts → API models, flags, output options
│   ├── metadata/               # ← song metadata ("Find Music" + enrichment), see server-implementation-plan.md
│   │   ├── model.py            # TrackMetadata, merge rules, MetadataContext
│   │   ├── resolver.py         # runs providers in order (METADATA_PROVIDERS), never fails a job
│   │   ├── http.py             # shared HTTP client, per-host pacing, TTL caches
│   │   ├── text.py             # title/artist normalisation and matching, non-original detection
│   │   └── providers/          # ytmusic, youtube, musicbrainz, lastfm, lyrics
│   ├── media/
│   │   ├── ffmpeg.py           # remux/transcode command, run with progress/cancel, ffprobe
│   │   ├── tags.py             # tag writers for m4a / mp3 / opus (TagLib-compatible), filename template
│   │   ├── quality.py          # quality validation: codec, duration, bitrate, sample rate, silence
│   │   └── artwork.py          # pick best thumbnail, fetch (allow-listed hosts), square-crop, JPEG (Pillow)
│   ├── jobs/
│   │   ├── manager.py          # JobManager: asyncio.Queue, workers, cancel, timeouts
│   │   ├── registry.py         # in-memory JobRegistry with TTL
│   │   ├── pipeline.py         # resolve → download → process → tag → verify → publish
│   │   └── maintenance.py      # sweeper + canary background tasks
│   └── storage/
│       └── local.py            # LocalFileStore on data/files (+ .json sidecars), sweeper logic
├── scripts/
│   ├── setup.ps1               # Windows: .venv from installed Python, deps, ffmpeg/deno into tools/
│   ├── setup.sh                # Linux/macOS: same
│   ├── run.ps1 / run.sh        # start the server from .venv with tools/ on PATH
│   └── update-ytdlp.ps1 / .sh  # upgrade yt-dlp inside .venv (see deployment.md)
├── tests/                      # unit + API (fake providers), media (real FFmpeg), live (opt-in: -m live)
├── .venv/                   ✗  # all Python libraries (venv from your installed Python, local mode)
├── tools/                   ✗  # ffmpeg, ffprobe, deno binaries (local mode)
├── data/                    ✗  # tmp/ and files/ (both modes; Docker mounts it)
├── secrets/                 ✗  # optional cookies.txt
├── .env                     ✗  # configuration
├── .env.example                # documented defaults
├── Dockerfile                  # server mode: one image with Python, FFmpeg, Deno, app
├── docker-compose.yml          # one service (+ optional pot-provider and tunnel profiles)
├── pyproject.toml              # dependencies
├── requirements.lock           # pinned versions (generated)
└── docs/                       # ← you are here
```

The code doesn't care which mode it runs in. `config.py` resolves `TOOLS_DIR` and `DATA_DIR`. In local mode they
default to `./tools` and `./data`; in Docker the tools are on `PATH` and `DATA_DIR=/data`. See
[deployment.md](deployment.md).

---

## 5. Data flow

### 5.1 Search (synchronous)

```text
App ──GET /v1/search?q=…&type=song──▶ server
  auth → rate-limit → normalise query (trim, NFC, lower for cache key)
  TTLCache lookup search:{type}:{limit}:{sha1(q)}  ── hit ──▶ 200 (X-Cache: HIT)
  provider = YTMusicSearch | YtDlpSearch, run in threadpool, timeout SEARCH_TIMEOUT_SECONDS
       map → SearchResult[]  (id, title, artists, album, duration, thumbnails, explicit, …)
       on provider error for type=song → fall back to YtDlpSearch, mark "source":"youtube"
  store in TTLCache (SEARCH_CACHE_TTL, default 6 h)
  ──200 JSON──▶ App
```

### 5.2 Metadata + preview (synchronous)

`GET /v1/videos/{id}` runs `extract_info(skip_download=True)` in a threadpool (≈1–4 s, cached for
`METADATA_CACHE_TTL`, default 1 h, because format URLs expire). It returns full metadata, the audio formats on
offer, and the flags `embeddable`, `age_restricted`, `is_live`, `downloadable`. `/preview` is a cheap subset of
the same data.

### 5.3 Download (background job)

```text
App ──POST /v1/downloads {video_id, format}──▶ server
  validate id (^[A-Za-z0-9_-]{11}$), format, per-key active-job limit
  dedupe key = sha1(video_id + format + quality + TAGGER_VERSION + overrides)
        ├─ data/files/{key}.json exists ──▶ job created already "completed" (cache hit)
        └─ identical job in flight      ──▶ return that job_id
  JobRegistry.add(job); JobManager.queue.put(job_id)
  ──202 {job_id, status:"queued", links}──▶ App

download worker (pipeline.py):
  resolving    extract_info → reject live / too long / unavailable; choose format
  downloading  yt-dlp bestaudio → data/tmp/{job_id}/   (progress hook updates the Job object)
  (parallel)   metadata resolver: YouTube Music → yt-dlp info → MusicBrainz → Last.fm → lyrics
  processing   FFmpeg remux (m4a/opus) or transcode (mp3)
  tagging      wait for metadata (bounded); artwork fetch + square crop; tags + cover written
  verifying    quality check (codec, duration ±2 s, bitrate, sample rate, not silent) + tag read-back; sha256
  publishing   atomic rename → data/files/{key}.{ext} + {key}.jpg + {key}.json
  completed    job updated; tmp dir removed

App polls GET /v1/downloads/{job_id} (or listens on /events SSE)
App ──GET /v1/downloads/{job_id}/file──▶ server streams the file (Range supported)
```

The full step-by-step with timings, retries and failure handling is in [download-flow.md](download-flow.md).

### 5.4 Job state model

```text
queued ─▶ resolving ─▶ downloading ─▶ processing ─▶ tagging ─▶ verifying ─▶ completed
   │          │             │              │            │           │
   └──────────┴─────────────┴──────────────┴────────────┴───────────┴──▶ failed (error.code)
   └──▶ cancelled (DELETE while not terminal)                 completed ─▶ expired (file swept)
```

Job object (in memory): `id, status, stage, stage_progress, progress, video_id, format, created_at,
updated_at, error, file_key, owners (device ids), attempts, cancel_event (asyncio.Event)`.

---

## 6. Storage and file lifecycle

| Path | Contents | Lifetime |
|------|----------|----------|
| `data/tmp/{job_id}/` | yt-dlp partials, intermediate files | Deleted at the end of the job, on success or failure; sweeper removes leftovers older than 1 h and everything at startup |
| `data/files/{key}.{m4a\|mp3\|opus}` | Finished, tagged audio | `FILE_TTL_SECONDS` (default **24 h**) since last access; oldest-first eviction above `STORAGE_MAX_BYTES` |
| `data/files/{key}.jpg` | Square cover art (also embedded) | Same as the audio |
| `data/files/{key}.json` | Sidecar: metadata, size, sha256, mime, created/last-access times | Same as the audio. This is the dedupe index, so it survives restarts |

- The same song requested twice (by you or a tester) is served from `data/files/` with zero YouTube traffic.
- The server is a **processing relay, not a library.** Long-term storage is the phone's music folder.
- Storage is one class, `LocalFileStore`. If object storage is ever needed, it's the only class to replace.

---

## 7. Cross-cutting concerns

| Concern | Approach |
|---------|----------|
| **Authentication** | Every request carries the secret **app token** built into the APK (`X-App-Token`, checked against `APP_TOKENS` in constant time) and a per-install `X-Device-Id`. Jobs are visible only to the device that created them. Rotating the token cuts off old APKs. Details and limits: [deployment.md §8](deployment.md#8-how-the-app-reaches-the-server) |
| **Rate limiting** | Simple in-memory counters per device: `RATE_LIMIT_SEARCH_PER_MIN` (30), `RATE_LIMIT_DOWNLOADS_PER_HOUR` (60), `MAX_ACTIVE_JOBS_PER_DEVICE` (3). Plus a global cap, `RATE_LIMIT_GLOBAL_PER_MIN` (120), as a safety net if the token leaks. It counts only requests that cause YouTube traffic (uncached search/metadata, new downloads), so status polling and file downloads never use it up. `429` with `Retry-After` |
| **Back-pressure** | When more than `QUEUE_MAX_DEPTH` (default 20) jobs are waiting: `503 queue_full` |
| **Upstream protection** | Minimum delay between YouTube extractions (`YT_MIN_INTERVAL_SECONDS`) plus yt-dlp `sleep_interval_requests`. Spacing out requests keeps the IP's reputation healthy. If YouTube bot-checks the default route, the client retries once over IPv6 and remembers what worked (`YTDLP_IP_VERSION`) |
| **Timeouts** | Search 10 s, metadata 20 s, whole job 10 min (`JOB_TIMEOUT_SECONDS`, enforced with `asyncio.wait_for`), FFmpeg subprocess timeout |
| **Observability** | Plain logs to stdout (and an optional rotating file `data/logs/server.log`) tagged with `request_id`/`job_id`. `/v1/meta` shows versions and the last canary result |
| **Health** | `/healthz` (process up), `/readyz` (data folder writable, enough free disk, FFmpeg/Deno found) |
| **Security** | Validate IDs with a regex (user strings are never passed as URLs or CLI args); yt-dlp limited to the YouTube extractor with `noplaylist`; the file route resolves keys, never raw paths; Docker container runs as non-root; reachable from outside only over HTTPS (tunnel/reverse proxy), never on plain HTTP |
| **Configuration** | `.env` file read by pydantic-settings, the same in both modes; see [deployment.md §5](deployment.md#5-environment-variables) |

---

## 8. Extension roadmap

| Feature | What changes | Storage needed |
|---------|--------------|----------------|
| **Download history** | Worker's `publishing` step appends a row (video_id, tags, format, device id, time) | **SQLite** file in `data/` (no server, still one folder) |
| **User accounts / JWT** | Replace the app-token dependency with per-user login (JWT); `users` table | SQLite; PostgreSQL only if it ever becomes multi-user at scale |
| **Lyrics** (Harmoniq already supports LRC) | `LyricsProvider` (e.g. LRCLIB) called during `tagging`; embed or return `.lrc` | None |
| **Playlist / album download** | `POST /v1/downloads/batch`: a parent job with child jobs | None |
| **Survive restarts mid-job** | Persist the job registry to SQLite or a JSON file | SQLite |
| **Many users** | Swap `JobManager` for arq/RQ + Redis, run several processes; `LocalFileStore` → S3 | Redis (+ DB) |
| **Push instead of polling** | Notify the app via FCM when a job completes | None |

None of these are needed for the personal/tester phase.
