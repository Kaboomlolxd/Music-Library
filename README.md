# Local Music Library

Current release: **0.5.1**. The repository is licensed under PolyForm
Noncommercial 1.0.0; see `LICENSE`, `CHANGELOG.md`, and `SECURITY.md` before
redistributing it.

`MusicLibrary.ps1` starts the new default workflow: a private, loopback-only
dashboard for organizing **YouTube and Bilibili links and metadata**. It does
not download, upload, copy, or re-encode media.

```powershell
.\setup.ps1
.\MusicLibrary.ps1
```

The dashboard opens in your Windows default browser. It persists the library in
SQLite (by default `%LOCALAPPDATA%\LocalMusicLibrary\library.sqlite3`) and writes
readable `library.md` / `library.txt` snapshots beside it. Use
`.\MusicLibrary.ps1 -DataDir "D:\Backups\My music library"` to put both the
database and exports elsewhere.

It imports YouTube/Bilibili channel, playlist, and individual-video URLs into
local playlists; supports exact-ID deduplication, optional duplicate playlist
copies, pools, uploader-variety shuffle, decimal ratings, soft-delete/undo,
merge ordering, smart rating playlists, source-by-source refresh history, queue
history, queue clearing, one-click link copying, and possible cross-provider
duplicate review. Normal imports
avoid slow view-count lookups; set a minimum-view filter or request a refresh
when you need counts. For private/authorized sources, the import form accepts
the same cookie source as the legacy tool (`zen`, `chrome`, or
`chrome:Default`). Do not paste account passwords or cookie file contents.

The Maintenance view can scan metadata for proposed relations between tracks.
Matching preserves Unicode scripts, compares title, artist, album, and alias
evidence, and reports confidence and explanations. The track table's
**Explore relations** action opens a bounded relation neighborhood; accepted
relations can be saved as dynamic smart playlists. The scan never downloads
provider audio and never merges records automatically. Different-language
titles need a manual alias or translated title because the core release has no
translation service dependency.

The dashboard derives a **Creators / producers** list from all saved tracks.
Click a creator to filter the library to that producer's music; the normal
rating, selection, queue, and play controls continue to work there. This
includes music videos and cover-art/audio-style release uploads, so an MV is
not required for a release to be useful.

The **Subscriptions** section lets you save a YouTube channel/playlist or a
Bilibili feed/series and choose a local destination playlist. It performs an
initial metadata sync and periodically checks enabled subscriptions while the
local app is running. Each source can use append-only, mirror, or
mirror-and-preserve-local-removals behavior. Source provenance is stored per
playlist entry, so mirror mode only removes memberships owned by that source;
manually added and unrelated entries are never removed. This is a local read
subscription and never edits the remote YouTube/Bilibili playlist.

Imports and updates are durable jobs. A restart keeps their original job IDs:
work that had not started is safely re-queued, while an interrupted provider
request is shown as paused for explicit resumption. Playback remains independent
of these jobs.

The Library bulk editor supports previewed set/clear operations, provider
refresh toggles, find/replace, regular expressions, exact normalization,
tag/alias add/remove/replace, copying fields from another track, and one-step
undo for the complete operation.

Backups are validated ZIP snapshots created with SQLite's snapshot API.
Restore validates database integrity before an atomic replacement, keeps one
explicit pre-restore rollback copy, and reopens the live connection safely.
Retention settings keep calendar-spaced daily, weekly, monthly, and six-month
checkpoints.

Optional audio fingerprints are strictly opt-in. The app can store a
user-supplied fingerprint or run Chromaprint `fpcalc` against an explicitly
selected local file. Imports, updates, and Discover never fetch or download
audio for fingerprinting, and fingerprint matches are review suggestions rather
than automatic merges.

When the browser extension saves an individual video, YouTube and Bilibili
items are kept in separate local playlists: **YouTube browser saves** and
**Bilibili browser saves**. On provider home, search, and list pages, the
extension marks matching videos with a green **★ Saved** thumbnail pill and a
thin card outline. Its current-video widget contains the save and rating
controls.

