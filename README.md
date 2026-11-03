# harmoniq-server

Personal backend for the [Harmoniq](../Harmoniq) Android music player. It searches YouTube Music / YouTube, says
whether a video can be previewed, and turns a chosen video into a tagged audio file (m4a by default) with cover
art, ready for the app to save into its music folder.

One Python process. No Redis, no database. Everything lives in this folder.

> For personal use only, by the owner and a few testers. Don't run it as a public service. See
> [docs/research.md §6.1](docs/research.md#61-legal--terms-of-service--read-this-first).

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

The Harmoniq app's Global Search uses this server. The app reads the server URL and token at build time; see the
app's `docs/09-global-search-server.md`.

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

Licence: AGPL-3.0-or-later, like Harmoniq (declared in `pyproject.toml`; the repository has no `LICENSE` file yet).
