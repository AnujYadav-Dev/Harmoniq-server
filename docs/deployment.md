# Deployment

[← Docs index](README.md)

How to run harmoniq-server: locally from a self-contained Python environment, or on a server with Docker. Also
covers environment variables, YouTube-access hardening, keeping yt-dlp current, how the app reaches the server,
and keeping it running 24/7.

This is the **reference**. For a first deployment, follow the step-by-step
[deployment-guide.md](deployment-guide.md) (Docker Compose + Cloudflare Tunnel, verification, connecting the app,
updates, token rotation, moving machines, troubleshooting).

> Status: **implemented and tested** on Windows, in both local mode and Docker (Docker Desktop): setup, run,
> live search and downloads in all three formats, restart and cache behaviour.

---

## 1. Two ways to run it

| | **Local mode** (your PC, development, personal use) | **Server mode** (Docker) |
|---|---|---|
| Runs as | `python -m app` from the project's own `.venv` | One Docker container |
| Python + libraries | Your installed Python (≥ 3.11, 3.13 recommended); all libraries in `Harmoniq-server/.venv/` | Inside the image |
| FFmpeg, ffprobe, Deno | Downloaded into `Harmoniq-server/tools/` | Installed in the image |
| Downloads / work files | `Harmoniq-server/data/` | `Harmoniq-server/data/` mounted at `/data` |
| Config | `Harmoniq-server/.env` | The same `.env` |
| Installs anything system-wide? | **No** (only uses the Python you already have) | Only Docker itself |

Both modes run **the same code with the same `.env`**. Only `TOOLS_DIR`/`DATA_DIR` differ, and they have sensible
defaults in each mode.

Requirements: local mode needs **Python ≥ 3.11** already installed (3.13 recommended; on Windows the `py` launcher
from python.org works). The setup script fetches everything else. Server mode needs Docker Engine + the Compose
plugin.

---

## 2. Where to host it

The biggest operational factor isn't CPU. It's **how YouTube treats the machine's IP address.**

| Host | IP reputation with YouTube | Recommendation |
|------|----------------------------|----------------|
| **Your own PC / home server** (mini PC, NAS, Raspberry Pi 5, old laptop) | **Good**: normal household IP | **Best for this project.** Use local mode or Docker |
| VPS / cloud VM | **Poor**: datacenter IPs are often bot-checked ("Sign in to confirm you're not a bot") | Works only with the PO-token provider, and often also a paid residential proxy |
| Serverless / PaaS (Cloud Run, Lambda, Vercel…) | Poor + shared IPs | **Not suitable** |

Resources for one owner + a few testers: **1–2 CPU cores, 1 GB RAM, ~5 GB free disk**. Each running download uses
about 150–300 MB RAM; an mp3 encode uses one core while it runs.

---

## 3. Local mode: everything in one folder

### 3.1 Layout after setup

```text
Harmoniq-server/
├── .venv/        ✗  virtual environment made from your installed Python; all libraries live here:
│                    fastapi, uvicorn, yt-dlp[default], ytmusicapi, mutagen, Pillow, httpx, cachetools, …
├── tools/        ✗  ffmpeg(.exe), ffprobe(.exe), deno(.exe)
├── data/         ✗  tmp/, files/, logs/
├── secrets/      ✗  cookies.txt (optional)
├── .env          ✗  your configuration (copied from .env.example)
└── app/, scripts/, docs/, …   (committed)
```

