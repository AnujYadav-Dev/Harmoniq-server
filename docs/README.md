# harmoniq-server documentation

**harmoniq-server** is the backend that lets the Harmoniq Android music player search YouTube, preview a
result, and save its audio, with full tags and cover art, into the app's music folder.

> **Status (2026-11-03): v0.3.0 implemented** (playlist search, `type=all`, `/v1/playlists/{id}`). The server runs locally and in Docker, tested end to end
> against real YouTube, with full song metadata (album, track number, release date, genres, ISRC, synced
> lyrics, cover art). The Android app's **Global Search is implemented** against it (staged in the app repo, not yet
> committed; device and signed-build tests still open, see
> [android-integration-plan.md §17](android-integration-plan.md#17-implementation-status-and-deviations)).
> Quick start: [../README.md](../README.md). First deployment: [deployment-guide.md](deployment-guide.md).
>
> **Scope:** a **personal project** used by the owner and a few testers who get the APK directly. It isn't
> public and isn't a service. The design is deliberately small: **one process, no Redis, no database**.

---

## The feature in one paragraph

The user searches inside Harmoniq. The server queries YouTube Music (songs) or YouTube (videos) and returns
results with title, artist, album, duration and artwork. Tapping a result plays it in YouTube's **official
embedded player** on the phone so the user can check it's the right one. Tapping *Download* creates a **job** that
the server runs in the background. It uses **yt-dlp** to fetch the best audio stream, **FFmpeg** to remux it (or
transcode it, if asked) into `m4a`/`opus`/`mp3`, and **mutagen** to embed tags and square cover art. The app then
downloads the finished file and writes it into the user's music folder, where Harmoniq's existing library scanner
picks it up.

## Recommended stack at a glance

| Concern | Choice |
|---------|--------|
| API | Python ≥ 3.11 (3.13 recommended) · FastAPI · Uvicorn (**single process**) |
| Song search | **ytmusicapi** (YouTube Music, structured artist/album) |
| Video search, metadata, download | **yt-dlp** + **Deno** (JS challenges); PO-token provider only if needed |
| Audio processing | **FFmpeg/ffprobe** (remux-first; default output **m4a/AAC**, no transcode) |
| Tagging / artwork | **mutagen** + Pillow |
| Background jobs | **In-process asyncio job manager** (2 downloads at a time); no Redis, no broker |
| Caches, rate limits | In memory (`cachetools`) |
| Persistence | **None.** Finished files + JSON sidecars in `data/`. SQLite later if history is wanted |
| Local run | One folder: `.venv/` (from your installed Python), `tools/` (FFmpeg, Deno), `data/` |
| Server run | **One Docker container** (`docker compose up -d`), same `.env` |
| App → server | HTTPS URL + **secret app token** built into the APK; Cloudflare Tunnel recommended for HTTPS |
| Preview (app side) | Official IFrame player via `android-youtube-player` 13.1.0 |

The reasoning for each choice is in [research.md §7](research.md#7-recommendation).

## Documents and reading order

| # | Document | Read it to learn… |
|---|----------|-------------------|
| 1 | [research.md](research.md) | Every library/project evaluated, with pros, cons, licences, API limits, the risk register, and **why the stack above was chosen** |
| 2 | [architecture.md](architecture.md) | The single-process design, folder layout, data flow, job state machine, file lifecycle, what happens on restart, extension roadmap |
| 3 | [api.md](api.md) | Every endpoint with request/response JSON, error codes, headers, and a curl walkthrough |
| 4 | [download-flow.md](download-flow.md) | Step-by-step search → preview → download → FFmpeg → tagging → delivery, with exact yt-dlp/FFmpeg settings, retries and failure handling |
| 5 | [deployment.md](deployment.md) | Reference: local mode (one folder, Python venv) and server mode (Docker), env vars, YouTube-access tips, updating yt-dlp, how the app reaches the server, running 24/7 |
| 5a | [deployment-guide.md](deployment-guide.md) | **Step-by-step first deployment** with Docker Compose + Cloudflare Tunnel: verify, connect the app, update, rotate tokens, move machines, troubleshoot, remaining work |
| 6 | [server-implementation-plan.md](server-implementation-plan.md) | **Metadata & quality plan (v0.2.0)**: what the old notebook stored, what Harmoniq reads, providers, tag mapping, API changes |
| 7 | [android-integration-plan.md](android-integration-plan.md) | **Android integration** (implemented): Global Search, preview, download, saving to `Download/Harmoniq`, library detection, testing, rollout, deviations from the plan |

How they fit together:

```text
research.md ──(decides dependencies)──▶ architecture.md ──(defines components)──┬──▶ api.md            (contract)
                                                                               ├──▶ download-flow.md  (behaviour)
                                                                               └──▶ deployment.md     (operations)
                                                                                      └──▶ deployment-guide.md (first deployment, step by step)
```

## Things to know

1. **Personal use only.** Testers get the APK from you; the server only accepts requests carrying the app's secret
   token, over HTTPS, and the URL is never published. The download feature stays out of any Google Play build
   ([research.md §6.1](research.md#61-legal--terms-of-service--read-this-first)).
2. **Run it from home.** YouTube often bot-checks cloud/datacenter IPs. Your own PC or a home server avoids most
   of that ([deployment.md §2](deployment.md#2-where-to-host-it)).
3. **Update yt-dlp regularly.** It's one command in both modes
   ([deployment.md §7](deployment.md#7-keeping-yt-dlp-current)).
4. **One process.** Job state is in memory, so run exactly one server process. A restart forgets running jobs (the
   app resubmits) but keeps finished files.
5. **Preview never goes through the server.** YouTube stream URLs are bound to the IP that requested them, so the
   phone uses the official embed player instead.
6. **Licence.** Harmoniq is AGPL-3.0. The server should be AGPL-3.0 too, which is compatible with the
   GPL-licensed dependencies (mutagen, the PO-token plugin).

## Next steps

The server and the app integration are implemented. What's left is deployment and testing:

1. Deploy the server with a stable HTTPS URL: [deployment-guide.md](deployment-guide.md) (§16 lists the steps in
   order).
2. Build a signed `canary` APK against it and run the test matrix in
   [android-integration-plan.md §16](android-integration-plan.md#16-validating-the-complete-flow-before-testers-get-it),
   including Android 9 (API 28) and Android 14 (API 34) devices.
3. Commit both repositories and add a `LICENSE` file (AGPL-3.0-or-later) to this one.
4. Remaining work is tracked in [deployment-guide.md §16](deployment-guide.md#16-next-steps--remaining-work).
