# Local Music Library

Current release: **0.5.1**. This is a Windows desktop dashboard for organizing
long YouTube and Bilibili playlists using local metadata. It stores links,
ratings, playlists, queue state, aliases, and review decisions. It does not
download or re-encode media.

The repository is source-available under [PolyForm Noncommercial 1.0.0](LICENSE).
Read [CHANGELOG.md](CHANGELOG.md), [SECURITY.md](SECURITY.md), and
[SUPPORT.md](SUPPORT.md) before distributing a build.

## Start the dashboard

```powershell
.\setup.ps1
.\MusicLibrary.ps1
```

The app opens in your default browser and keeps its SQLite data under
`%LOCALAPPDATA%\LocalMusicLibrary` unless you choose another directory:

```powershell
.\MusicLibrary.ps1 -DataDir "D:\MusicLibrary"
```

Paste YouTube or Bilibili videos, playlists, channels, or feeds into Imports.
Choose a local playlist, then start the metadata job. The dashboard supports:

- exact provider-ID deduplication and deliberate duplicate playlist copies;
- ratings, tags, aliases, notes, soft deletion, undo, and bulk editing;
- saved searches, pools, queue history, shuffle modes, and exports;
- subscriptions with append-only or mirror membership rules;
- backups with validation, restore, rollback, and retention settings;
- relation suggestions for recordings, covers, remixes, and alternate mixes;
- optional Chromaprint checks for files you explicitly select on disk.

Relation scans use Unicode-aware metadata. They never fetch provider audio,
merge tracks, or rewrite playlists without review. Translation packages are not
part of the core install; add translated titles as aliases when needed.

## Browser extension and queue playback

Build the current packages with:

```powershell
powershell -ExecutionPolicy Bypass -File .\PackMusicLibraryExtension.ps1
```

Install the Chrome ZIP from `chrome://extensions`, or load the Firefox/Zen XPI
temporarily from `about:debugging`. The extension README has the pairing steps.
Use `http://127.0.0.1:8765`, paste the dashboard token, and choose the Zen
browser mode when pairing Zen.

The extension reuses one normal provider tab for the queue. It adds a small
save control to unsaved result cards and reveals a saved marker beside a title
only while you hover the card. Playback commands retry while a provider page
loads. Browser autoplay, live videos, unavailable videos, ads, and provider
privacy tools can still require a manual Play or Next action.

## Legacy `bili2yt` relay

`bili2yt.py` is a separate terminal workflow. It discovers Bilibili or YouTube
lists, downloads one selected item at a time with yt-dlp, and sends the file to
an unlisted YouTube playlist through the visible YouTube Studio browser. It is
not used by the dashboard.

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
or 429; an authorized browser cookie, slower requests, or direct URLs are the
supported fallbacks.

## Tests and release checks

```powershell
.\.venv\Scripts\python.exe -m unittest -v
node --check extension/background.js
node --check extension/content.js
node --check extension/options.js
.\.venv\Scripts\python.exe tools/release_check.py
```

The release builder creates a clean Windows ZIP and SHA-256 file. Extension
archives are rebuilt from the current manifest and use POSIX paths so Firefox
AMO accepts entries such as `icons/icon-128.png`.

## Known limits

- The core app is local-only and does not include a translation service.
- Audio similarity is limited to explicit local fingerprints. Cover and remix
  suggestions rely on explainable metadata evidence.
- Provider cookies remain in the browser. Never paste cookie files or passwords
  into the dashboard or issue tracker.
- Spotify support is not included.

For a bug report, include the app version, provider, a redacted error message,
and the smallest reproduction you can share. Do not include pairing tokens,
cookies, database files, or account credentials.
