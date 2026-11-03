# Android Integration Plan: Global Search & downloads in Harmoniq

[← Docs index](README.md) · Companion document: [Server Implementation Plan](server-implementation-plan.md)

> Status (2026-11-03): **implemented** in the app (Harmoniq 1.3.0 source, staged on the `dev` branch, not yet
> committed). JVM unit tests pass; device tests, signed `canary`/`release` builds and most of the §16 matrix are
> still open. What differs from this plan and what is unverified: [§17](#17-implementation-status-and-deviations).
> The plan text below is kept as the design record; code snippets are illustrative. App-side setup and
> troubleshooting: the app's [docs/09-global-search-server.md](https://github.com/AnujYadav-Dev/Harmoniq/blob/main/docs/09-global-search-server.md). Server setup: [deployment-guide.md](deployment-guide.md).

---

## 1. User experience

1. The **Search** tab gets a **Library | Global** switch next to the search bar.
2. With **Global** selected, typing searches the Harmoniq server (YouTube Music songs, optionally YouTube videos).
3. Tapping a result opens a **preview sheet**: cover, title, artists, album, track number, year, and the official
   YouTube player to listen and check it's the right song.
4. **Download** queues the song. The server processes it into a high-quality, fully tagged file.
5. The app shows progress (a notification and a row in the sheet/list). The file is downloaded and verified.
6. The file is saved to **`Download/Harmoniq/`** on the phone.
7. It appears in the library with its title, artists, album, cover, track number, genre, year and synced lyrics,
   because all of it is **embedded in the file** by the server.

## 2. What already exists in the app (relevant parts)

| Area | Existing code | How Global Search uses it |
|------|---------------|---------------------------|
| App-wide state | `Symphony` (AndroidViewModel) owns `settings`, `groove`, `radio`, `database`; UI reaches it via `ViewContext.symphony` | Add `symphony.globalSearch` the same way |
| Search UI | `ui/view/SearchTabContent.kt`: one `TextField`, chips per kind, 250 ms debounce, local fuzzy search | Add the Library/Global switch and a global results list |
| Networking | OkHttp 4.12 (`HttpClient`, used by `AppMeta` for the GitHub release check), `kotlinx-serialization-json` | Reuse both; no Retrofit needed |
| Images | Coil (`coil-compose`) | Result thumbnails and preview cover |
| Library | `MediaExposer` scans the **SAF tree URIs** in `settings.mediaFolders`, keeps files whose MIME type is `audio/*`, parses tags with `:metaphony` (TagLib) | Downloads must land in a folder covered by `mediaFolders` (§11) |
| Tags read | `AudioMetadataParser`: `TITLE`, `ARTIST`, `ALBUMARTIST`, `ALBUM`, `GENRE`, `TRACKNUMBER`/`TRACKTOTAL`, `DISCNUMBER`, `DATE`, `LYRICS`, first embedded picture | The server writes exactly these (verified with TagLib) |
| Lyrics | Same-named `.lrc` next to the song, else the embedded `LYRICS` tag (lyrics cache) | Synced lyrics are embedded; a `.lrc` isn't needed |
| Playback | `Radio` wraps `android.media.MediaPlayer`; `radio.pause()` | Pause it while a preview plays |
| Refresh | `groove.fetch(Groove.FetchOptions(...))`; the scan cache skips unchanged files (by `lastModified`) | Trigger after each download |
| Permissions | `INTERNET`, `POST_NOTIFICATIONS`, `FOREGROUND_SERVICE(_MEDIA_PLAYBACK)` | Add data-sync foreground service + legacy storage for API 28 (§13) |
| minSdk / target | 28 / 34 | Two storage code paths (API 28 vs 29+) |

## 3. Architecture

```text
ui/view/SearchTabContent.kt ──┐               (Compose; existing tab, new "Global" mode)
ui/view/settings/GlobalSearchSettingsView.kt   (format, .lrc, folder hint, server status)
ui/view/globalsearch/         │
  GlobalSearchResults.kt      │  state: StateFlow
  GlobalPreviewSheet.kt ──────┤
  GlobalDownloadControls.kt   │  download button, progress, format picker
  YouTubePreviewPlayer.kt     │
  LibraryLinkPrompt.kt        │
                              ▼
services/globalsearch/  (new; GlobalSearch is created by Symphony like groove/radio)
  GlobalSearch.kt            facade: search(), meta, enqueueDownload(), downloads, library prompt, rescans
  GlobalSearchServices.kt    builds the API client from a plain Context (UI and worker); "revoked" flag
  GlobalSearchMessages.kt    error code → user message
  ServerConfig.kt            base URL, app token, enabled flag (BuildConfig) + timeouts and limits
  DeviceId.kt                random UUID per install (SharedPreferences "global_search"), sent as X-Device-Id
  api/HarmoniqServerApi.kt   OkHttp calls + error mapping (problem+json → ServerError)
  api/Dto.kt, ServerError.kt @Serializable models mirroring docs/api.md (ignoreUnknownKeys = true); error codes
  download/ServerDownloadWorker.kt   WorkManager CoroutineWorker that runs DownloadPipeline
  download/DownloadPipeline.kt  submit → poll → fetch → verify → save
  download/DownloadSaver.kt  MediaStore (API 29+) / legacy file (API 28) writer for Download/Harmoniq
  download/LibraryLink.kt    "is Download/Harmoniq covered by a media folder?"
  download/DownloadRecords.kt  small persisted list (SharedPreferences JSON) for the UI and dedupe
  download/DownloadNotifications.kt, DownloadErrorPolicy.kt, DownloadProgressData.kt, FileNames.kt
```

Design rules:

- **The server does all the tagging.** The app never writes tags. It downloads, verifies the SHA-256, and saves
  the bytes as they are.
- **Downloads run in WorkManager**, not in the UI or the playback service. They survive the app being closed and
  retry on network loss, and the system shows a progress notification.
- **Feature flag:** Global Search exists only in builds that have a server URL and token (`BuildConfig`), so other
  builds stay purely offline.

## 4. Configuration and secrets

| Value | Where it comes from | Notes |
|-------|--------------------|-------|
| `HARMONIQ_SERVER_URL` | env var or `local.properties` → `buildConfigField` | Per build type via `HARMONIQ_SERVER_URL_<BUILDTYPE>`, e.g. `_DEBUG` → your PC, `_CANARY`/`_RELEASE` → the tunnel URL |
| `HARMONIQ_APP_TOKEN` | same (never committed, like the `SIGNING_*` secrets) | The secret part of one `APP_TOKENS` entry. Rotating it cuts off old APKs |
| enabled | derived in `ServerConfig.isEnabled`: the URL parses and the token is non-empty | Hides the Library/Global switch and the settings entry otherwise |

As implemented in `app/build.gradle.kts`:

```kotlin
fun secret(name: String): String = System.getenv(name) ?: localProperties.getProperty(name) ?: ""
fun buildTypeSecret(name: String, buildType: String): String =
    secret("${name}_${buildType.uppercase()}").ifEmpty { secret(name) }

buildTypes.configureEach {
    listOf("HARMONIQ_SERVER_URL", "HARMONIQ_APP_TOKEN").forEach { key ->
        buildConfigField("String", key, buildConfigString(buildTypeSecret(key, name)))
    }
}
```

The keys are documented in the app's [docs/06-configuration-and-secrets.md](https://github.com/AnujYadav-Dev/Harmoniq/blob/main/docs/06-configuration-and-secrets.md) and [docs/09-global-search-server.md](https://github.com/AnujYadav-Dev/Harmoniq/blob/main/docs/09-global-search-server.md).

## 5. API endpoints used

All requests send `X-App-Token` and `X-Device-Id`. Full contract: [api.md](api.md).

| When | Endpoint | Purpose |
|------|----------|---------|
| App start (Global mode first opened) | `GET /v1/meta` | Server reachable? Formats, limits, `status.youtube` (`ok`/`degraded`/`blocked` → banner) |
| Typing | `GET /v1/search?q=&type=song&limit=20` | Results (`type=video` for the "Videos" chip) |
| Preview sheet | `GET /v1/videos/{id}/track` | Album, track n/total, year, genres, cover URL, suggested filename |
| Preview sheet | `GET /v1/videos/{id}/preview` | `embeddable`, `embed_url`, `age_restricted` |
| Download tap | `POST /v1/downloads` (+ `Idempotency-Key`) | Start the job; `202` with `job_id` (or `cache_hit: true`, already done) |
| While processing | `GET /v1/downloads/{job_id}` every 2 s (or `/events` SSE) | Status, progress, then final `file` + `metadata` |
| Done | `GET /v1/downloads/{job_id}/file` (with `Range` on resume) | The tagged audio |
| After saving | `DELETE /v1/downloads/{job_id}` | Free the server copy |
| Optional | `GET /v1/downloads/{job_id}/lyrics` | Save a `.lrc` too (off by default; lyrics are embedded) |

## 6. Search flow (Global Search)

```text
TextField ──(debounce 400 ms, ≥ 2 chars, cancel previous)──▶ GlobalSearch.search(q, type)
   │                                                          └─ GET /v1/search
   ▼
results: List<SearchResultDto>  → LazyColumn rows: cover (Coil), title, "Artist · Album · 3:45", explicit badge,
                                   a "Downloaded" badge when DownloadRecords has this videoId
```

- **Switch state:** `Library | Global` is remembered with `rememberSaveable` plus a settings entry. Library mode
  keeps today's behaviour exactly.
- **Chips in Global mode:** `Songs` (default, `type=song`) and `Videos` (`type=video`, for covers, live sessions and
  uploads that aren't in the Music catalogue).
- **Debounce at 400 ms** (instead of 250 ms locally) and skip identical queries. The server allows 30 searches per
  minute per device and caches repeated queries.
- **Empty and error states:** "No results", "Server unreachable" (retry), "Rate limited, try again in N s" (from
  `Retry-After`), and a banner when `/v1/meta` says YouTube is `blocked`.

## 7. Preview / playback flow

```text
tap result ─▶ GlobalPreviewSheet (ModalBottomSheet)
               ├─ immediately: data from the search row (title, artists, cover)
               ├─ GET /track   → album · "Track 8 of 13" · year · genres · lyrics available
               ├─ GET /preview → embeddable?
               └─ YouTubePlayerView (android-youtube-player, Apache-2.0)
                    on play: symphony.radio.pause()   (one audio source at a time)
                    on sheet close: player.release()
              [ Download ▾ ]  (format picker: Best quality (Opus) · Compatible (M4A) · MP3)
```

- Use **`com.pierfrancescosoffritti.androidyoutubeplayer:core`**. It loads the official IFrame player in a
  WebView with a proper origin, which avoids YouTube's **error 153** (missing referrer) and the WebView
  configuration work.
- If `embeddable == false` or the player reports an error, show the cover and an **Open in YouTube** button.
  Download still works.
- The preview never goes through the server: YouTube's stream URLs are tied to the IP that requested them.

## 8. Metadata handling

The server embeds everything; the app only reads it with its existing scanner. This is what arrives in each file
and how Harmoniq uses it:

| Tag (TagLib key) | Example | Harmoniq field |
|------------------|---------|----------------|
| `TITLE` | Get Lucky (feat. Pharrell Williams and Nile Rodgers) | title |
| `ARTIST` | `Daft Punk; Pharrell Williams; Nile Rodgers` | artists (split on `;`, a default separator) |
| `ALBUMARTIST` | Daft Punk | album artist |
| `ALBUM` | Random Access Memories | album |
| `TRACKNUMBER` (+`TRACKTOTAL` in Opus) | `8/13` | track number / total |
| `DISCNUMBER` | `1/1` | disc number |
| `DATE` | `2013-05-17` | date + year |
| `GENRE` | `Electronic; Disco; Funk` | genres (split on `;`) |
| `LYRICS` | `[00:30.000]First lyric line…` | lyrics cache → synced lyrics view |
| picture | 1200×1200 JPEG front cover | artwork cache |
| `ISRC`, `ARTISTS`, `COMMENT`, `HARMONIQ_SOURCE`, MusicBrainz IDs | – | not read today (available for future features) |

**Two small app fixes to make in the same change** (both found while designing the server output):

1. **Year-only dates are dropped.** `AudioMetadataParser.parseDate` parses `"2013"` with `LocalDate.parse(text,
   "yyyy")`, which always throws (a `LocalDate` needs month and day), and `DATE_YEAR_MONTH` also uses `"yyyy"`.
   The server writes full dates when it knows them, but some songs only have a year. Fix:

   ```kotlin
   private fun parseDate(text: String): LocalDate? =
       runCatching { LocalDate.parse(text) }.getOrNull()                          // 2013-05-17
           ?: runCatching { YearMonth.parse(text).atDay(1) }.getOrNull()          // 2013-05
           ?: runCatching { Year.parse(text.take(4)).atDay(1) }.getOrNull()       // 2013
   ```

2. **LRC fractions are read as milliseconds.** `TimedContent.fromLyrics` does `plusMillis(groupValues[3])`, so the
   standard `[00:12.34]` becomes 12.034 s instead of 12.340 s. The server already writes `[mm:ss.mmm]`
   (unaffected), but existing `.lrc` files are slightly off. Fix: scale by digit count
   (`frac.padEnd(3, '0').take(3).toLong()`).

Also recommended: the `HARMONIQ_SOURCE` tag (`youtube:<videoId>`) lets the app tell which library songs came from
Global Search. Use it later for a "Downloaded" badge that survives reinstalls, by reading it in `:metaphony`.

## 9. Download flow

```text
Download tap
  └─ GlobalSearch.enqueueDownload(videoId, format, overrides)
       WorkManager.enqueueUniqueWork("dl-$videoId-$format", KEEP, ServerDownloadWorker)
         constraints: network CONNECTED · backoff: exponential 30 s · setForeground(progress notification)

ServerDownloadWorker.doWork()
  1. POST /v1/downloads {video_id, format, metadata?: from the search row}   Idempotency-Key = workId
       202 → job_id   (cache_hit → skip to step 3)
  2. poll GET /v1/downloads/{job_id} every 2 s  → setProgress(status, progress) → notification + UI
       failed → map error (§12): retryable → Result.retry(); else Result.failure(code)
       404 job_not_found (server restarted) → back to step 1
  3. GET /file → stream to cacheDir/downloads/<job_id>.part  (Range: bytes=<size>- when resuming)
  4. verify size == file.size_bytes and SHA-256 == file.sha256      (mismatch → delete .part, retry)
  5. DownloadSaver.save(part, file.filename, file.mime_type)  → Download/Harmoniq/<name>   (§10)
  6. DELETE /v1/downloads/{job_id}
  7. LibraryLink.onSongSaved(uri)  → rescan (and the one-time folder prompt if needed, §11)
  8. DownloadRecords.markDone(videoId, savedUri, metadata)  → "Downloaded" badge; success notification
```

- **Format default:** `opus` on Android 10+ (`Build.VERSION.SDK_INT >= 29`), which is the best source quality
  (~130–160 kbps Opus, no re-encoding). Use `m4a` on Android 9. Make it a setting: *Download format: Best (Opus) ·
  Compatible (M4A) · MP3*.
- **Overrides:** pass `metadata.artwork_url` (the search result's square art) and nothing else. The server's own
  resolution is better than what the search row knows.
- **Concurrency:** WorkManager runs them in parallel, but the server caps a device at 3 active jobs (`429
  too_many_active_jobs` → retry later). Queue up to about 20 locally.
- **Cancel:** the notification action calls `WorkManager.cancelWorkById` → the worker's `finally` sends `DELETE`
  for the job.

## 10. File naming and folder structure

```text
/storage/emulated/0/Download/Harmoniq/
    Daft Punk - Get Lucky (feat. Pharrell Williams and Nile Rodgers).opus
    Coldplay - Yellow.opus
    Coldplay - Yellow (2).m4a       ← name collision, different file
```

- **Name:** use `file.filename` from the server (`FILENAME_TEMPLATE`, default `{artist} - {title}.{ext}`). It's
  already sanitised for FAT/Android: no `\ / : * ? " < > |`, at most 150 characters.
- **Collisions:** if a file with that name exists, compare its size and SHA-256 (stored in `DownloadRecords`).
  Same file → keep it and mark done. Different file → append ` (2)`, ` (3)`…
- **Flat folder**, no per-artist subfolders: Harmoniq groups by tags anyway, and a flat folder is easy to find in
  any file manager.
- **Lyrics:** embedded. Optional setting "Also save .lrc files" writes `<same name>.lrc` from `/lyrics`. Harmoniq
  prefers a same-named `.lrc` when present.

```kotlin
// DownloadSaver: API 29+ (no storage permission needed)
val values = ContentValues().apply {
    put(MediaStore.Downloads.DISPLAY_NAME, fileName)
    put(MediaStore.Downloads.MIME_TYPE, mimeType)            // audio/ogg, audio/mp4, audio/mpeg
    put(MediaStore.Downloads.RELATIVE_PATH, "${Environment.DIRECTORY_DOWNLOADS}/Harmoniq")
    put(MediaStore.Downloads.IS_PENDING, 1)
}
val uri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)!!
resolver.openOutputStream(uri)!!.use { out -> part.inputStream().use { it.copyTo(out) } }
resolver.update(uri, ContentValues().apply { put(MediaStore.Downloads.IS_PENDING, 0) }, null, null)
// API 28: File(Environment.getExternalStoragePublicDirectory(DIRECTORY_DOWNLOADS), "Harmoniq/$fileName")
//         + WRITE_EXTERNAL_STORAGE (maxSdkVersion=28) + MediaScannerConnection.scanFile(...)
```

## 11. How the downloaded song reaches the library

Harmoniq only scans the folders in **Settings → Groove → Media folders** (SAF tree URIs). MediaStore and
"Downloads" visibility don't matter. So:

1. **Coverage check** (`LibraryLink.isCovered()`): a media folder covers `Download/Harmoniq` if its tree document
   ID (`DocumentsContract.getTreeDocumentId(uri)`, e.g. `primary:Download/Harmoniq`, `primary:Download`,
   `primary:`) is that path or a parent of it.
2. **Not covered → one-time prompt** after the first download, when the folder exists: *"Add the Harmoniq downloads
   folder to your library?"* → `ActivityResultContracts.OpenDocumentTree()` with
   `EXTRA_INITIAL_URI = DocumentsContract.buildDocumentUri("com.android.externalstorage.documents",
   "primary:Download/Harmoniq")`. Then persist the read permission (`ActivityUtils.makePersistableReadableUri`,
   as `MultiSystemFolderTile` does) and add the URI to `settings.mediaFolders`.
   - Android 11+ won't let users pick the **root** of `Download`, but subfolders like `Download/Harmoniq` are
     allowed.
   - If the user declines, remember it and show a small "Not in library: add folder" hint on downloaded items
     instead of asking again.
3. **Rescan** after each saved file: `symphony.groove.fetch(Groove.FetchOptions())`. The existing scan cache skips
   unchanged files, so this is cheap. A future improvement is a single-file scan
   (`MediaExposer.scanAudioFile` for one URI).
4. **Result:** the song shows up under Songs, Albums ("Random Access Memories", track 8), Artists (Daft Punk,
   Pharrell Williams, Nile Rodgers), Genres, with cover and synced lyrics. Nothing else to configure.

## 12. Progress and error handling

**Progress states shown to the user**

| Server `status` | UI text | Progress bar |
|-----------------|---------|--------------|
| `queued` | Waiting… | indeterminate |
| `resolving` | Finding the song… | 0–10 % |
| `downloading` | Downloading on server… | 10–75 % (server `progress`) |
| `processing` / `tagging` / `verifying` | Adding metadata & artwork… | 75–100 % |
| (local) fetching file | Saving to phone… | bytes received / `size_bytes` |
| done | Saved to Download/Harmoniq | ✓ |

**Errors** (codes from [api.md §Errors](api.md#errors))

| Code | User message | Worker action |
|------|--------------|---------------|
| network error / timeout | "Waiting for connection" | `Result.retry()` (WorkManager waits for network) |
| `upstream_blocked`, `upstream_error`, `upstream_timeout`, `processing_failed`, `queue_full`, `rate_limited`, `too_many_active_jobs` | "Server busy, will retry" | `Result.retry()` with backoff; give up after 5 attempts → "Failed, tap to retry" |
| `video_unavailable`, `age_restricted`, `live_not_supported`, `duration_limit_exceeded` | Specific reason ("This video is private", "Longer than 15 min"…) | `Result.failure()` |
| `unauthorized` | "This app version can't use Global Search anymore. Please update." | `Result.failure()`; hide Global mode until next app update |
| `job_not_found` / `job_expired` | (invisible) | Re-submit the job |
| SHA-256 / size mismatch | (invisible) | Delete partial file, retry once |
| storage write fails / disk full | "Not enough storage" | `Result.failure()` |

## 13. Permissions and storage

| Permission / item | Why | Notes |
|-------------------|-----|-------|
| `INTERNET` | Server API | Already declared |
| `POST_NOTIFICATIONS` | Progress/finished notifications | Already declared and requested on Android 13+ |
| `FOREGROUND_SERVICE_DATA_SYNC` + WorkManager's `SystemForegroundService` with `foregroundServiceType="dataSync"` | Long downloads as foreground work on Android 14 (target 34) | Merge into the manifest: `<service android:name="androidx.work.impl.foreground.SystemForegroundService" android:foregroundServiceType="dataSync" tools:node="merge"/>` |
| `WRITE_EXTERNAL_STORAGE` with `android:maxSdkVersion="28"` | Writing to `Download/` on Android 9 only | Runtime request on API 28 before the first download |
| No `READ_MEDIA_AUDIO` / `MANAGE_EXTERNAL_STORAGE` | The library reads through SAF grants; writing uses MediaStore | Keeps the app's storage model unchanged |
| Network security | HTTPS only for real servers | `debug` build only (`app/src/debug/`): a `network_security_config` with `<base-config cleartextTrafficPermitted="true" />`, so `127.0.0.1` (adb reverse), `10.0.2.2` (emulator) and any LAN IP work. `release`/`nightly`/`canary` keep Android's default (no cleartext since API 28) |
| New dependencies | `androidx.work:work-runtime-ktx`, `com.pierfrancescosoffritti.androidyoutubeplayer:core` | Add to `gradle/libs.versions.toml`; add R8 keep rules if the release build needs them (test the release build, as `docs/08-troubleshooting.md` warns) |

## 14. Testing strategy (Android)

| Level | What | How |
|-------|------|-----|
| Unit | DTO parsing against real server JSON (save responses from the live server as fixtures); error mapping; filename collision logic; LRC/date parsing fixes | JUnit + kotlinx-serialization; `ignoreUnknownKeys` |
| API client | Headers sent, problem+json parsing, `Retry-After`, Range resume | OkHttp `MockWebServer` |
| Worker | Success path, retry on 5xx/network, failure on 4xx, cancel sends DELETE, re-submit on 404 | `androidx.work:work-testing` (`TestListenableWorkerBuilder`) + MockWebServer |
| Storage | MediaStore save on API 29/30/34, legacy save on API 28, collision naming | Instrumented tests on emulators (API 28, 30, 34) |
| Library | After save + folder grant + rescan, `groove.song` contains the file with album/track/genre/lyrics | Instrumented test with a known server file (or a fixture file with the same tags) |
| UI | Global switch, debounce, preview sheet, download button states | Compose UI tests + manual |
| Release build | R8 doesn't strip serialization / player classes | Install `release`/`canary` builds before sharing (existing checklist) |

## 15. Server deployment process

1. **Local (development):** run the server on your PC (`scripts\run.ps1`). The `debug` build points at
   `http://127.0.0.1:8000` with `adb reverse tcp:8000 tcp:8000`, `http://10.0.2.2:8000` on an emulator, or
   `http://<pc-lan-ip>:8000` (cleartext allowed in debug only). Use the curl walkthrough to confirm the server first.
2. **Test server:** pick the host ([deployment.md §2](deployment.md#2-where-to-host-it): a home machine is best).
   Run `docker compose up -d`, plus the Cloudflare Tunnel profile for a stable HTTPS name
   ([deployment.md §8](deployment.md#8-how-the-app-reaches-the-server)). Set `CANARY_VIDEO_ID`, check `/readyz` and
   `/v1/meta`.
3. **Secrets:** generate the production token on the server (`APP_TOKENS`). Put the same value and the HTTPS URL
   into the `canary`/`release` build's local properties.
4. **Keep it healthy:** weekly yt-dlp update ([deployment.md §7](deployment.md#7-keeping-yt-dlp-current)); watch
   `status.youtube` in `/v1/meta`.

The full first-time walkthrough of steps 2–4 is [deployment-guide.md](deployment-guide.md).

## 16. Validating the complete flow before testers get it

**Stage gates**

| Stage | Who | Build | Server | Exit criteria |
|-------|-----|-------|--------|---------------|
| 1. Dev | you | `debug` | local PC | All rows of the test matrix below pass on one device |
| 2. Internal | you, 2+ devices (one Android 9 or emulator, one Android 14) | `canary` | test server via HTTPS tunnel | Matrix passes on both; 24 h canary green on the server |
| 3. Testers | the few testers | `canary` APK shared directly | same | No blocking issues for a week; collect feedback |
| 4. Release | everyone you share with | `release` | same (production token) | Pre-release checklist in the app's [docs/05-release-and-distribution.md](https://github.com/AnujYadav-Dev/Harmoniq/blob/main/docs/05-release-and-distribution.md) |

**Test matrix** (each one: search → preview → download → appears in library with correct tags)

| Case | Example | Check |
|------|---------|-------|
| Album track (ATV) | Daft Punk – Get Lucky | Album, track 8/13, 2013-05-17, genres, synced lyrics, 1200 px cover |
| Music video (OMV) | an official video ID | Clean title (no "(Official Video)"), album borrowed from the catalogue |
| Upload (UGC) | "Me at the zoo" | Title + artist from the video; no bogus album |
| Non-Latin title | a Hindi/Japanese song | Filename and tags intact |
| Multi-disc album | a 2-CD album track | Disc 2, per-disc track number |
| Each format | Opus / M4A / MP3 | Plays in Harmoniq; tags and cover present |
| Duplicate | download the same song twice | No second copy; "Downloaded" badge |
| Interrupted | airplane mode mid-download | Resumes, SHA-256 matches |
| Server down / wrong token / rate limited | stop server; wrong token build; 31 quick searches | Correct messages, no crash |
| Not in library yet | fresh install | One-time folder prompt; song appears after granting |
| Android 9 | emulator API 28 | Legacy storage path + permission prompt |

## 17. Implementation status and deviations

**Done** (every item of the original checklist): Gradle config and dependencies, `services/globalsearch` with unit
tests, the Library/Global switch, the preview sheet with the YouTube player (pauses `Radio`),
`ServerDownloadWorker` + `DownloadSaver` (API 28 / 29+) + `DownloadRecords` + notifications, the `LibraryLink`
prompt and rescans, both app fixes (`AudioMetadataDates` in `:metaphony` for year / year-month dates,
`TimedContent` fraction scaling), and the Global search settings screen.

**Deviations from the plan**

| Plan | Implemented | Why |
|------|-------------|-----|
| WorkManager (latest) | `androidx.work` **2.10.5** (`gradle/libs.versions.toml`) | 2.11+ pulls in Room 2.7.0; the app is on Room 2.6.1 |
| android-youtube-player 12.1.x | **13.1.0** | Current release |
| — | Lint check `NullSafeMutableLiveData` **disabled** (`lint {}` in `app/build.gradle.kts`) | The check shipped with lifecycle 2.9.x (pulled in by the player) crashes AGP 8.7.3's lint. The app has no LiveData. Remove after an AGP upgrade |
| Worker reaches the app through `Symphony` | The worker builds its API client from a plain `Context` (`GlobalSearchServices`, `DeviceId`, `GlobalSearchPreferences`) | WorkManager runs the worker without the UI-scoped `Symphony` ViewModel |
| Rescan after each save | `groove.fetch(Groove.FetchOptions())`, serialized by a mutex, **not** `refreshLibrary()` | `refreshLibrary()` calls `radio.stop()`, which would stop playback whenever a download finishes |
| One key per variant | Build-type keys (`HARMONIQ_SERVER_URL_CANARY` etc.) override the shared key | One `local.properties` serves debug (local PC) and canary/release (tunnel) |
| `unauthorized` → hide Global until next update | A flag keyed to `versionCode`: after a `401`, Global Search is hidden from the next launch until an APK with a **different `versionCode`** is installed | Token rotation therefore needs a `versionCode` bump ([deployment-guide.md §11](deployment-guide.md#11-rotating-the-app-token)) |

**Verified:** `:app:testDebugUnitTest` (82 tests: DTO parsing against server fixtures, API client with
MockWebServer, pipeline, worker under Robolectric, file names, library coverage, records, LRC timing) and
`:metaphony:testDebugUnitTest` (date parsing) pass.

**Not verified yet**

- Device tests on API 28 (legacy storage + permission prompt) and API 34, i.e. the §14 "Storage", "Library" and
  "UI" rows.
- Signed `canary` and `release` builds (R8 with kotlinx.serialization and the YouTube player; no new keep rules
  were added).
- The §16 matrix against the deployed HTTPS server.

**Follow-ups**

1. Commit the app changes (staged on `dev`) and the server docs.
2. After an AGP upgrade: move WorkManager to 2.11+ together with Room 2.7, and drop the lint exception.
3. Later: single-file scan instead of a full `fetch()`; read `HARMONIQ_SOURCE` for a reinstall-proof "Downloaded"
   badge (§8).