✗ = git-ignored. The only thing outside the folder is the Python interpreter you already have. Deleting the folder
removes everything else. To move to another machine, copy the folder minus `.venv/` and re-run setup there (a venv
can't be moved between machines).

### 3.2 First-time setup

Windows (PowerShell):

```powershell
cd Harmoniq-server
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

Linux / macOS:

```bash
cd Harmoniq-server && ./scripts/setup.sh
```

What the setup script does (idempotent, so it's safe to re-run):

1. Finds your Python (`py -3.13`, then `py -3`, then `python` on Windows; `python3` elsewhere; or `-Python <path>`)
   and checks it's **≥ 3.11**. It stops with a clear message if not.
2. Creates `.venv/` with `python -m venv .venv`, upgrades pip, and installs `requirements.lock` + the latest
   `yt-dlp[default]` (which brings the matching `yt-dlp-ejs` challenge solver). Packages go into `.venv/` only.
3. Downloads **FFmpeg + ffprobe** into `tools/`:
   - Windows: the `ffmpeg-master-latest-win64-gpl` zip from `yt-dlp/FFmpeg-Builds` (the build yt-dlp is tested with)
   - Linux: the matching `linux64-gpl` / `linuxarm64-gpl` tarball from the same releases
   - macOS: the static builds from evermeet.cx, or `brew install ffmpeg` if you prefer
4. Downloads **Deno** (single binary) into `tools/` from the official GitHub release for your OS/CPU.
5. Creates `data/`, `secrets/`, and `.env` from `.env.example` if missing, with a freshly generated `APP_TOKENS`
   secret.
6. Prints the versions of everything (`python`, `yt-dlp`, `ffmpeg`, `deno`) as a self-check.

### 3.3 Running

```powershell
scripts\run.ps1            # Windows
```

```bash
./scripts/run.sh           # Linux / macOS
```

The run script puts `tools/` first on `PATH` and runs `.venv`'s Python with `-m app` (no need to "activate"
anything). That starts Uvicorn on `HOST:PORT` from `.env` (default `0.0.0.0:8000`) as a **single process**. The
app also passes the tool locations to yt-dlp explicitly (`ffmpeg_location`, Deno path), so it works even if `PATH`
isn't set up.

For development, `scripts/run.ps1 -Dev` adds `--reload` and enables `/docs` (Swagger UI).

---

## 4. Server mode: Docker

### 4.1 `Dockerfile`

```dockerfile
# syntax=docker/dockerfile:1.7
# Server mode: one image with Python, FFmpeg, Deno and the app (docs/deployment.md §4).
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DENO_INSTALL=/usr/local \
    DENO_DIR=/data/cache/deno \
    DATA_DIR=/data \
    TOOLS_DIR=""

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl unzip tini \
 && curl -fsSL https://deno.land/install.sh | sh -s -- --yes \
 && apt-get purge -y unzip && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.lock ./
RUN pip install -r requirements.lock

# yt-dlp in its own layer so it can be refreshed cheaply:
#   docker compose build --no-cache                       (latest)
#   docker compose build --build-arg YTDLP_SPEC="yt-dlp[default]==2026.10.26"
ARG YTDLP_SPEC="yt-dlp[default]"
RUN pip install --upgrade "$YTDLP_SPEC" \
 && ffmpeg -hide_banner -version | head -1 && ffprobe -hide_banner -version | head -1 \
 && deno --version | head -1 && python -c "import yt_dlp; print('yt-dlp', yt_dlp.version.__version__)"

COPY app ./app
RUN mkdir -p /data && chmod 0777 /data
EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "app"]
```

The image is about 1.1 GB, mostly Debian's FFmpeg package and Deno. The container runs as the user set in compose
(`HARMONIQ_UID`/`HARMONIQ_GID`, default 1000), so files it writes to `./data` belong to you on a Linux host.

### 4.2 `docker-compose.yml`

```yaml
name: harmoniq

