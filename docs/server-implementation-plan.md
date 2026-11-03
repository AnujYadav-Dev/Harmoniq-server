# Server Implementation Plan: high-quality audio with full metadata

[← Docs index](README.md) · Companion document: [Android Integration Plan](android-integration-plan.md)

> Status: **implemented** in v0.2.0. Every "implemented" item below is in the code; "Phase 2" items are deliberately
> deferred.

This plan upgrades the existing server so every downloaded song carries the same rich metadata as the old
Spotify notebook (`old-script.ipynb`). It's built for the current server architecture, not copied from the
notebook, and the tags it writes are the ones Harmoniq's library reader actually understands.

---

## 1. What the old script stored (analysis)

The notebook took track metadata from Spotify, genres from Last.fm, and lyrics from several sites. It wrote
ID3v2.3 tags into a 320 kbps MP3 and saved a separate `.lrc` file.

| Information | Old script source | Tag it wrote | Keep? |
|-------------|------------------|--------------|-------|
| Title | Spotify track name | `TIT2` | ✅ |
| Artist(s) | Spotify artists, joined with `, ` | `TPE1` | ✅ (as a list, see §3.3) |
| Album artist | first Spotify artist | `TPE2` | ✅ |
| Album | Spotify album | `TALB` | ✅ |
| Year | album release date (year only) | `TDRC` | ✅ (upgraded to a **full date** when known) |
| Track number | Spotify `track_number` (no total) | `TRCK` | ✅ (now `n/total`) |
| Disc number | Spotify `disc_number` (no total) | `TPOS` | ✅ (now `n/total`) |
| ISRC | Spotify `external_ids.isrc` | `TSRC` | ✅ |
| Genre | Spotify artist genres, else Last.fm top tags (max 3, joined `; `) | `TCON` | ✅ |
| Cover art | largest Spotify album image | `APIC` (front cover) | ✅ (square, ≤ 1200 px) |
| Lyrics | LRCLIB → lyrics.ovh → Genius scrape → syncedlyrics | `USLT` + separate `.lrc` file | ✅ (synced LRC **embedded**; `.lrc` optional via API) |
| Source URL | Spotify track URL | `WOAF` | ✅ (YouTube URL) |
| Comment | "Downloaded <date> \| ISRC:…" | `COMM` | ✅ (source URL) |
| Duration | Spotify `duration_ms` | used for matching only | ✅ (used for matching and validation) |

**Processing ideas worth keeping**

- Score the search candidates and reject non-originals: titles with karaoke, instrumental, cover, tribute, backing
  track, etc.
- Gate candidates by duration.
- Prefer real audio formats over silent alternate streams.
- Cache genre lookups per artist.

**What we deliberately don't copy**

| Old behaviour | Why not | Replacement |
|---------------|---------|-------------|
| Always transcode to **MP3 320k** | YouTube sources are ~128 kbps AAC / ~130–160 kbps Opus. Re-encoding to "320" adds size, not quality (lossy → lossy) | Remux the original stream (m4a/opus); MP3 only when asked, as VBR V0 |
| Spotify API (needs a Premium account since Feb 2026, OAuth) | Extra account and credentials, and not needed | YouTube Music catalogue (ytmusicapi) + MusicBrainz (free, no key) |
| Scraping Genius / lyrics.ovh | Fragile and against those sites' terms | YouTube Music lyrics (timed, licensed via LyricFind/Musixmatch) → LRCLIB (open API) |
| "Funny" placeholder lyrics when none are found | Pollutes the library with fake lyrics | No lyrics tag at all |
| Fake LRC timings (3 s per line) for plain lyrics | Shows wrong "synced" lyrics | Plain lyrics stay plain |
| ID3v2.3 | No real multi-value support, year-only date frame | ID3v2.4 (`TDRC` full date, proper UTF-8) |
| `Title - Artist.mp3` naming | Personal preference | `{artist} - {title}.{ext}` by default, configurable with `FILENAME_TEMPLATE` |

## 2. What Harmoniq actually reads (compatibility target)

Harmoniq parses files with **TagLib 2.x** (`:metaphony` module, `AudioMetadataParser.kt`), using TagLib's
*property map* keys. Anything outside this list is invisible to the app today, though other players may use it.

