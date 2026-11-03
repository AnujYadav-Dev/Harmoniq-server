# Research: YouTube search, preview and audio extraction

[← Docs index](README.md)

> Research date: **2026-11-02**. The YouTube extraction ecosystem changes every few weeks. Re-check the
> "Version snapshot" table and the [risk register](#6-risks-and-limitations) before you implement, and at
> least once per quarter after that.

This document evaluates the open-source options for each part of the feature, then gives the recommended
dependency set and explains why each piece was chosen.

---

## 1. Requirements the options were measured against

| # | Requirement | What it means in practice |
|---|-------------|---------------------------|
| R1 | Search YouTube by free-text query | Return title, channel/artist, duration, thumbnail, video ID; ideally "songs" vs "videos" |
| R2 | Preview / verify before download | The phone plays the video in an official embedded player |
| R3 | Extract audio | Get the best audio stream, put it in a container Android plays, with no avoidable quality loss |
| R4 | Return audio and full metadata | Tags (title, artist, album, year, cover art) embedded in the file *and* returned as JSON |
| R5 | Run 24/7 for the owner and a few testers | Survives YouTube changes, bot detection, crashes, full disks; restarts cleanly |
| R6 | Python + FastAPI, Docker-ready, Postgres only if needed | Fits the stated stack |
| R7 | Permissive or AGPL-compatible licences | Harmoniq is AGPL-3.0, so GPL/AGPL dependencies are acceptable; proprietary ones are not |
| R8 | Personal scale: owner + a few testers, minimal resources, no Redis | One process; runs from a local venv or one Docker container |

---

## 2. Search and metadata

### 2.1 Options evaluated

| Option | Type | Licence | Needs API key | Maintenance (Nov 2026) | Verdict |
|--------|------|---------|---------------|------------------------|---------|
| **yt-dlp** `ytsearchN:` / `ytsearchdate:` with `extract_flat` | Python library + CLI (InnerTube scraping) | Unlicense (public domain) | No | Very active: releases every few days (2026.10.26 latest stable) | **Use for video search and for all per-video metadata** |
| **ytmusicapi** (`sigma67/ytmusicapi`) | Python library for the YouTube Music InnerTube API | MIT | No (unauthenticated search works) | Active; 1.x series. Sources disagree on the newest version (1.8.2 / 1.11.x), so pin after checking PyPI | **Use for "songs" search** |
| YouTube Data API v3 (`search.list`) | Official REST API | Google API ToS | Yes | Official | Fallback only. Default quota is 10,000 units/day and `search.list` costs 100 units, so about **100 searches per day per project**. Its ToS also forbid the download part of this feature (see §6.1) |
| youtube-search-python | Python, InnerTube scraping | MIT | No | **Archived since 2022** | Reject |
| yt-search-python / innertubei / ytlookup | Forks/successors of the above | Various | No | Small, single-maintainer projects | Reject: low bus-factor, nothing yt-dlp/ytmusicapi doesn't already do |
| tombulled/innertube | Generic low-level InnerTube client | MIT | No | Slow | Reject for v1: we would rebuild parsing that ytmusicapi/yt-dlp already maintain |
| pytubefix | Python download + search library (pytube fork) | MIT | No | Active (11.1.0, Sept 2026) | Viable second choice; smaller community than yt-dlp and slower to react to YouTube changes |
| Invidious / Piped public APIs | Alternative front-ends with JSON APIs | AGPL-3.0 | No | Public instances heavily blocked; Invidious down to a handful | Reject as a dependency: we'd inherit their blocking problems plus an extra network hop |

### 2.2 Why two search providers

YouTube has two different search surfaces, and each one answers a different question:

- **YouTube Music search (ytmusicapi, `filter="songs"`)** returns *tracks*: clean `title`, a separate list of
  `artists`, `album`, `duration`, `isExplicit`, square album art, and a `videoId` that yt-dlp can download.
  This is what a music app usually wants. It produces much better tags than parsing "Artist - Title (Official
  Video) [4K]" out of a video title.