services:
  server:
    build: .
    image: harmoniq-server:latest
    env_file: .env
    environment:
      DATA_DIR: /data
      TOOLS_DIR: ""
    user: "${HARMONIQ_UID:-1000}:${HARMONIQ_GID:-1000}"   # owner of ./data on Linux hosts
    ports:
      - "127.0.0.1:8000:8000"       # only the tunnel / reverse proxy on this host talks to it
    volumes:
      - ./data:/data
      - ./secrets:/run/secrets:ro   # optional cookies.txt → YTDLP_COOKIES_FILE=/run/secrets/cookies.txt
    restart: unless-stopped
    stop_grace_period: 1m
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://localhost:8000/healthz"]
      interval: 30s
      timeout: 5s
      retries: 3
    logging:
      driver: json-file
      options: { max-size: "10m", max-file: "3" }

  # Optional: only if YouTube starts bot-checking this machine (docs/deployment.md §6).
  # Enable with:  docker compose --profile pot up -d   and set POT_PROVIDER_URL=http://pot-provider:4416
  pot-provider:
    image: brainicism/bgutil-ytdlp-pot-provider:latest   # keep in step with the plugin version in requirements.lock
    profiles: ["pot"]
    restart: unless-stopped
    init: true

  # HTTPS for the app via Cloudflare Tunnel (docs/deployment.md §8).
  # Enable with:  docker compose --profile tunnel up -d
  # Create the tunnel in the Cloudflare dashboard, route harmoniq.<your-domain> → http://server:8000,
  # and put its token in .env as CLOUDFLARE_TUNNEL_TOKEN.
  tunnel:
    image: cloudflare/cloudflared:latest
    profiles: ["tunnel"]
    command: tunnel --no-autoupdate run
    environment:
      TUNNEL_TOKEN: ${CLOUDFLARE_TUNNEL_TOKEN:-}
    restart: unless-stopped

# IPv6 for containers. Many home/mobile ISPs share one IPv4 address between customers (CGNAT) and YouTube
# often bot-checks those, while the same connection's IPv6 works. With YTDLP_IP_VERSION=auto (default) the
# server retries over IPv6 when that happens (docs/deployment.md §6).
# Docker Desktop: works out of the box. Linux: needs Docker 27+ and IPv6 on the host.
networks:
  default:
    enable_ipv6: true
```

```bash
cp .env.example .env        # then set APP_TOKENS etc.
docker compose up -d --build
docker compose logs -f server
curl -fsS http://localhost:8000/readyz
```

With the `pot` profile enabled, set `POT_PROVIDER_URL=http://pot-provider:4416` in `.env`. The PO-token plugin
(`bgutil-ytdlp-pot-provider`) is installed in the image but does nothing unless that URL is set.

---

## 5. Environment variables

One `.env` file, used by both modes. `.env.example` documents every variable with these defaults.

### Core

| Variable | Default | Description |
|----------|---------|-------------|
| `HOST` / `PORT` | `0.0.0.0` / `8000` | Listen address |
| `APP_ENV` | `production` | `development` enables `/docs` and verbose errors |
| `LOG_LEVEL` | `INFO` | |
| `APP_TOKENS` | — **required** | The secret token(s) built into the APK. Comma-separated `id:secret` pairs, e.g. `app-v1:hq_…`. Normally **one**; two only while rotating (old + new APK). Generated by the setup script; make one with `python -c "import secrets;print('hq_'+secrets.token_hex(32))"` |
| `CLOUDFLARE_TUNNEL_TOKEN` | *(empty)* | Only for the `tunnel` compose profile (§8) |
| `PUBLIC_BASE_URL` | *(empty)* | The HTTPS URL the app uses (e.g. `https://harmoniq.example.com`). Informational: shown in `/v1/meta` and logs |
| `DATA_DIR` | `./data` (local) · `/data` (Docker) | Holds `tmp/`, `files/`, `logs/` |
| `TOOLS_DIR` | `./tools` (local) · empty = use `PATH` (Docker) | Where `ffmpeg`, `ffprobe`, `deno` are looked up |

### Limits