| TagLib key | Harmoniq field | How it's parsed | Server writes |
|-----------|---------------|-----------------|---------------|
| `TITLE` | title | first value | ✅ |
| `ARTIST` | artists | all values, then split on the **artist separators** (`;` `/` `,` `+` by default) | ✅ one value, artists joined with `"; "` |
| `ALBUMARTIST` | album artists | same splitting | ✅ main artist |
| `ALBUM` | album | first value | ✅ |
| `GENRE` | genres | split on the genre separators (same defaults) | ✅ up to 3, joined with `"; "` |
| `TRACKNUMBER` (+ `TRACKTOTAL`) | track number / total | `"n/total"` | ✅ |
| `DISCNUMBER` | disc number / total | `"n/total"` | ✅ when known |
| `DATE` | date + year | **only `YYYY-MM-DD` parses** (see ⚠️ below) | ✅ full date when known, else `YYYY` |
| `LYRICS` | lyrics cache | LRC timestamps → synced view | ✅ synced LRC text when available, else plain. **Format rules** from the app's `TimedContent` parser: no `[ti:]`/`[ar:]` header lines (they'd show up as lyric text); timestamps written as `[mm:ss.mmm]` because the parser reads the fraction as milliseconds |
| `COMPOSER` | composers | split | ⛔ not available from our sources (Phase 2) |
| embedded picture | cover art | **first** picture | ✅ one front cover (JPEG) |

> ⚠️ **App bug found:** `AudioMetadataParser.parseDate` tries `LocalDate.parse("2013", "yyyy")`, which always throws,
> so a **year-only** `DATE` becomes *no year at all*. The server writes a full date whenever MusicBrainz knows it,
> which it does for most albums. The Android fix (parse `Year` / `YearMonth` too) is listed in the
> [Android plan §8](android-integration-plan.md#8-metadata-handling).

Extra tags written for other players and for the future (Harmoniq ignores them): `ARTISTS` (one value per
artist), `ISRC`, `COMMENT`, `ENCODEDBY`, `ITUNESADVISORY` (explicit), `HARMONIQ_SOURCE` (`youtube:<id>`), and
MusicBrainz IDs.

## 3. Design

### 3.1 Target flow

```text
Search/Request ─▶ Find Music ─▶ Download/Process ─▶ Add Metadata & Artwork ─▶ Validate Quality ─▶ Store/Serve ─▶ App
 /v1/search       resolve        yt-dlp + FFmpeg     metadata resolver +       ffprobe + loudness   data/files     /file
 /v1/videos/…/    track          (remux first)       tag writer                + tag read-back      + sidecar      /lyrics
   track          (YT Music)                         (MusicBrainz, lyrics)
```

Metadata resolution only needs the video ID, so it **runs in parallel with the audio download** in a helper
thread. Enrichment adds no time unless it's slower than the download.

### 3.2 New module: `app/metadata/`

```text
app/metadata/
├── model.py         # TrackMetadata (+ provenance), MetadataContext, merge rules
├── resolver.py      # MetadataResolver: runs providers in order, merges, never fails the job
├── http.py          # shared httpx client, polite User-Agent, per-host rate limits, TTL caches
└── providers/
    ├── youtube.py      # from the yt-dlp info dict: title/channel heuristics (baseline)
    ├── ytmusic.py      # YouTube Music catalogue: artists, album, year, track n/total, square art, lyrics id
    ├── musicbrainz.py  # album-level match: full release date, genres, disc n/total, ISRC, MBIDs
    ├── lyrics.py       # YouTube Music timed lyrics → LRCLIB (synced, else plain)
    └── lastfm.py       # optional genre fallback (only when LASTFM_API_KEY is set), like the old script
```

Each provider implements one small interface:

```python
class MetadataProvider(Protocol):
    name: str
    def enrich(self, ctx: MetadataContext, current: TrackMetadata) -> TrackMetadata | None: ...
```

Providers get the video ID, the yt-dlp info (once known), the request overrides, and the metadata merged so far.
They return only the fields they know. Adding a source later (Discogs, Deezer, Cover Art Archive…) means adding one
file and listing it in `METADATA_PROVIDERS`.

### 3.3 Metadata model and merge rules

`TrackMetadata` fields: `title`, `artists[]`, `album_artist`, `album`, `track_number`, `track_total`,
`disc_number`, `disc_total`, `date` (`YYYY-MM-DD` / `YYYY-MM` / `YYYY`), `genres[]`, `isrc`, `explicit`, `lyrics`
(+ `lyrics_synced`, `lyrics_source`), `artwork_url`, `musicbrainz` IDs, and `sources` (which provider supplied
which field).

**Precedence**, highest first:

| Field | 1st | 2nd | 3rd | 4th |
|-------|-----|-----|-----|-----|
| title, artists, album | request overrides | YouTube Music catalogue | yt-dlp `track`/`artists`/`album` | title heuristics |
| album artist | override | YT Music album artists | first artist | channel name |
| track n/total | override | YT Music album tracklist (title + duration match) | MusicBrainz release | — |
| disc n/total | override | MusicBrainz release | — | — |
| date | override | MusicBrainz release-group first release (full date) | YT Music year | — |
| genres | override | MusicBrainz release-group genres (top 3, vote count ≥ 2) | Last.fm top tags (optional) | — |
| ISRC | MusicBrainz track | — | — | — |
| lyrics | YT Music (timed) | LRCLIB synced | LRCLIB plain / YT Music plain | — |
| artwork | override URL | YT Music album art (square, upscaled to 1200) | video thumbnail (centre-cropped) | — |

**Artist handling:** artists are stored as a list. They're written to `ARTIST` as one string joined with `"; "`,
which Harmoniq splits by default and which reads cleanly in other players. They're also written as one value per
artist in `ARTISTS`. The filename uses the main artist only.

### 3.4 Finding the right catalogue entry ("Find Music")

1. `get_watch_playlist(videoId)` gives the canonical artists, album name and ID, year, `videoType`, and the lyrics
   browse ID.
2. If an album is known, `get_album(albumId)` gives the album artists, track count and square art. The track number
   comes from matching **normalised title + duration (±3 s)**: the album tracklist often lists a *different* video
   ID for the same song (music video vs. audio).
3. For **music videos (OMV) and uploads (UGC)** with no album, search YouTube Music *songs* for "artist title" and
   accept a hit only if:
   - the normalised titles match;
   - an artist overlaps;
   - the durations are within ±15 s;
   - it isn't a karaoke/cover/instrumental/live version (the old script's rejection list, kept as a scoring rule).

   Then use that song's album data.