For automatic next-track navigation, double-click
[`PackMusicLibraryExtension.bat`](PackMusicLibraryExtension.bat) to create the
Chrome and Firefox/Zen install archives, then follow the installation steps in
[`extension/README.md`](extension/README.md). Paste the dashboard's pairing
token in its Options page, then use **Open in Library Player**. The extension
creates one designated normal YouTube/Bilibili tab and reuses it for the queue.
Providers, browser autoplay rules, live/unknown duration media, unavailable
videos, and ad/privacy extensions can still require a manual Next click; the
manager does not attempt to bypass them.

Spotify is intentionally not included in this release. The provider boundary is
kept modular so a future compliant adapter can be added without changing local
playlist and queue rules.

---

# Legacy `bili2yt` relay

`bili2yt` is a local, terminal-driven relay for moving videos from Bilibili or
YouTube lists/channels/playlists into an **unlisted** YouTube playlist. It does
not use the YouTube Data API and therefore does not consume YouTube API quota.

The media path is designed to reduce SSD writes:

1. yt-dlp discovers the source list and reads view counts.
2. yt-dlp's native downloader pulls one selected video/audio pair into the
   ImDisk VM-backed RAM drive, handling fragments, retries, and expiring URLs.
3. If video and audio are separate streams, yt-dlp invokes ffmpeg only to
   merge/remux them into one MP4 with stream copy. No video or audio re-encode
   is configured. A progressive source can be downloaded without ffmpeg.
4. The signed-in YouTube Studio web uploader receives that one file through
   `agent-browser`, or the file is left in a persistent folder for manual upload.
5. The temporary file is deleted before the next video.

YouTube Studio requires a local file selection, so the program uses a RAM-backed
file path rather than pretending a browser can upload an in-memory Python
object. By default it creates an 8 GB temporary ImDisk VM disk at `R:` and
detaches it when the run ends. The tiny optional state ledger is separate and
stays in the project directory unless you move it with `--state-file` or
disable it.

## Setup on Windows

Install the two external tools first:

```powershell
# Project-local Python environment and yt-dlp
.\setup.ps1

# ffmpeg: needed when yt-dlp must merge separate video/audio streams; install
# it with winget/chocolatey/manual download, then verify:
ffmpeg -version

# Browser automation used for YouTube Studio (the browser is visible by default)
npm i -g agent-browser
agent-browser install
```

The automatic RAM drive requires an elevated Windows token on many systems
because ImDisk creates a virtual drive. `Bili2YouTube.bat` requests
Administrator permission automatically when needed. If `R:` is already used,
choose another free letter:

```powershell
python .\bili2yt.py <source-url> --playlist "Imports" --all --ram-drive S: --ram-size 8G
```

The first upload opens a browser session. Sign in to the Google account/channel
that should receive the videos, complete 2FA if requested, and press Enter in
the terminal. The saved `agent-browser` session is reused on later runs. Do
not put Google passwords in command-line arguments or source files.

If Google blocks the automated browser sign-in, use manual upload mode. It skips
`agent-browser`, saves each downloaded MP4 in the directory you choose, and
leaves the files there so you can drag them into YouTube Studio or select them
with its file picker:

```powershell
python .\bili2yt.py `
  "https://space.bilibili.com/28677456/lists/4942596?type=series" `
  --all `
  --manual-upload-dir "$env:USERPROFILE\Videos\Bili2YouTube"