| Variable | Default | Description |
|----------|---------|-------------|
| `MAX_CONCURRENT_DOWNLOADS` | `2` | Download jobs processed at the same time |
| `QUEUE_MAX_DEPTH` | `20` | Waiting jobs before `503 queue_full` |
| `MAX_ACTIVE_JOBS_PER_DEVICE` | `3` | Per app install (`X-Device-Id`) |
| `RATE_LIMIT_SEARCH_PER_MIN` | `30` | Per device |
| `RATE_LIMIT_DOWNLOADS_PER_HOUR` | `60` | Per device |
| `RATE_LIMIT_GLOBAL_PER_MIN` | `120` | All devices together, counting only requests that cause YouTube traffic (uncached search/metadata, new downloads). A safety net if the token ever leaks |
| `MAX_DURATION_SECONDS` | `900` | Reject longer videos (15 min) |
| `JOB_TIMEOUT_SECONDS` | `600` | Hard cap per job |
| `YT_MIN_INTERVAL_SECONDS` | `2` | Minimum gap between YouTube extractions |

### Caching and storage

| Variable | Default | Description |
|----------|---------|-------------|
| `SEARCH_CACHE_TTL` | `21600` (6 h) | In-memory |
| `METADATA_CACHE_TTL` | `3600` (1 h) | Keep ≤ a few hours; stream URLs expire |
| `JOB_TTL_SECONDS` | `86400` | How long finished job records stay in memory |
| `FILE_TTL_SECONDS` | `86400` (24 h) | Finished files, from last access |
| `STORAGE_MAX_BYTES` | `5368709120` (5 GiB) | Oldest files evicted above this |
| `DISK_MIN_FREE_BYTES` | `1073741824` | `/readyz` fails below this |

### Audio

| Variable | Default | Description |
|----------|---------|-------------|
| `DEFAULT_FORMAT` | `m4a` | `m4a` \| `opus` \| `mp3` |
| `MP3_STANDARD_BITRATE` | `192k` | For `quality=standard` |
| `ARTWORK_MAX_PX` | `1200` | |

### Metadata

| Variable | Default | Description |
|----------|---------|-------------|
| `METADATA_PROVIDERS` | `ytmusic,youtube,musicbrainz,lastfm,lyrics` | Providers in priority order; remove one to disable it |
| `LYRICS_PROVIDERS` | `ytmusic,lrclib` | Lyrics sources in order; empty = no lyrics |
| `MUSICBRAINZ_CONTACT` | *(empty)* | Contact (URL or email) for the MusicBrainz User-Agent. Optional; MusicBrainz appreciates one, but nothing personal is sent unless you set it |
| `LASTFM_API_KEY` | *(empty = off)* | Optional genre fallback when MusicBrainz has none (free key at last.fm/api) |
| `METADATA_TIMEOUT_SECONDS` | `25` | Time budget for metadata per job (runs in parallel with the download); what isn't ready by then is skipped |
| `FILENAME_TEMPLATE` | `{artist} - {title}` | Suggested filename; placeholders `{artist} {title} {album} {year} {track}`. `{title} - {artist}` matches the old notebook |

### YouTube access

| Variable | Default | Description |
|----------|---------|-------------|
| `YTDLP_JS_RUNTIME` | `deno` | `deno` \| `node` \| `bun` \| `quickjs` |
| `POT_PROVIDER_URL` | *(empty = off)* | e.g. `http://pot-provider:4416` (Docker) or `http://127.0.0.1:4416` |
| `YTDLP_COOKIES_FILE` | *(empty)* | e.g. `secrets/cookies.txt` or `/run/secrets/cookies.txt` (Netscape format) |
| `YTDLP_PROXY` | *(empty)* | e.g. `socks5://user:pass@host:port` |
| `YTDLP_IP_VERSION` | `auto` | `auto`: use the OS default, and if YouTube bot-checks it, retry over IPv6 and keep using what worked (until restart). `6` / `4`: always use IPv6 / IPv4. Set `6` if you know your IPv4 is flagged, to skip the failed first attempt after each restart |
| `YTDLP_SLEEP_REQUESTS` | `0.5` | Seconds between extractor HTTP requests |
| `CANARY_VIDEO_ID` | *(empty = off)* | A stable, Creative Commons video tested hourly |

---

## 6. YouTube access hardening

From your home connection, steps 1–3 are usually enough. Go further only if downloads start failing with
`upstream_blocked`.