- **YouTube search (yt-dlp `ytsearchN:` with `extract_flat=True`)** returns *videos*: covers, live sessions,
  remixes and uploads that aren't in the Music catalogue. It is cheap (one request, no per-video extraction)
  and needs no extra dependency, because yt-dlp is needed for downloading anyway.

The API exposes this as `type=song|video` (see [api.md](api.md#get-v1search)). Both providers sit behind a
`app/providers/` module, so the Data API or another source can be added later without touching the routes.

### 2.3 Per-video metadata

For the selected video, `yt_dlp.YoutubeDL({"skip_download": True}).extract_info(url)` returns everything we need:
`title`, `channel`, `uploader`, `duration`, `upload_date`, `thumbnails[]`, `view_count`, `like_count`, `tags`,
`categories`, `chapters`, `age_limit`, `is_live`, `availability`, and the full `formats[]` list (so we know the
audio bitrate and codec before downloading). For Music tracks, yt-dlp also fills `track`, `artist`, `album` and
`release_year` when YouTube provides them.

---

## 3. Preview / verification

### 3.1 Options evaluated

| Option | How it works | Verdict |
|--------|--------------|---------|
| **Official IFrame Player in the app** (via `PierfrancescoSoffritti/android-youtube-player`, Apache-2.0, v12.x) | The app embeds `https://www.youtube.com/embed/{id}` in a WebView managed by the library | **Recommended.** It is the only approach YouTube explicitly allows, it costs the server nothing, and the library already handles the WebView, origin and lifecycle details |
| Hand-rolled WebView + IFrame API | Same as above without the library | Works, but since late 2025 YouTube returns **error 153** when the embed has no `Referer`/client identity. The WebView must use `loadDataWithBaseURL()` with a real https base URL. The library handles this, which is another reason to use it |
| Server proxies a short audio clip | Server downloads N seconds and streams it | Reject for v1: it uses the same bot-detection budget as a full download and moves the ToS problem onto the preview step |
| Give the app the `googlevideo.com` stream URL | Server extracts the direct URL; the phone plays it | **Does not work.** Stream URLs are signed for the IP that requested them (and need PO tokens), so the phone gets 403 |
| Invidious/Piped proxied playback | Third-party proxy | Reject (see §2.1) |

### 3.2 What the backend contributes to preview

The backend doesn't stream preview media. It returns what the app needs to decide *whether* and *how* to preview:
`embeddable` (some uploaders disable embedding, which shows **error 101/150**), `age_restricted` (embeds of
age-gated videos need sign-in), `is_live`, `embed_url` and `watch_url`. If `embeddable=false`, the app should
fall back to the thumbnail + metadata card, or open the video in the YouTube app or browser.

---

## 4. Audio extraction

### 4.1 Downloader options

| Option | Licence | Strengths | Weaknesses | Verdict |
|--------|---------|-----------|------------|---------|
| **yt-dlp** | Unlicense | De facto standard (one of the most-starred projects on GitHub, hundreds of contributors). Fastest response to YouTube changes. Python API with progress hooks. Built-in FFmpeg post-processors, metadata/thumbnail embedding, cookies, proxy, rate-limit and PO-token plugin system | Since **2025.11.12** it needs an external **JS runtime (Deno recommended)** plus the pinned `yt-dlp-ejs` package to solve YouTube's player challenges. Breaks for hours or days at a time when YouTube changes, so updating must be cheap | **Recommended** |
| pytubefix | MIT | Pure Python, no JS runtime | Smaller team; historically several days behind yt-dlp when YouTube changes; no plugin ecosystem for PO tokens | Fallback candidate only |
| youtube-dl | Unlicense | — | Effectively unmaintained for YouTube | Reject |
| NewPipeExtractor | GPL-3.0, Java | Runs on the *phone*, which avoids datacenter-IP blocking entirely | Java, not Python; conflicts with the server-side design asked for | Not for the backend. Noted in §7 as a future on-device fallback |
| Cobalt (`imputnet/cobalt`) | AGPL-3.0, Node.js | Self-hostable "download API" service with a similar purpose | Node, not Python; its YouTube support has been unstable and depends on the same tricks yt-dlp uses | Use as a design reference only |

### 4.2 Required companions for yt-dlp on YouTube (2026)

| Component | Why it's needed | Licence |
|-----------|-----------------|---------|
| **Deno ≥ 2.x** (or Node / Bun / QuickJS) | Runs YouTube's player JavaScript to solve the signature and `n` challenges. Without it, yt-dlp warns that YouTube support is deprecated and many formats disappear | MIT |
| **`yt-dlp-ejs`** (install as `yt-dlp[default]`) | The challenge-solver scripts. Each yt-dlp release pins one exact version and rejects any other | Unlicense (verify on PyPI) |
| **`bgutil-ytdlp-pot-provider`** (plugin + HTTP sidecar container, port 4416) | Generates **PO tokens**. YouTube increasingly requires them, especially from flagged/datacenter IPs. Tokens are bound to the video ID, so one is minted per video | GPL-3.0 (verify) |
| Cookies file (optional) | Logged-in session from a throwaway account. Unlocks age-restricted videos and sometimes passes bot checks | — |
| Proxy (optional) | Residential/ISP proxy when the server's IP is flagged | — |

### 4.3 Audio processing: FFmpeg + mutagen

- **FFmpeg** (LGPL-2.1+, or GPL for builds with GPL codecs) does the remuxing and, when asked, transcoding, plus
  thumbnail conversion. yt-dlp calls it through its `FFmpegExtractAudio` post-processor. Use the distro package
  (Debian 13 "trixie", the base of current `python:*-slim` images, ships FFmpeg 7.1) or the static builds from `yt-dlp/FFmpeg-Builds`, which include patches yt-dlp
  relies on. **ffprobe** must also be installed.
- **mutagen** (GPL-2.0-or-later) writes the tags *we* control: title, artist, album, year, square-cropped cover
  art, and a `comment`/custom frame holding the source video ID. yt-dlp's `--embed-metadata` also uses
  mutagen/FFmpeg, but setting tags ourselves gives deterministic, Music-quality tags. The Android app's
  `:metaphony` module (TagLib) reads MP4, ID3v2 and Vorbis-comment tags, so the result shows up correctly in the
  library.

### 4.4 Output format decision

What YouTube offers (typical): **Opus ~130–160 kbps in WebM (itag 251)** and **AAC-LC 128 kbps in MP4 (itag 140)**.
Harmoniq plays through `android.media.MediaPlayer` with `minSdk 28`.

| Output | Process | Quality | Android 9+ playback | CPU | Verdict |
|--------|---------|---------|---------------------|-----|---------|
| **`m4a` (AAC)** | **Remux** itag 140, no re-encode | Bit-identical to the source | Yes, everywhere | ~0 | **Default** |
| `opus` (Ogg Opus) | Remux itag 251 from WebM into Ogg | Best source quality | Ogg-Opus is only guaranteed on Android 10+ | ~0 | Opt-in (`format=opus`) |
| `mp3` | Transcode (lossy → lossy) | Slight generational loss | Yes, everywhere | High (~1–3 s CPU per minute of audio) | Opt-in for maximum compatibility |

Never upscale. Transcoding 128 kbps AAC to "320 kbps MP3" only makes the file bigger. MP3 output uses
`-q:a 0` (VBR ~245 kbps) or a fixed 192 kbps.

---

## 5. Backend building blocks

| Concern | Choice | Licence | Why | Alternatives considered |
|---------|--------|---------|-----|-------------------------|
| HTTP API | **FastAPI** + Uvicorn (single process) | MIT / BSD | Required stack; async; OpenAPI generated for free (useful for the Android client) | — |
| Settings | **pydantic-settings** | MIT | Typed env-var config | — |
| Background jobs | **In-process `asyncio` job manager** (standard library: `asyncio.Queue`, worker tasks, `asyncio.to_thread`) | — | For one owner and a few testers, a separate queue is pure overhead. Jobs run inside the FastAPI process with bounded concurrency. No extra service to install or keep running | arq / RQ / Celery + Redis: only if it ever needs many users (the `JobManager` interface allows swapping later). FastAPI `BackgroundTasks`: no concurrency limit, queue or cancellation |
| Caches | **cachetools** `TTLCache` (in memory) | MIT | Search and metadata caching; losing it on restart is harmless | Redis (not needed) |
| Rate limiting | **Small in-memory counter** in the auth dependency | — | Mostly to protect your own IP from accidental bursts | `slowapi` (fine too, but more than needed) |
| Persistence | **None.** Finished files + JSON sidecars in `data/` are the only state. If history is wanted later: **SQLite** (a file in `data/`) | — | Keeps the "one folder" setup. PostgreSQL would be a second server to run with no benefit at this scale | PostgreSQL (only for real multi-user scale) |
| Local environment | **venv + pip** from your installed Python (≥ 3.11) | — | Standard tooling, nothing extra to learn; all packages stay in `Harmoniq-server/.venv/` | uv (faster, and could also put Python itself in the folder; can be adopted later) |
| App → server authentication | **Shared secret app token** in the APK (`X-App-Token`) + per-install device ID | — | Testers just install the APK; no accounts or extra apps | Per-user login (later); Tailscale (rejected: every tester would need to install it) |
| HTTPS exposure | **Cloudflare Tunnel** (`cloudflared`) | Apache-2.0 | Stable HTTPS URL with no open router ports, works the same on any host | Caddy reverse proxy (good on a VPS) |

### Reference projects (studied, not used as dependencies)

| Project | Licence | What it teaches us |
|---------|---------|--------------------|
| **MeTube** (`alexta69/metube`) | AGPL-3.0 | yt-dlp web UI in Docker: job queue, progress reporting, audio-only output directory |
| **spotDL** | MIT | Same pipeline idea for music: ytmusicapi to *match* songs, yt-dlp to download, mutagen to tag. Confirms the recommended stack |
| **ytdl-sub** | GPL-3.0 | yt-dlp + FFmpeg presets, robust tagging and file naming |
| **Cobalt** | AGPL-3.0 | Shape of a public "give me the media" API; shows what happens operationally when YouTube blocks a public instance |
| **Invidious / Piped** | AGPL-3.0 | How public YouTube proxies get blocked; why a **small, authenticated, private** deployment is more durable |

---

## 6. Risks and limitations

### 6.1 Legal / terms of service — read this first

- **YouTube's Terms of Service** forbid downloading content unless YouTube shows a download button/link or gives
  written permission. The **YouTube API Services Developer Policies** separately forbid API clients from
  downloading, caching or storing audiovisual content and from "making content available for offline playback".
- This means:
  1. The **official Data API must not be used for the download feature.** If it is used for search, the
     project accepts those API terms, which the download feature then violates. That is the main reason the
     recommended stack uses no official API.
  2. Using yt-dlp doesn't make downloading permitted. It only means no API agreement was signed. Copyright on the
     audio stays with the rights holders.
- **Project decision (owner, 2026-11-02):** harmoniq-server is for **personal use only**. It runs privately, is
  used by the owner and a few testers, and will **not be made public** or offered as a service. This is the
  lowest-risk way to run a tool like this. To keep it that way:
  - testers get the server only through the APK you give them. Every request needs the secret app token built
    into the APK, the URL isn't published anywhere, and traffic is HTTPS only (see
    [deployment.md §8](deployment.md#8-how-the-app-reaches-the-server));
  - the download feature stays out of any Google Play build (Play policy rejects apps that download YouTube
    content). Sideloaded/GitHub APKs, as Harmoniq ships today, are unaffected.
- Harmoniq is **AGPL-3.0**. If the server is also AGPL-3.0 (recommended, which also allows the GPL deps), the
  testers you let use it are entitled to its source. In practice that's satisfied by the source being in your repo.

### 6.2 Technical risk register

| Risk | Likelihood | Impact | Mitigation (where documented) |
|------|-----------|--------|-------------------------------|
| YouTube changes break extraction | **High** (several times a year) | Downloads fail until yt-dlp ships a fix (hours to days) | Keep yt-dlp updatable without code changes; one-command update script / image rebuild; canary job ([deployment.md §7](deployment.md#7-keeping-yt-dlp-current)) |
| **Datacenter IP flagged** ("Sign in to confirm you're not a bot") | **High** on cloud VPSs | All downloads fail | Run at home (**residential IP**); optional PO-token provider; optional cookies; optional residential proxy ([deployment.md §6](deployment.md#6-youtube-access-hardening)) |
| **Shared (CGNAT) IPv4 flagged** at home | **Seen on the dev machine** (mobile ISP) | Bot-check over IPv4 while IPv6 works | Automatic IPv6 retry (`YTDLP_IP_VERSION=auto`), IPv6 enabled in compose ([deployment.md §6](deployment.md#6-youtube-access-hardening)) |
| SABR-only formats for some clients | Medium | Fewer or no formats | Keep yt-dlp current; it picks player clients automatically. Let the PO-token provider handle the web client |
| Cookies expire or account gets flagged | Medium | Age-restricted videos fail | Use a throwaway account; detect `login_required` and alert; never use a personal account |
| Disk fills with audio files | Medium | Server outage | Short TTL, size cap, periodic sweeper, dedupe cache ([architecture.md §6](architecture.md#6-storage-and-file-lifecycle)) |
| Long videos / livestreams abuse resources | Medium | CPU/disk/bandwidth | `MAX_DURATION_SECONDS`, reject `is_live`, per-key concurrency limits |
| Unofficial search APIs change shape | Medium | Search returns errors | Two providers with fallback; contract tests in CI; pinned versions |
| GPL/AGPL obligations | Low | Licence non-compliance | Server licensed AGPL-3.0, publish source |

---

## 7. Recommendation

### Recommended dependency set

| Layer | Dependency | Pin strategy |
|-------|-----------|--------------|
| Language | **Python ≥ 3.11, 3.13 recommended** (yt-dlp dropped 3.10 in Oct 2026) | Your installed Python (local) / base image tag (Docker) |
| API | **FastAPI**, **Uvicorn**, **pydantic v2 / pydantic-settings** | Lock file |
| Search (songs) | **ytmusicapi** | Lock file; bump monthly |
| Search (videos) + metadata + download | **yt-dlp[default]** (brings the pinned `yt-dlp-ejs`) | **Not** locked tightly. Track latest stable; see deployment |
| JS runtime | **Deno** | Downloaded into `tools/` (local) / image build (Docker) |
| PO tokens (optional) | **bgutil-ytdlp-pot-provider** plugin + its container, enabled only if YouTube bot-checks you | Plugin version matched to the container version |
| Audio | **FFmpeg + ffprobe** | Downloaded into `tools/` (local) / image build (Docker) |
| Tagging | **mutagen**, **Pillow** (cover art square crop/convert) | Lock file |
| Jobs | **asyncio** (standard library) in-process `JobManager` | — |
| Cache | **cachetools** | Lock file |
| HTTP client (thumbnails) | **httpx** | Lock file |
| Environment / packaging | **venv + pip** (local), **Docker** single container (server), **Cloudflare Tunnel** for HTTPS | — |
| App-side preview (later, Android) | **android-youtube-player** (Apache-2.0) | Not part of this repo |

### Why this is the best combination

1. **yt-dlp is the only option with the maintenance speed YouTube requires.** Every alternative either ships
   fixes days later or depends on yt-dlp's work. It also covers search, metadata, download and FFmpeg
   post-processing in one dependency.
2. **ytmusicapi adds what yt-dlp lacks for a music app:** structured artist/album metadata and song-only results.
   spotDL uses the same pairing, and it holds up in production.
3. **No official API** avoids a 100-searches-per-day quota and avoids signing terms that explicitly forbid the
   feature.
4. **Remux-first audio (m4a default)** gives the best quality per byte with almost no CPU, so one small server
   can handle the load easily, even a laptop.
5. **One process, no infrastructure.** An in-process asyncio job manager, in-memory caches and plain files cover
   one owner and a few testers. There's no Redis, broker or database to install, update or keep alive, and the
   whole server lives in one folder. The internals sit behind interfaces, so a queue or DB can be added later
   without changing the API.
6. **Everything is open source and AGPL-compatible**, matching Harmoniq's own licence.

### Considered and set aside for later

- **On-device extraction (NewPipeExtractor in the app)** is the most robust option against IP blocking, because each
  phone uses its own residential IP. It's out of scope (the brief asks for a backend), but the API is designed so
  the app could later fall back to it for search or download without changing the UX.

---

## Version snapshot (2026-11-02)

| Package | Latest seen | Note |
|---------|-------------|------|
| yt-dlp | 2026.10.26 (stable) | Requires Python ≥ 3.11 and a JS runtime for YouTube |
| ytmusicapi | 1.x (1.8.2–1.11.x reported) | Confirm on PyPI before pinning |
| pytubefix | 11.1.0 | Fallback only |
| android-youtube-player | 12.1.x | App side, later |
| bgutil-ytdlp-pot-provider | 1.x (PyPI), Rust rewrite 0.8.x | Extractor-arg names differ between implementations. Match the plugin to the sidecar |

## Sources

- yt-dlp: [JS runtime announcement #15012](https://github.com/yt-dlp/yt-dlp/issues/15012), [upcoming requirements #14404](https://github.com/yt-dlp/yt-dlp/issues/14404), [releases](https://github.com/yt-dlp/yt-dlp/releases), [2026.10.21 release](https://github.com/yt-dlp/yt-dlp/releases/tag/2026.10.21), [PyPI](https://pypi.org/project/yt-dlp/), [PO Token Guide](https://github.com/yt-dlp/yt-dlp/wiki/PO%20Token%20Guide), [SABR issue #16082](https://github.com/yt-dlp/yt-dlp/issues/16082), [VideoHelp changelog](https://www.videohelp.com/software/yt-dlp)
- PO tokens: [bgutil-ytdlp-pot-provider (PyPI)](https://pypi.org/project/bgutil-ytdlp-pot-provider/0.6.0), [Rust version (crates.io)](https://crates.io/crates/bgutil-ytdlp-pot-provider), [yt-dlp-getpot-wpc](https://pypi.org/project/yt-dlp-getpot-wpc/)
- Datacenter blocking: [DEV: cloud video service & IPs](https://dev.to/osovsky/i-was-building-a-cloud-video-service-youtube-turned-me-into-an-ip-trafficker-1l9o), [MusicBot wiki: YouTube Access](https://github.com/umutxyp/MusicBot/wiki/YouTube-Access), [TechNetExperts fix guide](https://www.technetexperts.com/fix-yt-dlp-cloud-signin-error/)
- Search: [ytmusicapi GitHub](https://github.com/sigma67/ytmusicapi), [ytmusicapi releases](https://github.com/sigma67/ytmusicapi/releases), [ytmusicapi PyPI](https://pypi.org/project/ytmusicapi), [tombulled/innertube](https://www.github.com/tombulled/innertube), [yt-search-python](https://pypi.org/project/yt-search-python), [pytubefix (piwheels)](https://www.piwheels.org/project/pytubefix/)
- Official API: [Data API quota costs](https://developers.google.com/youtube/v3/determine_quota_cost), [Data API overview](https://developers.google.com/youtube/v3/getting-started), [Developer Policies](https://developers.google.com/youtube/terms/developer-policies)
- Embedding: [IFrame API revision history (error 153)](https://developers.google.com/youtube/iframe_api_revision_history), [Simon Willison on error 153](https://simonwillison.net/2025/Dec/1/youtube-embed-153-error/), [Iframely on WebView](https://iframely.com/help/501993-you-tube-error-153-in-android-web-view), [Kickstarter Android fix](https://github.com/kickstarter/android-oss/pull/2446), [android-youtube-player](https://repos.ecosyste.ms/hosts/GitHub/repositories/PierfrancescoSoffritti/android-youtube-player)
- Alternatives: [Invidious vs Piped 2026](https://sumguy.com/invidious-piped-redlib-nitter-2026/), [Invidious blocking issue](https://www.github.com/iv-org/invidious/issues/3822), [MeTube](https://github.com/alexta69/metube), [ytdl-sub](https://github.com/jmbannon/ytdl-sub/releases/tag/2026.07.17.post1)
