# Local Music Library

Current release: **0.5.1**.

Local Music Library is a Windows dashboard for organizing long YouTube and
Bilibili libraries without downloading provider media. It saves links and
metadata in a local SQLite database, then gives you playlists, queues,
subscriptions, ratings, review tools, backups, and exports.

The repository is source available under the [PolyForm Noncommercial 1.0.0
license](LICENSE). Read the [changelog](CHANGELOG.md), [security policy](SECURITY.md),
and [support notes](SUPPORT.md) before making a release or sharing a build.

## Start the dashboard

From PowerShell:

```powershell
.\setup.ps1
.\MusicLibrary.ps1
```

The app opens in your default browser. By default it stores data in
`%LOCALAPPDATA%\LocalMusicLibrary`. Choose another directory when needed:

```powershell
.\MusicLibrary.ps1 -DataDir "D:\MusicLibrary"
```

The dashboard binds to `127.0.0.1:8765`. Keep the terminal running while you
use the dashboard or browser extension.

## Main workflows

### Import links

Open **Imports**, paste one or more YouTube or Bilibili video, playlist,
channel, series, or feed URLs, choose a local playlist, and start the metadata
job. Imports save provider links and metadata. They do not download or
re-encode media.

The import page shows current jobs, completed jobs, and recent paused or failed
jobs separately. Jobs survive a restart when they can be resumed safely.

### Organize the library

The library supports:

- exact provider-ID deduplication;
- deliberate duplicate playlist entries;
- ratings, tags, aliases, notes, and bulk metadata edits;
- soft deletion, Trash, undo, and typed confirmation for large deletions;
- Shift-click range selection, including range deselection;
- saved searches and pools;
- queue playback from selected tracks, playlists, pools, or **All Library**;
- shuffle modes, uploader spacing, playback history, and exports;
- backups with validation, restore, rollback, and retention settings.

All Library queues every saved track, including tracks that are not on the
currently loaded page. The selection and filter controls are separate from
that queue source.

### Subscriptions

Open **Subscriptions** to track a channel, playlist, series, or feed. Each
subscription has its own destination playlist and can use either mode:

- **Auto-saved new songs** adds newly discovered source tracks to the selected
  playlist.
- **Review before adding** imports metadata and shows new tracks in the
  subscription page without adding playlist membership.

The page includes unread counts, new-song lists, manual refresh, pause/resume,
mark-as-read, and the mode switch. Scheduled checks run when **Settings →
Updates and safety → Automation** is set to **Every 3 hours** or **Daily**.
New installations default to every three hours. Existing installations keep
their current setting until you change it.

### Creators and relations

Open **Creators / producers** to filter creators and sort by song count, views,
or name. From a creator card you can open the matching library view or start a
subscription.

Maintenance can suggest `same_recording`, `possible_duplicate`, `cover_of`,
`remix_of`, `alternate_mix_of`, and related-title links from metadata. Open
**Explore relations** on a track to inspect evidence, change traversal depth,
review suggestions, or create a relation-based smart playlist. Suggestions are
never accepted as destructive changes automatically.

Relation matching preserves Unicode and uses title, creator, alias, album,
duration, provider, and version evidence. It does not fetch provider audio.
Optional local Chromaprint fingerprints are available only for audio files you
explicitly select on disk. Translation packages are not part of the core
install. Add translated names as aliases when needed.

## Browser extension and queue playback

Build the current Chrome and Firefox/Zen packages with:

```powershell
powershell -ExecutionPolicy Bypass -File .\PackMusicLibraryExtension.ps1
```

The builder clears stale files from `dist`, creates the current archives, and
uses POSIX paths inside both packages. This matters for Firefox Add-ons
validation.

Install the Chrome package from `chrome://extensions` using **Developer mode →
Load unpacked** after extracting it. Load the Firefox/Zen XPI temporarily from
`about:debugging`. Permanent Firefox/Zen installation requires Mozilla signing.
See [extension/README.md](extension/README.md) for the full install and pairing
steps.

The extension reads the pairing token from the local dashboard when possible.
Use the exact loopback address `http://127.0.0.1:8765`, choose **Zen** when
running Zen, and use the profile label only as a local name. The extension
tries `localhost` as a fallback for the live WebSocket. If HTTP pairing works
but the player WebSocket does not, saved-status checks, browser saves, and
ratings can still work while queue controls remain unavailable.

The extension adds a small save control to unsaved provider result cards. Saved
cards show a saved marker beside the title, without a persistent green outline.
The marker is shown while the card is active or hovered. It reuses one normal
YouTube or Bilibili tab for queue playback and retries navigation while a page
loads. Browser autoplay, live videos, unavailable videos, ads, and provider
privacy tools can still require a manual Play or Next action.

## Legacy `bili2yt` relay

`bili2yt.py` is a separate terminal workflow. It discovers Bilibili or YouTube
lists, downloads one selected item at a time with yt-dlp, and sends the file to
an unlisted YouTube playlist through the visible YouTube Studio browser. The
dashboard does not use it.

Install its optional tools:

```powershell
.\setup.ps1
ffmpeg -version
npm install --global agent-browser
agent-browser install
```

The default relay uses an ImDisk RAM drive and may require an elevated Windows
token. Manual upload mode leaves MP4 files in a folder instead:

```powershell
python .\bili2yt.py `
  "https://www.youtube.com/playlist?list=..." `
  --playlist "Imports" --all `
  --manual-upload-dir "$env:USERPROFILE\Videos\Bili2YouTube"
```

Use only media and accounts you are authorized to handle. The relay does not
attempt CAPTCHA or anti-bot circumvention. Bilibili may return HTTP 401, 412,
or 429. Authorized browser cookies, slower requests, or direct URLs are the
supported fallbacks.

## Checks before a release

Run the local checks from the repository root:

```powershell
.\.venv\Scripts\python.exe -m unittest -v
node --check extension/background.js
node --check extension/content.js
node --check extension/options.js
python tools/release_check.py
```

The release checklist is in [extension/RELEASE_CHECKLIST.md](extension/RELEASE_CHECKLIST.md).
The store copy and privacy text are in
[extension/STORE_LISTING.md](extension/STORE_LISTING.md).

## Known limits

- The core app is local-only. It does not provide cloud sync or remote access.
- Audio similarity is limited to explicit local fingerprints. Cover and remix
  suggestions use explainable metadata evidence.
- The core installer has no translation service.
- Provider cookies stay in the browser. Do not paste cookie files or passwords
  into the dashboard or an issue report.
- Spotify support is not included.

For a bug report, include the app version, provider, job status, and a redacted
error message. Do not include pairing tokens, cookies, database files, exports
with private links, or account credentials.
