# harmoniq-server

Self-hosted backend for **Global Search** in [**Harmoniq**](https://github.com/AnujYadav-Dev/Harmoniq), an
offline-first Android music player. The app asks this server to find music; the server returns search results and
preview info, and turns a chosen song into a high-quality, fully tagged audio file. The app then saves that file into
its music library.

**Version 0.3.0** · Python 3.11+ · FastAPI · one process, no Redis, no database · runs locally or in Docker

> For personal use only, by the owner and a few testers. Don't run it as a public service. See
> [docs/research.md §6.1](docs/research.md#61-legal--terms-of-service--read-this-first).

## What it does

| Feature | Details |
|---------|---------|
| **Search** | Songs (YouTube Music catalogue), videos (YouTube), **playlists**, or **all three at once** (`type=all`, 10 per section, fetched in parallel) |
| **Playlists** | `GET /v1/playlists/{id}`: playlist info and its tracks, in the same shape as search results |
| **Preview info** | Whether a video can play in the app's embedded YouTube player |
| **Song details before download** | `GET /v1/videos/{id}/track`: album, track and disc number, release date, genres, ISRC, artwork |
| **Downloads** | Background jobs with progress (polling or SSE), resumable file transfer and SHA-256 |
| **Audio quality** | Keeps YouTube's original stream when possible (**Opus** or **M4A**, no re-encoding), or **MP3** VBR. Checks codec, duration, bitrate, sample rate and silence before serving |
| **Metadata** | Embedded title, artists, album artist, album, track/disc n/total, full release date, genres, ISRC, square 1200 px cover and synced lyrics. Sources: YouTube Music, MusicBrainz, LRCLIB (optional Last.fm) |
| **Access control** | Secret app token (`X-App-Token`) + per-install device ID, per-device rate limits |
| **Resilience** | yt-dlp with Deno and an optional PO-token provider, IPv6 fallback when YouTube bot-checks a shared IPv4, hourly canary, cache and disk cleanup |

Full contract: [docs/api.md](docs/api.md).

## How it fits with the Harmoniq app

```text
Harmoniq app (Android) ──HTTPS + X-App-Token──▶ harmoniq-server ──▶ YouTube Music / YouTube (yt-dlp, ytmusicapi)
      ▲                                              │              MusicBrainz · LRCLIB
      └──── tagged audio file (Opus / M4A / MP3) ◀────┘
            saved to Download/Harmoniq → appears in the app's library
```

- **App repository:** [AnujYadav-Dev/Harmoniq](https://github.com/AnujYadav-Dev/Harmoniq). Its Global Search, preview
  player, downloads and Watch List use this server.
- The app finds its default server at runtime from [`config/global-search.json`](https://github.com/AnujYadav-Dev/Harmoniq/blob/main/config/global-search.json) in the
  app repository: the server URL and one of the tokens in this server's `APP_TOKENS`. Editing that file moves the
  server or rotates the token for every installed app, without a rebuild. Users can also point the app at their own
  harmoniq-server in its Settings.
- Release builds of the app only accept **HTTPS**, so a deployed server needs a stable HTTPS address (Cloudflare
  Tunnel recommended). A debug build can use your PC directly: run the server locally and
  `adb reverse tcp:8000 tcp:8000`.

## Quick start (local mode, Windows)

Needs Python 3.11+ (3.13 recommended). Everything else is downloaded into this folder.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

```powershell
scripts\run.ps1
```

The setup script creates `.venv\`, downloads FFmpeg and Deno into `tools\`, and writes `.env` with a fresh app
token, which it prints once. On Linux/macOS use `./scripts/setup.sh` and `./scripts/run.sh`.

Try it (Git Bash; the token is in `.env`):

```bash
TOKEN=$(grep '^APP_TOKENS=' .env | cut -d: -f2)
curl -s "http://localhost:8000/v1/search?q=coldplay+yellow&limit=3" -H "X-App-Token: $TOKEN"
```

`scripts\run.ps1 -Dev` adds auto-reload and Swagger UI at http://localhost:8000/docs.

## Server mode (Docker)

```bash
cp .env.example .env    # set APP_TOKENS
docker compose up -d --build
```

Optional profiles: `--profile tunnel` (Cloudflare Tunnel for HTTPS) and `--profile pot` (PO-token provider if
YouTube starts bot-checking the server).

Deploying it for the app for the first time (host choice, Docker, `.env`, Cloudflare Tunnel, verification,
updates, token rotation): follow [docs/deployment-guide.md](docs/deployment-guide.md).

Once it's reachable over HTTPS, put its URL and token into the app repository's
[`config/global-search.json`](https://github.com/AnujYadav-Dev/Harmoniq/blob/main/config/global-search.json) (see the
[app README](https://github.com/AnujYadav-Dev/Harmoniq#connecting-global-search-to-your-server-optional)).

## Keeping it working

YouTube changes often. When downloads start failing, update yt-dlp first:

```powershell
scripts\update-ytdlp.ps1
```

Then restart the server. In Docker: `docker compose build --no-cache && docker compose up -d`.

## Tests

```powershell
.venv\Scripts\python -m pip install pytest
```

```powershell
.venv\Scripts\python -m pytest
```

That runs the offline suite: unit tests, API tests with fake YouTube providers, and real FFmpeg/tagging tests.
`.venv\Scripts\python -m pytest -m live` also runs end-to-end tests against real YouTube.

## Documentation

| Doc | Contents |
|-----|----------|
| [docs/README.md](docs/README.md) | Overview and reading order |
| [docs/api.md](docs/api.md) | Endpoints, JSON, error codes |
| [docs/architecture.md](docs/architecture.md) | Design, code layout, job lifecycle |
| [docs/download-flow.md](docs/download-flow.md) | Search → preview → download → FFmpeg → tags |
| [docs/deployment-guide.md](docs/deployment-guide.md) | Step-by-step first deployment: Docker + Cloudflare Tunnel, verify, update, rotate, move, troubleshoot |
| [docs/deployment.md](docs/deployment.md) | Reference: local/Docker setup, env vars, HTTPS for the app, 24/7 |
| [docs/android-integration-plan.md](docs/android-integration-plan.md) | Global Search in the app: design, status, deviations |
| [docs/research.md](docs/research.md) | Library research and why this stack |
| [docs/server-implementation-plan.md](docs/server-implementation-plan.md) | Metadata and audio-quality design (what gets embedded and why) |

## Related repositories

| Repository | What it is |
|------------|------------|
| [**Harmoniq**](https://github.com/AnujYadav-Dev/Harmoniq) | The Android music player that uses this server for Global Search |
| [**Harmoniq-server**](https://github.com/AnujYadav-Dev/Harmoniq-server) (this repo) | This backend |

Licence: AGPL-3.0-or-later, like Harmoniq (declared in `pyproject.toml`; the repository has no `LICENSE` file yet).
