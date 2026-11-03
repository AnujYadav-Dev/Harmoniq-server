# Deployment guide: first-time setup, step by step

[← Docs index](README.md) · Reference: [deployment.md](deployment.md)

This guide takes you from nothing to a harmoniq-server that Harmoniq's `canary` and `release` builds can reach at a
stable HTTPS address. It uses **server mode** (Docker Compose) with **Cloudflare Tunnel**, the setup the project
recommends. Every option and environment variable is explained in [deployment.md](deployment.md); this guide only
shows the path through them.

> Placeholders used below: `https://harmoniq.example.com` for your server URL, `hq_xxx…` for the app token,
> `eyJxxx…` for the tunnel token. Never paste real tokens into docs, issues, chats or commits.

**What you end up with**

```text
Phone (Harmoniq canary/release APK)
   │  HTTPS, X-App-Token
   ▼
Cloudflare edge  ── https://harmoniq.example.com
   │  outbound-only tunnel (no router ports opened)
   ▼
Your machine:  docker compose
   ├── tunnel   (cloudflared)  ──▶ http://server:8000
   └── server   (harmoniq-server, 127.0.0.1:8000 on the host)
```

---

## 1. Choose a host

| | Home machine (PC, mini PC, NAS, Raspberry Pi 5) | VPS / cloud VM |
|---|---|---|
| YouTube IP reputation | **Good**: a normal household IP | **Poor**: datacenter IPs are often bot-checked |
| Extra work | Keep it powered on; no sleep | PO-token provider and often a paid residential proxy ([deployment.md §6](deployment.md#6-youtube-access-hardening)) |
| Cost | Electricity | Monthly fee + proxy |
| Verdict | **Recommended** | Only if a home machine is impossible |

Why: the hard part of this server isn't CPU, it's YouTube accepting the machine's IP address
([deployment.md §2](deployment.md#2-where-to-host-it)). Resources for the owner plus a few testers: 1–2 CPU cores,
1 GB RAM for the server, ~5 GB free disk (plus about 1.1 GB for the image).

Because the app reaches the server through a **Cloudflare Tunnel hostname**, you can move the server to another
machine later without even editing the app's config file (§12).

## 2. Prerequisites

- A **domain name** you control (any registrar), added to a free Cloudflare account (§6.1).
- **Git** to clone the repository.
- **Docker** (§3).
- A shell: PowerShell or Git Bash on Windows, any shell on Linux. `curl` is built into Windows 10/11 as
  `curl.exe`; `jq` is optional but makes §8 easier.

## 3. Install Docker

### Windows: Docker Desktop

1. Check the requirements on the [Docker Desktop for Windows](https://docs.docker.com/desktop/setup/install/windows-install/)
   page: 64-bit Windows 10 22H2 (build 19045) or Windows 11 23H2 (build 22631) or newer, hardware virtualization on,
   WSL 2 (version 2.1.5 or later). The Home edition is fine here: it runs **Linux containers** only, which is all
   this project uses.
2. Download and run *Docker Desktop Installer.exe*, keep **Use WSL 2 instead of Hyper-V** selected, finish, then
   start Docker Desktop (it doesn't start by itself after installing).
3. **Settings → General → "Start Docker Desktop when you sign in to your computer"**: turn it on (off by default).
4. **Settings → Network → Default networking mode** should stay **Dual IPv4/IPv6 (default)**. That's what lets the
   container reach YouTube over IPv6 (§4.3).

Docker Desktop is free for personal use (see the licence note on the same page).

```powershell
docker version
```
Prints the client and server versions. An error about the daemon means Docker Desktop isn't running yet.

### Linux (Ubuntu / Debian): Docker Engine

Follow the official apt-repository instructions for [Ubuntu](https://docs.docker.com/engine/install/ubuntu/) or
[Debian](https://docs.docker.com/engine/install/debian/). They add Docker's apt repository and then install:

```bash
sudo apt install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```
Installs Docker Engine and the Compose plugin (run the repository setup from the official page first).

```bash
sudo docker run hello-world
```
Confirms the engine works.

Then the [post-install steps](https://docs.docker.com/engine/install/linux-postinstall/):

```bash
sudo usermod -aG docker $USER
```
Lets your user run `docker` without `sudo` (log out and back in afterwards). Note: the `docker` group is
root-equivalent on that machine.

```bash
sudo systemctl enable docker.service containerd.service
```
Starts Docker at boot (already the default on Debian and Ubuntu).

## 4. Get the code and create `.env`

### 4.1 Clone

```bash
git clone https://github.com/AnujYadav-Dev/Harmoniq-server.git
cd Harmoniq-server
```
Gets the server code. Everything below runs from this folder.

```bash
cp .env.example .env
```
Creates your configuration file (PowerShell: `Copy-Item .env.example .env`). `.env` is git-ignored; keep it that way.

### 4.2 Generate a production app token

The token is the shared secret built into the APK. Use a new one for production; don't reuse the token from a
development machine.

```bash
echo "hq_$(openssl rand -hex 32)"
```
Git Bash / Linux: prints a random token like `hq_xxx…` (64 hex characters after `hq_`).

```powershell
$b = New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); 'hq_' + (($b | ForEach-Object { $_.ToString('x2') }) -join '')
```
The same in Windows PowerShell 5.1, using the cryptographic random generator.

Put it into `.env` with an **id** in front. The id (here `app-v1`) is only a label for logs and rotation; the app
gets the part **after** the colon:

```dotenv
APP_TOKENS=app-v1:hq_xxx…
```

### 4.3 The other values to set

Edit `.env` and set (uncomment) these lines:

```dotenv
PUBLIC_BASE_URL=https://harmoniq.example.com
CANARY_VIDEO_ID=<a short, stable public video ID>
YTDLP_IP_VERSION=auto
CLOUDFLARE_TUNNEL_TOKEN=          # filled in at §6.3
```

| Key | What to put | Why |
|-----|-------------|-----|
| `PUBLIC_BASE_URL` | Your final HTTPS URL | Informational only (logs, `/v1/meta`) |
| `CANARY_VIDEO_ID` | A short video that won't disappear, ideally Creative Commons or your own upload. The repo's live tests use `jNQXAC9IVRw` ("Me at the zoo", 19 s) | The server downloads it **every hour** and reports the result as `status.youtube` / `last_canary_*` in `/v1/meta`; the app shows a banner when YouTube is degraded or blocked. Empty = no canary |
| `YTDLP_IP_VERSION` | `auto` (default). Use `6` if you already know your IPv4 is bot-checked | Many home/mobile ISPs share one IPv4 address between customers (CGNAT) and YouTube bot-checks it, while the same connection's IPv6 works. `auto` retries over IPv6 when that happens; `6` skips the failed first attempt after each restart |
| `HARMONIQ_UID` / `HARMONIQ_GID` | Linux only: the output of `id -u` / `id -g` if not 1000 | Files the container writes into `./data` then belong to you |

Leave everything else at its default. `docker-compose.yml` already enables IPv6 for the containers
(`enable_ipv6: true`). On a Linux host that needs IPv6 connectivity on the host and a recent Docker Engine (the compose
file notes 27+); without it, remove the `networks:` block and set `YTDLP_IP_VERSION=4`
([deployment.md §6](deployment.md#6-youtube-access-hardening)).

## 5. Start the server (without the tunnel yet)

```bash
docker compose up -d --build
```
Builds the image (first time: a few minutes, ~1.1 GB) and starts the `server` container in the background.

```bash
docker compose ps
```
Shows the container; after about 30 s its status should read `healthy`.

```bash
docker compose logs -f server
```
Follows the log. Look for `harmoniq-server 0.2.0 starting; components: …`. A warning `APP_TOKENS is empty` means
`.env` wasn't edited. Press Ctrl+C to stop following (the server keeps running).

```bash
curl -fsS http://localhost:8000/readyz
```
Local check from the same machine: `{"status":"ready",…}` means data folder, disk space, FFmpeg, ffprobe and Deno are
all fine. The port is bound to `127.0.0.1` only, so nothing else on the network can reach it directly.

## 6. Cloudflare Tunnel with your own domain

Cloudflare's dashboard labels change from time to time. The steps below match the
[Cloudflare Tunnel docs](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/get-started/create-remote-tunnel/)
as of 2026-11; if a label differs, follow the linked page.

### 6.1 Put your domain on Cloudflare (once)

1. Sign up at cloudflare.com (the Free plan is enough).
2. **Domains → Onboard a domain**, enter your domain, pick a plan, and let Cloudflare import the existing DNS records.
3. At your **registrar**, replace the nameservers with the two Cloudflare nameservers shown
   ([Onboard a domain](https://developers.cloudflare.com/fundamentals/manage-domains/add-site/)). Wait until
   Cloudflare shows the domain as active (minutes to a day).

### 6.2 Create the tunnel

1. **Networking → Tunnels → Create a tunnel**. Name it, e.g. `harmoniq`, and select **Create Tunnel**.
2. On the install step, choose **Docker** if offered and copy the shown command. **Don't run it.** You only need the
   token: the long `eyJxxx…` string after `--token`. (You can show it again later from the tunnel's **Overview** tab
   → **Add a replica**.)

### 6.3 Run the tunnel next to the server

Put the token into `.env`:

```dotenv
CLOUDFLARE_TUNNEL_TOKEN=eyJxxx…
```

```bash
docker compose --profile tunnel up -d
```
Starts the `tunnel` service (`cloudflared … tunnel run`, token passed as `TUNNEL_TOKEN`) in addition to `server`.
From now on, **always include `--profile tunnel`** in `up` commands, or the tunnel service is ignored.

```bash
docker compose logs tunnel
```
Should show `Registered tunnel connection` lines. In the dashboard the tunnel turns **Healthy**.

### 6.4 Publish the hostname

1. **Networking → Tunnels →** your tunnel **→ Routes → Add route → Published application**.
2. Subdomain `harmoniq`, Domain `example.com` (yours), no path.
3. **Service URL: `http://server:8000`**. Not `localhost`: `cloudflared` runs in its own container and reaches the
   server by its compose service name.
4. **Add route.** Cloudflare creates the DNS record and the certificate. `https://harmoniq.example.com` now leads to
   your server.

No ports are opened on your router and your home IP isn't published.

## 7. Firewall and exposure check

- The server listens on `127.0.0.1:8000` on the host only ([docker-compose.yml](../docker-compose.yml)); don't
  change that to `0.0.0.0` in production.
- Every `/v1` request needs the app token. `/healthz` and `/readyz` are open on purpose (they reveal nothing
  sensitive).

## 8. Verify from outside

Run these from any computer, ideally **not** on the same network (e.g. a laptop on a phone hotspot). Git Bash shown;
in PowerShell use `curl.exe` instead of `curl` and `$env:BASE = "…"` for variables.

```bash
BASE=https://harmoniq.example.com
TOKEN='hq_xxx…'            # the secret part of APP_TOKENS, after the colon
H="X-App-Token: $TOKEN"
```
Sets shell variables for the checks below (nothing is saved).

```bash
curl -fsS "$BASE/healthz"
```
`{"status":"ok"}`: the tunnel and the process work.

```bash
curl -fsS "$BASE/readyz"
```
`{"status":"ready",…}`: tools and disk are fine. A `503 service_unavailable` lists what is missing.

```bash
curl -s -o /dev/null -w "%{http_code}\n" "$BASE/v1/meta"
```
Must print `401`: requests without the token are rejected.

```bash
curl -fsS "$BASE/v1/meta" -H "$H"
```
Shows `server_version` (`0.2.0`), `components` (yt-dlp, FFmpeg, Deno versions) and `status.youtube`. After the first
canary run (about a minute after start, then hourly) `last_canary_ok` should be `true`.

```bash
curl -fsS "$BASE/v1/search?q=coldplay+yellow&limit=3" -H "$H"
```
A search: JSON with `results[]`, each with a `video_id`.

A full download (needs `jq`; [api.md](api.md#end-to-end-example-shell) has the same flow):

```bash
ID=$(curl -fsS "$BASE/v1/search?q=coldplay+yellow&limit=1" -H "$H" | jq -r '.results[0].video_id')
JOB=$(curl -fsS -X POST "$BASE/v1/downloads" -H "$H" -H 'Content-Type: application/json' \
  -d "{\"video_id\":\"$ID\",\"format\":\"m4a\"}" | jq -r .job_id)
while :; do S=$(curl -fsS "$BASE/v1/downloads/$JOB" -H "$H" | jq -r .status); echo "$S"
  case "$S" in completed|failed|cancelled|expired) break;; esac; sleep 2; done
curl -fsS -OJ "$BASE/v1/downloads/$JOB/file" -H "$H"
curl -fsS -X DELETE "$BASE/v1/downloads/$JOB" -H "$H"
```
Searches, starts a job, polls until it ends, saves the tagged `.m4a` into the current folder, then frees the server
copy. If the status ends as `failed`, run `curl -fsS "$BASE/v1/downloads/$JOB" -H "$H"` to see `error.code`
(see §15).

## 9. Connect the Android build

The app finds the server **at runtime**, so connecting it needs no rebuild. In the Harmoniq app repository, edit
[`config/global-search.json`](https://github.com/AnujYadav-Dev/Harmoniq/blob/main/config/global-search.json) on the `main` branch (GitHub's web editor is fine):

```json
{
  "version": 1,
  "server_url": "https://harmoniq.example.com",
  "app_token": "hq_xxx…",
  "enabled": true,
  "message": null
}
```

`app_token` is the part after the colon of one `APP_TOKENS` entry. Every installed app using the default
"Harmoniq server" picks it up within minutes. **That file is public**, so this token is a public app key: it keeps
out bots, and the server's rate limits do the rest. For a private server, users choose **Settings → Global search →
Server → My own server** in the app and enter the URL and token there (stored encrypted on the phone).

Then open **Settings → Global search** in the app: *Connection* should say "From Harmoniq's GitHub", and *Server
status* should show the server version and YouTube status. Every key, local testing (`adb reverse`, `10.0.2.2`, LAN IP), and the app-side
troubleshooting are in the app docs:
[docs/09-global-search-server.md](https://github.com/AnujYadav-Dev/Harmoniq/blob/main/docs/09-global-search-server.md) in the Harmoniq repository.

## 10. Updating

**Server code** (new harmoniq-server version):

```bash
git pull
```
Gets the latest code.

```bash
docker compose --profile tunnel up -d --build
```
Rebuilds the image and recreates the containers. Running jobs are lost and the app resubmits them; finished files
survive ([deployment.md §10](deployment.md#10-day-to-day-operations)).

**yt-dlp, weekly** (and immediately when downloads fail with `upstream_error`):

```bash
docker compose build --no-cache server
```
Rebuilds the image from scratch, which installs the newest yt-dlp (and the latest Debian FFmpeg package).

```bash
docker compose --profile tunnel up -d
```
Restarts with the new image. Then check `components.yt_dlp` in `/v1/meta`.

**cloudflared**: the compose file uses `cloudflare/cloudflared:latest` with `--no-autoupdate`. Update it now and then:

```bash
docker compose --profile tunnel pull tunnel
```
Downloads the newest `cloudflared` image; follow with `docker compose --profile tunnel up -d`.

On Linux you can schedule the weekly yt-dlp rebuild with cron, e.g. `crontab -e` and:

```text
0 4 * * 1  cd /home/<you>/Harmoniq-server && docker compose build --no-cache server && docker compose --profile tunnel up -d
```

On Windows, put the same two commands in a Task Scheduler task (weekly), or set yourself a reminder.

## 11. Rotating the app token

Do this when a token leaks or someone should lose access. Old APKs stop working once their token is removed.

1. Generate a new token (§4.2) and **add** it, keeping the old one for now:

   ```dotenv
   APP_TOKENS=app-v1:hq_old…,app-v2:hq_new…
   ```

2. Restart so the server reads `.env` again:

   ```bash
   docker compose --profile tunnel up -d --force-recreate server
   ```
   Recreates the server container with the new environment. Both tokens are accepted now.

3. Put the new token into `app_token` in the app repository's
   [`config/global-search.json`](https://github.com/AnujYadav-Dev/Harmoniq/blob/main/config/global-search.json) and commit. No app rebuild is needed.
4. A day later, remove the old entry (`APP_TOKENS=app-v2:hq_new…`) and repeat step 2.

A phone that still has the old token gets one `401`, re-reads the config file and continues with the new token; a
download in progress retries instead of failing. Users of **My own server** update the token in the app's
Settings. (Details: the app's [docs/09](https://github.com/AnujYadav-Dev/Harmoniq/blob/main/docs/09-global-search-server.md#changing-or-updating-the-server-url-or-token).)

If the **tunnel token** leaked, refresh it as well: **Networking → Tunnels →** your tunnel **→ Overview → Refresh
token**, put the new token into `.env`, and recreate the tunnel with
`docker compose --profile tunnel up -d --force-recreate tunnel`
([tunnel permissions](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/remote-tunnel-permissions/)).

## 12. Moving to a new machine (same URL)

The URL belongs to the tunnel, not to the machine, so the app's `config/global-search.json` doesn't need to change.
(Moving to a **different** URL instead? Just update `server_url` in that file, see §9.)

1. On the new machine: install Docker (§3), clone (§4.1), and copy the old **`.env`** over (it holds `APP_TOKENS`
   and `CLOUDFLARE_TUNNEL_TOKEN`). On Linux check `HARMONIQ_UID`/`HARMONIQ_GID`. `data/` doesn't need copying: it's
   a cache.
2. Start it: `docker compose --profile tunnel up -d --build`, and check `docker compose logs tunnel`.
3. Stop the old one right away: on the old machine, `docker compose --profile tunnel down`.

While both run, Cloudflare treats them as **replicas** of the same tunnel and sends each request to the nearest
connector, so a job started on one machine could be polled on the other. Keep the overlap short. If you no longer
trust the old machine, refresh the tunnel token afterwards (§11).

## 13. Running 24/7

| Platform | What to do |
|----------|------------|
| All | Both services use `restart: unless-stopped`, and the server has a healthcheck, so Docker restarts them after crashes and after Docker itself starts |
| Windows (Docker Desktop) | Enable *Start Docker Desktop when you sign in* (§3). Docker Desktop starts **after you sign in**, so after a power cut or Windows Update reboot the server is down until someone signs in. Disable sleep (Settings → System → Power) and plan Windows Update active hours |
| Linux (Docker Engine) | `docker.service` is enabled at boot (§3), so the stack comes back after a reboot without anyone signing in. This is the more reliable choice for an always-on server |

Optional: point an uptime monitor at `https://harmoniq.example.com/readyz`.

## 14. Backups

Only **`.env`** needs a backup: it holds the app token(s) and the tunnel token. Store a copy in a password manager.
Everything else is replaceable: code is in git, `data/` is a cache, the image is rebuilt from the Dockerfile, and
an optional `secrets/cookies.txt` can be exported again.

## 15. Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `401 unauthorized` with curl | Wrong token, the `id:` part included, or quotes/spaces copied | Send only the part after the colon. Check the server log line `APP_TOKENS is empty` |
| App says *"The server didn't accept the access key"* | The token in `config/global-search.json` (or the user's own-server token) isn't in `APP_TOKENS` | Fix `app_token` in the config file, or add that token to `APP_TOKENS` (§11) |
| App says *"Global Search is paused right now"* | `"enabled": false` in `config/global-search.json` | Set it back to `true` |
| `curl` can't resolve or connect to the hostname | DNS not active yet, or no route | Cloudflare dashboard: domain active? Route under the tunnel's **Routes** tab? |
| A Cloudflare error page instead of JSON | Tunnel down, or Service URL wrong | `docker compose ps` (is `tunnel` running?), `docker compose logs tunnel`; Service URL must be `http://server:8000` |
| Tunnel shows **Down** / **Inactive** | Container stopped, token wrong or refreshed, machine offline | `docker compose --profile tunnel up -d`; recheck `CLOUDFLARE_TUNNEL_TOKEN` |
| `/readyz` → `503` | Disk below `DISK_MIN_FREE_BYTES` (1 GiB), data folder not writable, or a tool missing | The `detail` says which. On Linux, check `./data` ownership vs `HARMONIQ_UID` |
| Downloads fail with `upstream_blocked`; `status.youtube` is `blocked`; log says `YouTube bot-checked route …` | YouTube bot-checks your IP | Update yt-dlp (§10). Test IPv4 vs IPv6 (below); set `YTDLP_IP_VERSION=6` if only IPv6 works. Then PO-token provider / cookies ([deployment.md §6](deployment.md#6-youtube-access-hardening)) |
| Downloads fail with `upstream_error` | YouTube changed something; yt-dlp is outdated | Rebuild for the newest yt-dlp (§10) |
| Disk full | `data/files` cache, old images/build cache | `STORAGE_MAX_BYTES` caps the cache (5 GiB default). `docker system df` shows Docker's usage; `docker image prune` removes dangling old images after rebuilds |

IPv4 vs IPv6 test from inside the container:

```bash
docker compose exec server python -m yt_dlp --force-ipv4 -s "https://www.youtube.com/watch?v=jNQXAC9IVRw"
```
Simulates (`-s`) fetching a video over IPv4. Repeat with `--force-ipv6`. If only IPv6 works, set `YTDLP_IP_VERSION=6`;
if IPv6 fails with a network error, the container has no IPv6 route (Docker Desktop: check **Default networking mode**;
Linux: see §4.3).

Server logs: `docker compose logs server`, or `data/logs/server.log` (rotated, 10 MB × 3). Every error the app shows
carries a `request_id` you can search for.

## 16. Next steps & remaining work

**Deploying, in order**

1. Pick the host and install Docker (§1–§3).
2. Clone, create `.env` with a fresh production token, `CANARY_VIDEO_ID`, `YTDLP_IP_VERSION` (§4).
3. `docker compose up -d --build`, check `/readyz` locally (§5).
4. Domain on Cloudflare, create the tunnel, `docker compose --profile tunnel up -d`, publish `harmoniq.<domain>` →
   `http://server:8000` (§6).
5. Verify from outside: `/healthz`, `/readyz`, `401` without token, `/v1/meta`, a search and a download (§8).
6. Put the URL and token into the app's `local.properties` for `canary`/`release`, build a **signed canary**, install
   it, and run the test matrix (§9, [android-integration-plan.md §16](android-integration-plan.md#16-validating-the-complete-flow-before-testers-get-it)).
7. Set up the weekly yt-dlp update and back up `.env` (§10, §14).

**Remaining work**

| Item | Where |
|------|-------|
| Device tests on Android 9 (API 28: legacy storage path + permission prompt) and Android 14 (API 34) | App, [android-integration-plan.md §16](android-integration-plan.md#16-validating-the-complete-flow-before-testers-get-it) |
| Signed `release` and `canary` builds tested on a device (R8/minification) | App `docs/05-release-and-distribution.md` |
| The rest of the §16 test matrix (formats, duplicate, interrupted download, server down / wrong token / rate limited, multi-disc, non-Latin) | [android-integration-plan.md §16](android-integration-plan.md#16-validating-the-complete-flow-before-testers-get-it) |
| Commit both repositories (the app's Global Search changes are staged, not committed; server docs) | Both repos |
| Add a `LICENSE` file (AGPL-3.0-or-later, as declared in `pyproject.toml`) to the server repository | Server repo |
| After an AGP upgrade: move WorkManager past 2.10.x (2.11+ needs Room 2.7) and remove the `NullSafeMutableLiveData` lint exception | App, [android-integration-plan.md §17](android-integration-plan.md#17-implementation-status-and-deviations) |