```

The PowerShell launcher supports the same workflow:

```powershell
.\Bili2YouTube.ps1 -ManualUploadDir "$env:USERPROFILE\Videos\Bili2YouTube"
```

Manual mode uses a normal persistent folder instead of the temporary RAM disk.
The program does not mark a video complete until the automated upload path has
actually saved it, so the manual files remain available for you to upload.

## Examples

### Simplest way to run it

Double-click `Bili2YouTube.bat` in this folder. It asks for the source URL,
then the program asks for the YouTube playlist name and whether to import all
videos or apply a view-count cutoff. The launcher uses 1080p, Zen Browser
cookies, a two-second request delay, and the automatic RAM disk by default.

You can also start the same wizard from PowerShell:

```powershell
.\Bili2YouTube.ps1
```

The regular CLI remains available when you want exact control. The old
`run.ps1` helper now defaults to 1080p as well.

Preview a Bilibili series and ask for a view cutoff interactively:

```powershell
python .\bili2yt.py "https://space.bilibili.com/28677456/lists/4942596?type=series" --dry-run
```

When `--all` and `--min-views` are both omitted in an interactive terminal, the
program asks:

```text
Import all videos? [Y/n]:
Minimum views cutoff (examples: 100k, 1.5m):
```

Answer `Y` to keep everything, or `N` and enter a cutoff. For scripts and
non-interactive runs, keep using `--all` or `--min-views 100k`.

Relay everything into an existing or newly-created playlist called
`Bili imports`, using a RAM disk at `R:\bili2yt`:

```powershell
python .\bili2yt.py `
  "https://space.bilibili.com/28677456/lists/4942596?type=series" `
  --playlist "Bili imports" `
  --all
```

Keep only videos with at least 100,000 views and cap the run at 20 videos:

```powershell
python .\bili2yt.py `
  "https://space.bilibili.com/28677456/upload/video" `
  --playlist "Popular Bili imports" `
  --min-views 100k `
  --max-items 20
```

The same command accepts a YouTube channel, playlist, or video URL because
yt-dlp handles source extraction:

```powershell
python .\bili2yt.py "https://www.youtube.com/playlist?list=..." --playlist "YT imports" --min-views 1m
```

For a YouTube channel's Popular tab instead of its default/latest ordering:

```powershell
python .\bili2yt.py "https://www.youtube.com/@channel" --youtube-popular --playlist "Popular YT imports" --min-views 100k
```

For a source list file:

```powershell
python .\bili2yt.py --source-file .\sources.txt --playlist "Mixed imports" --all
```

If the source is private or requires a browser session, add a browser cookie
source understood by yt-dlp, for example `--cookies-from-browser chrome` or
`--cookies-from-browser edge:Default`.

Zen Browser is also supported as a convenience alias. The program finds the
Zen Firefox-compatible profile automatically:

```powershell
python .\bili2yt.py <source-url> --playlist "Imports" --all --cookies-from-browser zen
```

If Windows reports that it cannot decrypt Chrome/Edge cookies with DPAPI, close
the browser and retry. If that still fails, explicitly export an authorized
Netscape-format `cookies.txt` file and pass it without sharing the file:

```powershell
python .\bili2yt.py <source-url> --playlist "Imports" --all --cookies .\cookies.txt
```

Cookie files are equivalent to login credentials; keep them private and delete
them when you are finished.

Source requests use normal browser headers, one request at a time, a one-second
default delay, and bounded retries. View-count checks use a separate short
delay by default, so long lists do not wait a full source delay for every
video. You can increase either delay for a source that is returning temporary
blocks:

```powershell
python .\bili2yt.py <source-url> --playlist "Imports" --all --request-delay 3
# Use the same cautious delay for view-count checks too, if needed:
python .\bili2yt.py <source-url> --playlist "Imports" --min-views 100k --metadata-delay 3
```

YouTube playlist and channel discovery uses yt-dlp's flat/lazy enumeration:
the list can be processed as entries arrive instead of waiting for the whole
page to be materialized. Flat entries commonly include the title, URL, ID,
uploader/channel, and sometimes views. If a view cutoff is requested and an
entry does not include its count, the importer performs a metadata-only
single-video lookup for that entry; it never downloads media during filtering.
This is why a cutoff can still take longer than importing all videos.

Bilibili list pages are supported through the current yt-dlp Bilibili
extractor. When a flat Bilibili entry has only its BV ID, the importer uses the
lightweight public view endpoint to fill in its title, view count, and owner
without resolving formats. Bilibili series also try a paged public series
endpoint that can enrich up to 30 entries per request; it is used only for
matching IDs because the endpoint can lag behind the page. Bilibili may return incomplete list metadata or
HTTP 401/412/429 responses, especially without an authorized browser session.
The importer does not bypass those checks; cookies, slower delays, or direct
video URLs are the supported fallbacks.

