# VPS deployment guide: Docker, Nginx, Let's Encrypt and CI/CD

[← Docs index](README.md) · Home/tunnel setup: [deployment-guide.md](deployment-guide.md) · Reference: [deployment.md](deployment.md)

This guide puts harmoniq-server on a cloud VM with a public IP, next to whatever already runs there, and sets up
automatic deploys from GitHub. It's how production runs: Oracle Cloud (OCI) Ampere A1, Ubuntu 26.04 **arm64**, 1 CPU
core, 6 GB RAM, already running another site behind Nginx and Certbot. That server already had open ports, Nginx and
Certbot, so the firewall steps in §2 and the installs in §8 are standard practice rather than something tested there.

> Placeholders: `harmoniq.example.com` for your hostname, `<server-ip>` for the VM's public IP, `<owner>` for your
> GitHub user or organisation **in lower case**, `hq_xxx…` for the app token. Never put real tokens, keys or cookies
> into docs, issues, chats or commits.

**Which guide do I need?** At home, with a normal household IP, [deployment-guide.md](deployment-guide.md) is simpler
and YouTube rarely objects. On a VPS the HTTPS part is easy, but YouTube distrusts datacenter IPs, so §11 is required
reading.

**What you end up with**

```text
Phone ──HTTPS + X-App-Token──▶ Nginx on the host (:443, shared with your other sites)
                                  │  harmoniq.example.com
                                  ▼
                       127.0.0.1:8000 ─▶ server container ──▶ pot-provider (PO tokens)
                                               │ downloads
                                               ▼
                                           warp container ──▶ Cloudflare ──▶ YouTube

git push main ─▶ GitHub Actions: test ─▶ build image ─▶ push to GHCR ─▶ SSH: deploy.sh (health-gated, rolls back)
```

**How it stays isolated from your other services**

| Boundary | How |
|----------|-----|
| Files | Everything lives in `/opt/harmoniq`, owned by a dedicated `harmoniq` user |
| Network | Its own Docker network. Only `127.0.0.1:8000` is published, never `0.0.0.0` |
| Web server | Its own Nginx site file and its own certificate. Existing site files aren't edited |
| CPU and memory | Container limits, lower CPU priority and one download at a time (§5, §6) |
| Disk | `STORAGE_MAX_BYTES` and `DISK_MIN_FREE_BYTES` cap the cache; logs rotate |
| CI access | Separate SSH keys that can only run `deploy.sh` (§9) or `update-cookies.sh` (§11.4) |

---

## 1. Before you start

You need:

- A Linux VM with `sudo`. Ubuntu is what's tested. Allow about 1.5 GB RAM and 10 GB disk for Harmoniq.
- A hostname you can point at the VM (an A record).
- Your own fork of [Harmoniq-server](https://github.com/AnujYadav-Dev/Harmoniq-server), because the CI secrets and the
  image belong to whoever runs the server.

Look at what's already on the machine, and back it up, before changing anything:

```bash
sudo ss -tulpn
```
Lists listening ports. If `8000` is taken, pick another and change `127.0.0.1:8000:8000` (§5) and `proxy_pass` (§8).

```bash
uname -m; nproc; free -h; df -h /
```
Shows CPU architecture and free resources. `aarch64` means an arm64 image, `x86_64` means amd64 (§9.1).

```bash
B=/root/pre-harmoniq-backup-$(date +%Y%m%d); sudo mkdir -p $B; sudo sh -c "iptables-save > $B/iptables.v4; ip6tables-save > $B/iptables.v6; cp -a /etc/iptables $B/iptables-etc 2>/dev/null; cp -a /etc/nginx $B/nginx 2>/dev/null"; echo $B
```
Copies the firewall rules and Nginx config somewhere safe.

## 2. Firewall

Ports 80 and 443 must be open in **two** places. Skip this section if a site is already served over HTTPS from this VM.

1. **Cloud firewall.** On OCI: *Networking → Virtual Cloud Networks →* your VCN *→ Security Lists*, add ingress rules
   for TCP 80 and 443 from `0.0.0.0/0`.
2. **Host firewall.** Oracle's Ubuntu images use iptables with a final `REJECT` rule in `INPUT`:

   ```bash
   sudo iptables -L INPUT --line-numbers
   ```
   Shows the rules. Note the line number of the `REJECT` rule, for example `6`.

   ```bash
   sudo iptables -I INPUT 6 -p tcp -m multiport --dports 80,443 -m state --state NEW -j ACCEPT
   ```
   Adds the rule just above the `REJECT` (use your line number).

   ```bash
   sudo netfilter-persistent save
   ```
   Makes it survive reboots. **Do this before installing Docker** (see the warning below).

On hosts using `ufw`, run `sudo ufw allow 80,443/tcp` instead.

> **Once Docker is installed, never run `netfilter-persistent save` or `reload`.** `save` would write Docker's runtime
> chains into your saved rules, and `reload` removes Docker's rules until `sudo systemctl restart docker`. To change
> the firewall later, add the rule live with `iptables -I` and add the same line by hand to `/etc/iptables/rules.v4`.

Docker sets the `FORWARD` policy to `DROP` and adds its own chains. That's normal and doesn't affect `INPUT`. Published
ports skip `INPUT` altogether, which is why §5 binds to `127.0.0.1`.

## 3. Install Docker

```bash
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2
```
Installs Ubuntu's Docker packages. It doesn't restart Nginx or your other services. Docker's own apt repository
([deployment-guide.md §3](deployment-guide.md#3-install-docker)) works just as well.

```bash
sudo docker version --format '{{.Server.Version}}' && sudo docker compose version
```
Confirms the engine and the Compose plugin are installed.

## 4. Create the service user and folder

```bash
sudo useradd --system --create-home --home-dir /opt/harmoniq --shell /bin/bash harmoniq
```
Creates a system user whose home is `/opt/harmoniq`.

```bash
sudo usermod -aG docker harmoniq && sudo passwd -l harmoniq && sudo chmod 750 /opt/harmoniq
```
Lets it run Docker and locks its password. Note: the `docker` group is root-equivalent, which is why the CI key is
restricted in §9.2.

```bash
sudo -u harmoniq mkdir -p /opt/harmoniq/data /opt/harmoniq/secrets /opt/harmoniq/.ssh
```
Creates the data, secrets and SSH folders.

```bash
sudo chmod 700 /opt/harmoniq/.ssh /opt/harmoniq/secrets; id harmoniq
```
Protects the last two. Note the `uid` and `gid`; §5 needs them.

## 5. `compose.yml`

Create `/opt/harmoniq/compose.yml` as the `harmoniq` user (`sudo -u harmoniq nano /opt/harmoniq/compose.yml`). It
differs from the repository's `docker-compose.yml`: it pulls a built image instead of building, has no IPv6 block and
adds resource limits. Replace `<owner>`, `<uid>` and `<gid>`.

```yaml
# Managed by /opt/harmoniq/deploy.sh; the image tag comes from .tag
name: harmoniq

services:
  server:
    image: ghcr.io/<owner>/harmoniq-server:${TAG:-latest}
    env_file: .env
    environment:
      DATA_DIR: /data
      TOOLS_DIR: ""
    user: "<uid>:<gid>"
    ports:
      - "127.0.0.1:8000:8000"       # Nginx on this host is the only client
    volumes:
      - ./data:/data
      - ./secrets:/run/secrets      # must be writable: yt-dlp writes cookies.txt back after every use
    restart: unless-stopped
    stop_grace_period: 1m
    mem_limit: 1g
    memswap_limit: 1g
    cpus: 0.75                      # on a 1-core host, leaves room for everything else
    cpu_shares: 512
    pids_limit: 256
    security_opt:
      - no-new-privileges:true
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://localhost:8000/healthz"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 20s
    logging:
      driver: json-file
      options: { max-size: "10m", max-file: "3" }

  # Cloudflare WARP as yt-dlp's exit (§11). Its networking changes stay inside its own container.
  warp:
    image: caomingjun/warp:2026.8.2115.0-2.12.0   # pinned; arm64 and amd64
    restart: unless-stopped
    device_cgroup_rules:
      - "c 10:200 rwm"
    cap_add:
      - NET_ADMIN
    sysctls:
      - net.ipv6.conf.all.disable_ipv6=0
      - net.ipv4.conf.all.src_valid_mark=1
    environment:
      WARP_SLEEP: "2"
    volumes:
      - ./warp:/var/lib/cloudflare-warp
    mem_limit: 384m
    cpus: 0.5
    logging:
      driver: json-file
      options: { max-size: "5m", max-file: "2" }

  # PO tokens for yt-dlp (§11). Keep its version in step with bgutil-ytdlp-pot-provider in requirements.lock.
  pot-provider:
    image: brainicism/bgutil-ytdlp-pot-provider:latest
    profiles: ["pot"]
    restart: unless-stopped
    init: true
    mem_limit: 512m
    cpus: 0.5
```

Adjust the limits to your machine. With more cores, raise `cpus` and `MAX_CONCURRENT_DOWNLOADS`.

## 6. `.env`

```bash
echo "hq_$(openssl rand -hex 32)"
```
Prints a new app token.

Create `/opt/harmoniq/.env` as the `harmoniq` user, then run `sudo chmod 600 /opt/harmoniq/.env`. It stays on the server
and is never stored in GitHub.

```dotenv
APP_TOKENS=app-v1:hq_xxx…
APP_ENV=production
LOG_LEVEL=INFO
PUBLIC_BASE_URL=https://harmoniq.example.com

# This host has no IPv6. Use auto if yours does.
YTDLP_IP_VERSION=4

# Small host: one download at a time, short queue, bounded cache
MAX_CONCURRENT_DOWNLOADS=1
QUEUE_MAX_DEPTH=10
STORAGE_MAX_BYTES=5368709120
DISK_MIN_FREE_BYTES=2147483648

# YouTube access (§11)
POT_PROVIDER_URL=http://pot-provider:4416
YTDLP_PROXY=socks5://warp:1080

```

The full list of variables is in [deployment.md §5](deployment.md#5-environment-variables). The app gets the part of
`APP_TOKENS` **after** the colon.

## 7. `deploy.sh`

Create `/opt/harmoniq/deploy.sh` with this content. Replace `<owner>`.

```bash
#!/usr/bin/env bash
# Harmoniq deploy entrypoint. Also the forced command of the CI deploy key (~/.ssh/authorized_keys).
#   deploy.sh deploy <git-sha|latest>   pull (GHCR token optional on stdin), start, wait for health, else roll back
#   deploy.sh rollback                  go back to the previous healthy tag
#   deploy.sh status                    current/previous tag and container health
set -euo pipefail
cd /opt/harmoniq
exec 9>/opt/harmoniq/.deploy.lock
flock -w 600 9

IMAGE=ghcr.io/<owner>/harmoniq-server
[ -n "${SSH_ORIGINAL_COMMAND:-}" ] && set -- $SSH_ORIGINAL_COMMAND
CMD=${1:-status}; ARG=${2:-}
CUR=$(cat .tag 2>/dev/null || true); PREV=$(cat .tag.prev 2>/dev/null || true)
log() { echo "[deploy $(date -u +%FT%TZ)] $*"; }

health() {
  local id; id=$(docker compose ps -q server)
  [ -n "$id" ] && docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$id" || echo missing
}

start() {   # start <tag>; returns 0 once healthy
  TAG=$1 docker compose up -d --no-deps server
  for _ in $(seq 1 40); do
    case $(health) in healthy) return 0 ;; unhealthy|exited|dead) return 1 ;; esac
    sleep 3
  done
  return 1
}

prune() {   # keep only the current and previous tags
  local keep="$IMAGE:$(cat .tag) $IMAGE:$(cat .tag.prev 2>/dev/null || echo none)"
  docker images "$IMAGE" --format '{{.Repository}}:{{.Tag}}' | while read -r ref; do
    case " $keep $IMAGE:latest " in *" $ref "*) ;; *) docker rmi "$ref" >/dev/null 2>&1 || true ;; esac
  done
  docker image prune -f >/dev/null
}

case $CMD in
  deploy)
    [[ $ARG =~ ^([0-9a-f]{7,40}|latest)$ ]] || { echo "usage: deploy <git-sha|latest>"; exit 2; }
    if [ ! -t 0 ] && read -r -t 2 TOKEN && [ -n "$TOKEN" ]; then
      echo "$TOKEN" | docker login ghcr.io -u ci --password-stdin >/dev/null
      docker pull -q "$IMAGE:$ARG"; docker logout ghcr.io >/dev/null
    else
      docker pull -q "$IMAGE:$ARG" 2>/dev/null || docker image inspect "$IMAGE:$ARG" >/dev/null
    fi
    log "starting $ARG (current: ${CUR:-none})"
    if start "$ARG"; then
      [ -n "$CUR" ] && [ "$CUR" != "$ARG" ] && echo "$CUR" > .tag.prev
      echo "$ARG" > .tag; prune; log "healthy on $ARG"
    else
      log "$ARG failed its health check"; docker compose logs --tail 40 server || true
      if [ -n "$CUR" ]; then start "$CUR" && log "rolled back to $CUR"; fi
      exit 1
    fi ;;
  rollback)
    [ -n "$PREV" ] || { echo "no previous tag"; exit 1; }
    log "rolling back $CUR -> $PREV"
    start "$PREV" || { log "previous tag is unhealthy too"; exit 1; }
    echo "$CUR" > .tag.prev; echo "$PREV" > .tag; log "healthy on $PREV" ;;
  status)
    echo "current=${CUR:-none} previous=${PREV:-none} health=$(health)" ;;
  *) echo "usage: deploy.sh deploy <sha>|rollback|status"; exit 2 ;;
esac
```

```bash
sudo chown root:root /opt/harmoniq/deploy.sh && sudo chmod 755 /opt/harmoniq/deploy.sh
```
Makes it owned by root, so the `harmoniq` user can run it but not edit it directly.

## 8. DNS, Nginx and the certificate

Skip the installs if the machine already has Nginx and Certbot:

```bash
sudo apt-get install -y nginx && sudo snap install --classic certbot && sudo ln -sf /snap/bin/certbot /usr/bin/certbot
```
Installs Nginx and Certbot.

**1. DNS.** Add an A record: `harmoniq.example.com` → `<server-ip>`. On Cloudflare set it to **DNS only (grey
cloud)**, so that Certbot validates against your server and long-lived progress streams aren't cut by the proxy.

```bash
nslookup harmoniq.example.com 1.1.1.1
```
Must print `<server-ip>` before you continue.

A dedicated hostname is the safest choice: another site's Nginx file stays untouched, and the app doesn't need
path rewriting.

**2. Shared proxy headers.** Create `/etc/nginx/snippets/harmoniq-proxy.conf`:

```nginx
proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_connect_timeout 5s;
```

**3. Temporary HTTP site, so Certbot can validate.** Create `/etc/nginx/sites-available/harmoniq-server`:

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name harmoniq.example.com;
    location / { return 404; }
}
```

```bash
sudo ln -sfn /etc/nginx/sites-available/harmoniq-server /etc/nginx/sites-enabled/harmoniq-server
```
Enables the site.

```bash
sudo nginx -t && sudo systemctl reload nginx
```
Tests, then reloads without downtime. **Always `nginx -t` first and `reload`, never `restart`:** a syntax error here
would take every site on the host down.

**4. Get the certificate.**

```bash
sudo certbot certonly --nginx --non-interactive --agree-tos -m you@example.com -d harmoniq.example.com
```
Requests a certificate and, because of `certonly`, leaves your config files alone. Renewal uses Certbot's existing
timer. `--agree-tos -m` is only needed the first time Certbot runs on the machine.

**5. Final site.** Replace the contents of `/etc/nginx/sites-available/harmoniq-server` with:

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name harmoniq.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name harmoniq.example.com;

    ssl_certificate /etc/letsencrypt/live/harmoniq.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/harmoniq.example.com/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    client_max_body_size 1m;
    access_log /var/log/nginx/harmoniq.access.log;
    error_log /var/log/nginx/harmoniq.error.log;

    # Download progress as Server-Sent Events: no buffering, long-lived
    location ~ ^/v1/downloads/[^/]+/events$ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 1h;
        include /etc/nginx/snippets/harmoniq-proxy.conf;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_read_timeout 120s;
        proxy_max_temp_file_size 0;   # stream audio files straight through
        include /etc/nginx/snippets/harmoniq-proxy.conf;
    }
}
```

```bash
sudo nginx -t && sudo systemctl reload nginx
```
Applies it. `https://harmoniq.example.com/healthz` now returns `502` until the container runs, which is expected.

If the host's other site file is the first one Nginx loads, it remains the default for unknown hostnames, as it was.

## 9. CI/CD with GitHub Actions

[`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml) runs on every push to `main`, and on demand (*Actions →
Deploy → Run workflow*):

1. **test:** `python -m pytest` with FFmpeg installed.
2. **build:** builds the image on an arm64 runner and pushes `:<git-sha>` and `:latest` to GHCR.
3. **deploy:** connects as `harmoniq`, passes the commit SHA and a short-lived token for pulling the image, then checks
   `/healthz` over the public URL.

### 9.1 Adapt the workflow to your fork

| Line in `deploy.yml` | Change it to |
|----------------------|--------------|
| `IMAGE: ghcr.io/anujyadav-dev/harmoniq-server` | `ghcr.io/<owner>/harmoniq-server` |
| `curl … https://harmoniq-server.curio.dpdns.org/healthz` | Your hostname |
| `runs-on: ubuntu-24.04-arm` and `platforms: linux/arm64` | On an x86 VM: `ubuntu-latest` and `linux/amd64` |

The `ubuntu-24.04-arm` runner is tested on a **public** repository. If it isn't available for yours, stay on
`ubuntu-latest`, add `docker/setup-qemu-action@v3` before Buildx and keep `linux/arm64`. That works but is slower.

### 9.2 Create a restricted deploy key

On your own computer:

```bash
ssh-keygen -t ed25519 -N "" -C "github-actions-harmoniq-deploy" -f harmoniq_deploy_key
```
Creates `harmoniq_deploy_key` (private) and `harmoniq_deploy_key.pub`.

On the server, create `/opt/harmoniq/.ssh/authorized_keys` as the `harmoniq` user, with one line:

```text
restrict,command="/opt/harmoniq/deploy.sh" ssh-ed25519 AAAA… github-actions-harmoniq-deploy
```

```bash
sudo chmod 600 /opt/harmoniq/.ssh/authorized_keys
```
Sets the permissions SSH requires.

Test it from your computer. The first command should print a status line and the second a usage message, because the
key can't run anything else:

```bash
ssh -i harmoniq_deploy_key harmoniq@<server-ip> status
```

```bash
ssh -i harmoniq_deploy_key harmoniq@<server-ip> "ls /"
```

```bash
ssh-keyscan -t ed25519 <server-ip>
```
Prints the host key line for the next step. Compare it with the server's own key if you want to rule out interception.

### 9.3 Add the repository secrets

*Settings → Secrets and variables → Actions → New repository secret*:

| Secret | Value |
|--------|-------|
| `OCI_HOST` | `<server-ip>` |
| `OCI_KNOWN_HOSTS` | The line printed by `ssh-keyscan` |
| `OCI_DEPLOY_KEY` | The full contents of `harmoniq_deploy_key` |

The `OCI_` prefix is only a name; these work for any provider. Then delete the private key from your computer. If
you need a new one, generate it and replace the `authorized_keys` line.

### 9.4 Start the helper containers and deploy

```bash
sudo -u harmoniq sh -c 'cd /opt/harmoniq && docker compose --profile pot up -d warp pot-provider'
```
Starts WARP and the PO-token provider. Starting WARP registers a free device, which accepts
[Cloudflare's WARP terms](https://www.cloudflare.com/application/terms/). CI deploys only replace the `server`
container, so these keep running.

Push to `main`, or use *Run workflow*. The first run takes about 5–10 minutes.

```bash
sudo -u harmoniq /opt/harmoniq/deploy.sh status
```
Should show the new commit and `health=healthy`.

## 10. Verify

Run the checks in [deployment-guide.md §8](deployment-guide.md#8-verify-from-outside) from any computer, ideally on
another network. Also confirm that port 8000 isn't reachable:

```bash
curl -m 5 http://<server-ip>:8000/healthz
```
Must fail to connect.

Then confirm your other sites still respond.

## 11. YouTube access on a datacenter IP

Search can look fine while downloads fail, because song search doesn't go through yt-dlp. **Always test a real
download.** What was measured on the OCI server:

| Setup | Download result |
|-------|-----------------|
| Oracle IP only | Blocked: `Sign in to confirm you're not a bot` |
| Oracle IP + PO-token provider | Still blocked |
| Oracle IP + valid cookies | Worked at first, then the cookies were invalidated (see below) |
| **WARP + PO-token provider, no cookies** | **Worked: 3 of 3 test videos on 2026-11-06** |
| WARP + PO-token provider + cookies | Works |

So try **without cookies first**. They are the part that expires and they put a Google account at risk. Three videos
is a small sample and YouTube's behaviour changes, so turn on the alert email (§12) rather than relying on this table.

### 11.1 What goes through WARP

`YTDLP_PROXY` applies to everything yt-dlp does: video and playlist extraction, and the audio download. Song search,
album lookups and lyrics (ytmusicapi), MusicBrainz and LRCLIB connect directly from the server.

```bash
sudo docker exec harmoniq-server-1 curl -s --socks5-hostname warp:1080 https://cloudflare.com/cdn-cgi/trace | grep -E '^(ip|warp)='
```
Must show `warp=on` and an IP that isn't your server's.

### 11.2 Test a download without touching the configuration

```bash
sudo docker exec harmoniq-server-1 sh -c 'cd /tmp && python -m yt_dlp --no-cache-dir --proxy socks5://warp:1080 --force-ipv4 --extractor-args "youtubepot-bgutilhttp:base_url=http://pot-provider:4416" -f bestaudio -o /tmp/probe.webm --no-progress jNQXAC9IVRw; rm -f /tmp/probe.webm'
```
Downloads a 19-second video through WARP with no cookies. `Download completed` means cookies aren't needed right now.

### 11.3 Add cookies only if you need them

> **Use a throwaway Google account, never your own.** Accounts used for automated downloads can be suspended.

Set up WARP first, so that the new session is never used from your flagged datacenter IP. Then:

1. Open a **new private/Incognito window**. Allow your cookie-export extension there.
2. Sign in with the throwaway account, open youtube.com and play a video for a few seconds.
3. Export the **youtube.com cookies only**, in **Netscape** format. Don't use "export all".
4. **Close the window at once, without signing out, and never open that session again.**

Add this line to `.env` the first time, then recreate the server (§12):

```dotenv
YTDLP_COOKIES_FILE=/run/secrets/cookies.txt
```

Then install the file with the [Update cookies action](#114-update-cookies-from-github), and delete your local copy.

**Why cookies stop working, and how to tell:**

- **The browser kept using the session.** A normal window that's still browsing YouTube makes Google rotate the
  session, and the exported copy dies within minutes. This is the most common mistake.
- **yt-dlp rewrites the file every time it finishes using it.** Once a session is rejected, the rewrite drops the
  sign-in cookies for good. The file shrinks and those downloads run logged out.
- **A job running during an update can overwrite the new file**, because it saves its own copy when it finishes. The
  Update cookies action avoids this by stopping the server while it swaps the file.

```bash
sudo docker logs harmoniq-server-1 2>&1 | grep -c "no longer valid"
```
Anything above 0 since the last upload means the cookies were rejected.

```bash
sudo awk -F'\t' '!/^#/ && $6=="SID"' /opt/harmoniq/secrets/cookies.txt | wc -l
```
`0` means the file has lost its login. Export again.

### 11.4 Update cookies from GitHub

[`.github/workflows/update-cookies.yml`](../.github/workflows/update-cookies.yml) sends the `YTDLP_COOKIES` secret to the
server, which runs `update-cookies.sh`:

1. Checks that the file is a Netscape cookies file with YouTube cookies and a signed-in session.
2. Tests a copy with YouTube through the same proxy the server uses.
3. Stops the server, swaps the file, keeps the old one as `secrets/cookies.txt.prev`, and starts the server.

If a check fails, the old cookies stay untouched and the action shows as failed. Running downloads are lost when the
server stops; the app starts them again.

**Use a Secret, not a Variable.** Cookies work like a login, and a Variable isn't hidden. The repository being public
doesn't expose the Secret: only people with write access can run the action, and forks don't get it.

**Set it up once.** On your own computer:

```bash
ssh-keygen -t ed25519 -N "" -C "github-actions-harmoniq-cookies" -f harmoniq_cookies_key
```
Creates a key just for this action.

On the server, create `/opt/harmoniq/update-cookies.sh` from [scripts/update-cookies.sh](../scripts/update-cookies.sh), then
make root its owner so that the `harmoniq` user can't edit it:

```bash
sudo chown root:root /opt/harmoniq/update-cookies.sh && sudo chmod 755 /opt/harmoniq/update-cookies.sh
```

Add a second line to `/opt/harmoniq/.ssh/authorized_keys`, so this key can only run that script:

```text
restrict,command="/opt/harmoniq/update-cookies.sh" ssh-ed25519 AAAA… github-actions-harmoniq-cookies
```

Add these repository secrets, then delete the private key from your computer. The action also uses `OCI_HOST` and
`OCI_KNOWN_HOSTS` from §9.3.

| Secret | Value |
|--------|-------|
| `OCI_COOKIES_KEY` | The full contents of `harmoniq_cookies_key` |
| `YTDLP_COOKIES` | The full contents of your exported `cookies.txt` |

**Each time cookies expire:**

1. Export new cookies (§11.3).
2. *Settings → Secrets and variables → Actions → `YTDLP_COOKIES` → Update*, and paste the file's contents.
3. *Actions → Update cookies → Run workflow*.
4. Delete your local copy.

## 12. Day-to-day operations

Run these on the server.

| Task | Command |
|------|---------|
| Deploy | `git push` to `main`, or *Actions → Deploy → Run workflow* |
| Status | `sudo -u harmoniq /opt/harmoniq/deploy.sh status` |
| Roll back | `sudo -u harmoniq /opt/harmoniq/deploy.sh rollback` |
| Deploy a specific version | `sudo -u harmoniq /opt/harmoniq/deploy.sh deploy <git-sha>` (the image must be public or already on the server) |
| Logs | `sudo docker logs -f harmoniq-server-1` · `/var/log/nginx/harmoniq.*.log` |
| Restart | `sudo docker restart harmoniq-server-1` (running jobs are lost and the app resubmits them) |
| Apply a `.env` or `compose.yml` change | `sudo -u harmoniq sh -c 'cd /opt/harmoniq && TAG=$(cat .tag) docker compose up -d --no-deps server'` |
| Resource use | `sudo docker stats --no-stream` |
| Is YouTube working? | `GET /v1/meta` → `status.youtube` (`ok`, `degraded`, `blocked`) |
| Update cookies | *Actions → Update cookies → Run workflow* (§11.4) |

**Include `TAG=$(cat .tag)`** when you run `docker compose up` by hand. Without it, Compose uses `:latest`, which may not
be the version `deploy.sh` thinks is running.

**Updating yt-dlp.** When downloads fail with `upstream_error`, YouTube has usually changed something and yt-dlp needs
updating. Every build installs the newest version, so use *Actions → Deploy → Run workflow*. Check `components.yt_dlp` in
`/v1/meta` afterwards.

**Alert email.** The server emails you when 3 downloads are blocked by YouTube within 30 minutes, then stays quiet for
6 hours. Restarting the server resets the count. It only reacts to real downloads, so you find out after one fails.

1. Create a Gmail for sending, separate from your main account and from the account the cookies come from. A server
   problem then can't expose your main mailbox, and alerts keep working if Google suspends the cookie account.
2. Turn on 2-Step Verification for it and create an
   [App Password](https://support.google.com/accounts/answer/185833).
3. Add the settings to `/opt/harmoniq/.env` (no spaces in the password), then apply the change (table above):

   ```dotenv
   SMTP_HOST=smtp.gmail.com
   SMTP_PORT=587
   SMTP_USER=sender@gmail.com
   SMTP_PASSWORD=xxxxxxxxxxxxxxxx
   ALERT_EMAIL_TO=you@example.com
   ```

4. Send a test:

   ```bash
   sudo docker exec harmoniq-server-1 python -m app.alerts
   ```
   Prints `Test email sent` or the reason it failed.

Many cloud providers block port 25. Port 587 works from OCI.

**Rotating the app token:** [deployment-guide.md §11](deployment-guide.md#11-rotating-the-app-token), using the
`.env` command above in place of its restart command.

**Backups.** Keep a copy of `.env` in a password manager. The rest can be recreated from this guide: `compose.yml`,
`deploy.sh`, `update-cookies.sh`, the Nginx site and the certificate. `data/` is a cache; `warp/` and `secrets/cookies.txt` are recreated by
starting WARP and exporting again.

**Removing Harmoniq completely** (nothing else on the host is affected):

```bash
sudo -u harmoniq sh -c 'cd /opt/harmoniq && docker compose --profile pot down'
```
Stops and removes its containers and network.

```bash
sudo rm /etc/nginx/sites-enabled/harmoniq-server && sudo nginx -t && sudo systemctl reload nginx
```
Removes the site.

```bash
sudo certbot delete --cert-name harmoniq.example.com
```
Removes the certificate. To finish, delete `/opt/harmoniq` and the `harmoniq` user.

## 13. Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `502` from Nginx | Container down or still starting | `deploy.sh status`, `sudo docker logs harmoniq-server-1` |
| Certbot fails | DNS not pointing here yet, port 80 closed, or Cloudflare proxy on | §8 step 1, §2 |
| CI `test` fails with `No module named 'app'` | Bare `pytest` instead of `python -m pytest` | Use the workflow as committed |
| CI `deploy` fails with `Permission denied (publickey)` | Wrong key in `OCI_DEPLOY_KEY`, or `authorized_keys` mode or owner | §9.2 |
| CI `deploy` fails with `Host key verification failed` | `OCI_KNOWN_HOSTS` is wrong or the server was rebuilt | Run `ssh-keyscan` again, §9.3 |
| CI `deploy` fails with `manifest unknown` or `denied` | `<owner>` differs between workflow, `compose.yml` and `deploy.sh`, or isn't lower case | §5, §7, §9.1 |
| `deploy.sh` reports a failed health check | The new image doesn't start; the old one is back automatically | Read the printed log lines. Common cause: a bad `.env` value |
| Containers can't reach the internet after a firewall change | `netfilter-persistent reload` removed Docker's rules | `sudo systemctl restart docker` (briefly restarts the containers) |
| Network error about IPv6 when creating the network | The repository's `docker-compose.yml` was used on a host without IPv6 | Use the `compose.yml` from §5 |
| Downloads fail with `upstream_blocked`, `Sign in to confirm` | The IP is bot-checked or the cookies died | §11.1 to confirm WARP is on, §11.2 to test, then §11.3 |
| `HTTP Error 403` on `video data` | Usually the same: the session or the IP was rejected | As above, then update yt-dlp (§12) |
| Update cookies action fails | The new cookies were rejected, usually because the browser session kept running | Read the action's log, export again in a fresh Incognito window (§11.3). The old cookies are still in place |
| No alert email | Not all four settings are set, or the login failed | `sudo docker exec harmoniq-server-1 python -m app.alerts`; `sudo docker logs harmoniq-server-1 2>&1 \| grep -i alert` |
| Downloads fail with `upstream_error` | YouTube changed something | Update yt-dlp (§12) |
| WARP container restarting or `warp=off` | Cloudflare connection problem or out of memory | `sudo docker logs harmoniq-warp-1`; `docker stats` against its 384 MB limit |
| Search works, downloads don't | Normal for this failure: song search doesn't use yt-dlp | §11 |
| The host feels slow during downloads | Limits too high for the machine | Lower `cpus` (§5), keep `MAX_CONCURRENT_DOWNLOADS=1` |

App-side messages are explained in the app's
[troubleshooting table](https://github.com/AnujYadav-Dev/Harmoniq/blob/main/docs/09-global-search-server.md#troubleshooting-global-search).

## 14. Connect the app

In the Harmoniq app repository, set `server_url` and `app_token` in
[`config/global-search.json`](https://github.com/AnujYadav-Dev/Harmoniq/blob/main/config/global-search.json) on `main`
([deployment-guide.md §9](deployment-guide.md#9-connect-the-android-build)). No rebuild is needed. That file is public,
so its token is a public app key: it keeps out bots, and the server's rate limits do the rest.

Wait about 5 minutes for GitHub's cache, reopen the app, switch to **Global**, then search and download a song.