4. MusicBrainz is queried **at album level**: release-group search by album title + artist, accepting only an exact
   normalised title match on a plain *Album/EP/Single*. Then the earliest official release's tracklist gives the
   disc number and ISRC (title + duration match). Per-recording search was tested and rejected: remixes and
   reissues outrank the original.
5. MusicBrainz rules: at most **1 request per second**, and a User-Agent naming the app plus a contact that
   **you** configure (`MUSICBRAINZ_CONTACT`; never an email unless you set one). Results are cached per album for
   24 h, so the other songs from the same album cost nothing.

Every lookup has a short timeout. A failed or ambiguous match leaves the field empty; it never fails the job.

### 3.5 Audio quality

| Step | Behaviour |
|------|-----------|
| Format choice | Unchanged defaults: **m4a** (AAC remux), **opus** (Opus remux, the best YouTube source, ~130–160 kbps VBR), **mp3** (transcode, VBR V0). The selectors now **exclude DRC streams** and prefer itag 141 (256 kbps AAC) automatically when the account's cookies provide it |
| Validation (new `media/quality.py`) | One audio stream; expected codec; duration within ±2 s of the video; bitrate ≥ 48 kbps; sample rate ≥ 32 kHz; not silent (`volumedetect` max volume above −50 dB); file size sanity |
| Tag read-back | After writing, the tags are read back with mutagen and the required fields (title, artist, cover when one was fetched) are checked. A mismatch is `processing_failed` |
| Reported to the app | `quality: {codec, bitrate_kbps, sample_rate, channels, max_volume_db, transcoded, source_format_id}` |

Recommendation for the app: request **`opus` on Android 10+** (best quality) and `m4a` on Android 9.

### 3.6 Tag writer (`media/tags.py`, rewritten)

One function per container, all writing the same logical fields:

| Logical field | MP4 (m4a) | ID3v2.4 (mp3) | Vorbis comment (opus) |
|---------------|-----------|---------------|-----------------------|
| title | `©nam` | `TIT2` | `TITLE` |
| artist (joined) | `©ART` | `TPE1` | `ARTIST` |
| artists (list) | `----:com.apple.iTunes:ARTISTS` | `TXXX:ARTISTS` | `ARTISTS` (repeated) |
| album artist | `aART` | `TPE2` | `ALBUMARTIST` |
| album | `©alb` | `TALB` | `ALBUM` |
| track n/total | `trkn` | `TRCK` `n/t` | `TRACKNUMBER` + `TRACKTOTAL` |
| disc n/total | `disk` | `TPOS` `n/t` | `DISCNUMBER` + `DISCTOTAL` |
| date | `©day` | `TDRC` | `DATE` |
| genre (joined) | `©gen` | `TCON` | `GENRE` |
| lyrics | `©lyr` | `USLT` | `LYRICS` |
| ISRC | `----:com.apple.iTunes:ISRC` | `TSRC` | `ISRC` |
| explicit | `rtng` = 1 | `TXXX:ITUNESADVISORY` | `ITUNESADVISORY` |
| comment | `©cmt` | `COMM` | `COMMENT` |
| source URL | `----:com.harmoniq:source` | `WOAS` + `TXXX:HARMONIQ_SOURCE` | `HARMONIQ_SOURCE` |
| encoder | `©too` | `TENC` | `ENCODEDBY` |
| MusicBrainz IDs | `----:com.apple.iTunes:MusicBrainz …` | `TXXX:MusicBrainz …` | `MUSICBRAINZ_…` |
| cover | `covr` (JPEG) | `APIC` type 3 | `METADATA_BLOCK_PICTURE` |