1. **Keep yt-dlp current** (§7). This fixes most failures.
2. **Run from a home/residential IP** (§2).
3. **Use IPv6 if your IPv4 is shared.** Many home and mobile ISPs put customers behind one shared IPv4 address
   (CGNAT), and YouTube often bot-checks those addresses, while the same connection's IPv6 address works fine.
   This was the case on the development machine:
   - local mode worked because Windows prefers IPv6;
   - Docker containers only had IPv4 until IPv6 was enabled, so they were bot-checked.

   `docker-compose.yml` therefore enables IPv6, and `YTDLP_IP_VERSION=auto` (the default) retries over IPv6
   automatically. To check your own connection, run this from the server folder:

   ```bash
   .venv/Scripts/python -m yt_dlp --force-ipv4 -s "https://www.youtube.com/watch?v=jNQXAC9IVRw"
   ```

   Then run it again with `--force-ipv6` (on Linux/macOS the path is `.venv/bin/python`). If only IPv6 works, set
   `YTDLP_IP_VERSION=6`.

   On a **Linux** Docker host, `enable_ipv6` needs Docker 27+ and working IPv6 on the host; otherwise remove the
   `networks:` block and use `YTDLP_IP_VERSION=4`, or switch to local mode.
4. **PO-token provider.** Docker: `docker compose --profile pot up -d` and set `POT_PROVIDER_URL`. Local mode
   without Docker: the provider also has a script mode (needs Node.js), but running its container is simpler.
5. **Cookies from a throwaway Google account** (also enables age-restricted videos): export them from a private
   browser window that was *only* used to sign in and open YouTube once, into `secrets/cookies.txt`. Then close the
   window without signing out. **Never use your personal account**: accounts used for automated downloading can be
   suspended. Cookies last weeks to months.
6. **Residential proxy** via `YTDLP_PROXY`. Only relevant if you move to a VPS.

---

## 7. Keeping yt-dlp current

YouTube breaks yt-dlp several times a year, and fixes usually ship within hours to days. Updating is one command
and needs no code changes:

| Mode | Update to latest stable | Pin a specific / nightly version |
|------|-------------------------|----------------------------------|
| Local | `scripts\update-ytdlp.ps1` or `./scripts/update-ytdlp.sh` (runs `pip install -U "yt-dlp[default]"` inside `.venv`), then restart | `scripts\update-ytdlp.ps1 -Version 2026.10.26` · `-Nightly` |
| Docker | `docker compose build --no-cache && docker compose up -d` | `docker compose build --build-arg YTDLP_SPEC="yt-dlp[default]==2026.10.26"` |

Tips:
- Update roughly **weekly**, and immediately when downloads start failing with `upstream_error`.
- Check `components.yt_dlp` in `GET /v1/meta` afterwards.
- When updating `bgutil-ytdlp-pot-provider`, update the sidecar image to the same version.
- The setup scripts can also update FFmpeg and Deno: re-run setup with `-UpdateTools`.

---

## 8. How the app reaches the server

**Model:** you give testers the APK. The APK contains the server's **HTTPS URL** and a **secret app token**, and
sends the token with every request (`X-App-Token`). The server rejects any request without a valid token. Testers
install nothing else.

Because testers' phones can be anywhere (mobile data, other Wi-Fi), the server needs a URL that's reachable from
the internet **over HTTPS**. HTTPS isn't optional here: over plain HTTP, anyone on the same network could read the
token.

| Option | Works with | Effort | Notes |
|--------|-----------|--------|-------|
| **Cloudflare Tunnel** (recommended while the host is undecided) | Home PC, Raspberry Pi, VPS | Low | Run `cloudflared` next to the server. You get a stable `https://harmoniq.<your-domain>` with **no router ports opened** and your home IP hidden. Needs a (cheap) domain on a free Cloudflare account. Can be a second service in `docker-compose.yml` or a Windows service in local mode |
| **Caddy reverse proxy** + DNS name | VPS (public IP), or home with port 443 forwarded | Medium | Automatic Let's Encrypt HTTPS. On a home connection it needs port forwarding and dynamic DNS (DuckDNS works), and it exposes your home IP |
| Same Wi-Fi, plain HTTP | Development only | None | `http://<pc-ip>:8000` from your own phone while building the app. Not for testers |