## Important behavior and limitations

- The CLI chooses the best video rendition at or below `--max-height` when the
  extractor exposes one, and the highest-ranked separate audio stream. The
  default cap is now 1080p. If the source only exposes a larger video, the
  downloader keeps the best available source rather than re-encoding it down.
- All media downloads use yt-dlp's native downloader. When separate streams
  are selected, yt-dlp uses ffmpeg only for a stream-copy merge/remux into MP4;
  there is no `recodevideo`, H.264, or AAC encoding postprocessor. YouTube may
  transcode the upload on its own after it is submitted.
- View counts are read through public website metadata endpoints and yt-dlp's
  website extractors, not the YouTube Data API. If a list entry has no
  readable count and a cutoff is active, it is skipped unless
  `--include-unknown-views` is supplied.
- For Bilibili BV entries missing flat-list metadata, the importer uses the
  lightweight public view-metadata endpoint instead of resolving video formats.
  If that endpoint is unavailable, it falls back to the normal yt-dlp metadata
  extraction. Bilibili series additionally try paged batch metadata for
  matching entries.
- Native media downloads use yt-dlp's fragment-aware downloader so truncated
  DASH or HTTP responses are retried before any RAM-only merge/remux.
- Bilibili list/channel page support depends on the current yt-dlp extractor.
  A `--dry-run` is the quick way to see whether a particular page enumerates
  correctly. Some public `/upload/video` pages return HTTP 412 to automated
  clients; try `--cookies-from-browser chrome` or a text file of direct video
  URLs as a fallback.
- The program does not attempt CAPTCHA, fingerprint, or other anti-bot
  circumvention. If a site keeps returning 401/412/429, wait, use the browser
  session/cookies you are authorized to use, reduce request frequency, or use
  direct URLs.
- YouTube Studio is a changing web app. The adapter uses accessible text and
  CSS fallbacks, but if the UI changes it leaves the visible browser open and
  asks you to finish the title/playlist/unlisted steps manually.
- A browser-global failure, such as a dead agent-browser daemon, stops the
  batch instead of downloading more videos that cannot be uploaded.
- The browser adapter detects whether the installed `agent-browser` supports
  the optional `--restore` flag, so older and newer CLI releases can be used.
- `bili2yt-state.json` records only completed source IDs and video URLs. Delete
  it or use `--force` if you intentionally want to re-upload. Use `--no-state`
  to disable checkpoint writes.
- By default media is written only to the temporary ImDisk VM disk. `--temp-dir`
  is available for an existing ImDisk RAM disk and is checked before a real
  run. The explicit `--allow-disk-temp` override exists only for debugging.
- An automatically created RAM disk is detached when the run finishes,
  including after ordinary errors or Ctrl+C. A RAM disk that was already
  mounted before the run is left mounted; a machine crash or forced
  termination can also leave a stale mount. If the default `R:` mount is
  present but inaccessible, an interactive run asks before detaching and
  recreating it.
- Only relay material you are allowed to download and re-upload. Bilibili,
  YouTube, and your Google account can impose additional terms, copyright
  rules, regional restrictions, or anti-automation checks.

## Troubleshooting

Check local browser installation with:

```powershell
agent-browser --version
agent-browser --session bili2yt-probe open about:blank
agent-browser --session bili2yt-probe close
```

If even the harmless `about:blank` probe reports `Daemon failed to start`,
refresh the global Windows installation. This is especially useful when npm
reports a package version different from `agent-browser --version`:

```powershell
npm install --global --force agent-browser@latest
```

The importer captures browser-command output through a short-lived temporary
log because the agent-browser daemon persists after each CLI command. Media
files are still stored only on the RAM drive.

Run the offline regression tests with:

```powershell
.\.venv\Scripts\python.exe -m unittest -v
```

If Studio is stuck on a sign-in or verification screen, complete it in the
visible browser window. If the source needs cookies, close other browser
instances or use `--cookies-from-browser` with the correct profile.

Use `--keep-failed-file` only when debugging a failed upload; otherwise the
temporary MP4 is deleted even when an individual item fails.