The tests read every format back **with TagLib itself** (`pytaglib`, the same library as Harmoniq's `:metaphony`)
and assert the exact property-map values the app will see.

### 3.7 Storage

No new storage. The sidecar `data/files/{key}.json` now holds the full `TrackMetadata`, the quality report and the
lyrics text. `TAGGER_VERSION` goes to `3`, so files cached by older versions are rebuilt rather than served with
old tags. Lyrics also go into the sidecar so `/lyrics` can serve them without re-reading the audio file.

## 4. API changes (all backwards compatible)

| Change | Details |
|--------|---------|
| **New** `GET /v1/videos/{id}/track` | "Find Music" preview: the resolved `TrackMetadata` *before* downloading (artists, album, track n/total, date, genres, artwork URL, whether lyrics exist). Cached for 1 h. Lets the app show "Track 8 of 13 · Random Access Memories (2013)" in the preview sheet |
| **New** `GET /v1/downloads/{job_id}/lyrics` | `text/plain` LRC (synced) or plain text; `404 lyrics_not_available` otherwise. Optional for the app: lyrics are already embedded |
| `POST /v1/downloads` body | New optional fields: `embed_lyrics` (default `true`), `enrich` (default `true`, turns MusicBrainz/Last.fm on or off for this job). New override fields: `artists[]`, `date`, `disc_number`, `genres[]` |
| Job `metadata` (completed) | Adds `artists[]`, `track_total`, `disc_number`, `disc_total`, `date`, `genres[]`, `isrc`, `explicit`, `lyrics {available, synced, source, lines}`, `musicbrainz {…}`, `sources {field: provider}` |
| Job `file` | Adds `quality {…}` (see §3.5) |
| Job stages | Unchanged. If metadata isn't ready when the audio is, the job waits for it inside `tagging` (bounded by `METADATA_TIMEOUT_SECONDS`) |
| `GET /v1/meta` | Adds `features: {musicbrainz, lastfm, lyrics_providers}` |

## 5. Configuration (new env vars)

| Variable | Default | Purpose |
|----------|---------|---------|
| `METADATA_PROVIDERS` | `ytmusic,youtube,musicbrainz,lastfm,lyrics` | Order (= precedence) and on/off of providers |
| `MUSICBRAINZ_CONTACT` | *(empty)* | Contact for the MusicBrainz User-Agent (URL or email, your choice). Empty = the project name only |
| `LASTFM_API_KEY` | *(empty = off)* | Optional genre fallback, as in the old script |
| `LYRICS_PROVIDERS` | `ytmusic,lrclib` | Lyrics sources in order; empty = no lyrics |
| `METADATA_TIMEOUT_SECONDS` | `25` | Total budget for enrichment per job; whatever isn't ready by then is skipped |
| `FILENAME_TEMPLATE` | `{artist} - {title}` | Suggested filename (`{title} - {artist}` reproduces the old script) |

## 6. Testing

| Level | What |
|-------|------|
| Unit | Merge precedence; title/duration matching; karaoke/cover rejection; MusicBrainz album matcher on recorded JSON; LRC building from timed lines |
| Tag compatibility | For m4a/mp3/opus: write the tags, read them back with **TagLib (pytaglib)** and assert `TITLE`, `ARTIST`, `ALBUMARTIST`, `ALBUM`, `TRACKNUMBER`, `DISCNUMBER`, `DATE`, `GENRE`, `LYRICS`, `ISRC` exactly as Harmoniq parses them, plus that a picture is present |
| Pipeline | Full pipeline on generated audio with fake providers; quality validation rejects silent and truncated audio |
| API | `/track`, `/lyrics`, new body fields, error codes |
| Live (opt-in) | Real song end to end: album, track 8/13, full date, genres, synced lyrics, cover, all read through TagLib |

## 7. Phase 2 (not in this change)

- Trim non-music intros and outros of music videos using SponsorBlock's `music_offtopic` segments (the old script
  trimmed intro/outro/self-promo).
- Composer credits (from YouTube Music's credits browse ID or MusicBrainz work relations).
- Swap a music video for its album-audio counterpart (same song without video skits) when the user agrees.
- Cover Art Archive for covers above 1200 px.
- Batch / playlist downloads.