Whatever you choose, the **URL in the APK must stay the same**, otherwise every tester needs a new APK. A tunnel or
DNS name gives you that even if the server moves from your PC to a Pi or a VPS later.

### What the token does and doesn't protect

- ✅ Stops random people and scanners who find the URL from using the server.
- ✅ Can be **rotated** at any time (see §10). Old APKs then stop working.
- ⚠️ It's a shared secret inside the APK, so a determined tester could extract it (APKs can be decompiled). For a
  handful of trusted testers that's an acceptable risk. The per-device and global rate limits cap the damage if it
  ever leaks.
- Keep the token out of git on the Android side too. The app reads `HARMONIQ_SERVER_URL` / `HARMONIQ_APP_TOKEN`
  (optionally per build type, e.g. `HARMONIQ_SERVER_URL_CANARY`) from environment variables or `local.properties`
  at build time, the same way its signing secrets are handled. Changing the URL or token therefore means rebuilding
  and redistributing the APK. Details: the app's `docs/09-global-search-server.md`.

Step-by-step tunnel setup: [deployment-guide.md §6](deployment-guide.md#6-cloudflare-tunnel-with-your-own-domain).

---

## 9. Keeping it running 24/7

| Platform | How |
|----------|-----|
| **Docker** (any OS) | `restart: unless-stopped` + healthcheck (already in compose). Docker Desktop must be set to start at login (Windows/macOS) |
| **Windows, local mode** | Task Scheduler task: *At startup*, run `powershell -File <path>\scripts\run.ps1`, "Run whether user is logged on or not", restart on failure. Alternatively NSSM to wrap it as a Windows service. Disable sleep in power settings |
| **Linux, local mode** | systemd unit: `ExecStart=<path>/scripts/run.sh`, `Restart=always`, `RestartSec=5`, `User=<you>`, `WorkingDirectory=<path>` |

The server cleans up after itself (sweeper every 10 minutes; size cap via `STORAGE_MAX_BYTES`), so it can run
unattended.

## 10. Day-to-day operations

| Task | How |
|------|-----|
| **Logs** | stdout plus `data/logs/server.log` (rotated, 10 MB × 3). Search by `job_id` / `request_id` |
| **Is YouTube working?** | `GET /v1/meta` → `status.youtube` and `last_canary_at` |
| **Add a tester** | Send them the APK. Nothing changes on the server |
| **Rotate the app token** (leaked, or a tester should lose access) | Add a new `id:secret` to `APP_TOKENS`, restart, build and share an APK with the new token **and a higher `versionCode`**, then remove the old entry and restart again. Old APKs stop working. Step by step: [deployment-guide.md §11](deployment-guide.md#11-rotating-the-app-token) |
| **Free disk space** | Stop the server and delete `data/files/*`. It's only a cache |
| **Backups** | Only `.env` (app and tunnel tokens). `data/` is a cache; an optional `secrets/cookies.txt` can be exported again |
| **Restarting** | Safe at any time. Running jobs are lost and the app resubmits them; finished files survive |

## 11. Checklist before testers use it

- [ ] `APP_TOKENS` set with a strong secret; `.env`, `secrets/`, `data/`, `.venv/`, `tools/` are git-ignored
- [ ] `/readyz` returns 200; `/v1/meta` shows yt-dlp, FFmpeg and Deno versions
- [ ] A test search and download work end to end (the curl walkthrough in [api.md](api.md#end-to-end-example-shell))
- [ ] The server is reachable **only over HTTPS** at a stable URL; port 8000 isn't exposed directly
- [ ] A request without `X-App-Token` gets `401`
- [ ] A weekly reminder (or scheduled task) to update yt-dlp
