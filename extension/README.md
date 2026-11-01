# Local Music Library Player

The browser extension connects YouTube and Bilibili pages to the Local Music
Library dashboard running on the same computer. It can check saved status,
save a link, update a rating, and send queue playback commands. It does not
download media, capture audio, access provider passwords, or open a new tab for
each queue item.

On provider video pages, a small widget shows saved status and rating controls.
On home, search, channel, and list pages, unsaved videos get a small save dot.
Saved videos get a marker beside the title. These controls stay unobtrusive
until the card is active or hovered. Browser saves go into local playlists
named **YouTube browser saves** or **Bilibili browser saves**.

## Build the packages

Run `PackMusicLibraryExtension.bat` or:

```powershell
powershell -ExecutionPolicy Bypass -File .\PackMusicLibraryExtension.ps1
```

The builder creates these files under `dist` using the version in
`extension/manifest.json`:

- `local-music-library-player-<version>-chrome.zip`
- `local-music-library-player-<version>-firefox-zen.xpi`

It removes stale package files before building. The Chrome archive uses a
service worker. The Firefox/Zen XPI uses the Firefox background-script
fallback and POSIX paths such as `icons/icon-128.png`.

The extension's JavaScript, HTML, and CSS are included as readable source. The
packager does not bundle, minify, or generate extension code. It copies the
source files and changes the Firefox manifest to select the background-script
fallback and Firefox metadata. If Mozilla asks for source, provide the
repository source including `extension/`, `PackMusicLibraryExtension.ps1`, and
`tools/package_zip.py`. Rebuild both archives with the command above.

## Install and pair

Start `MusicLibrary.bat` and leave the terminal open. The local dashboard uses
`http://127.0.0.1:8765`.

The extension normally reads the pairing token from the dashboard
automatically. Open extension settings to check the server, token, and browser
connection mode. Select **Zen** if automatic detection reports Firefox. The
profile label is just a local name and does not choose the browser mode. Use
**Test connection** to check HTTP and the live-player WebSocket. **Forget this
browser** clears its pairing and player tab.

To rotate the token, open dashboard **Settings** and choose **Rotate pairing
token**. Pair each browser profile again after rotation.

### Chrome or Chromium

1. Extract the Chrome ZIP, or use the repository's `extension` folder while
   developing.
2. Open `chrome://extensions`.
3. Turn on **Developer mode**.
4. Select **Load unpacked** and choose the extracted `extension` folder.
5. Open the extension's options page to check the pairing.

Chrome does not permanently install a local ZIP directly. Keep the extracted
folder in place so the browser can load it.

### Zen or Firefox

1. Open `about:debugging#/runtime/this-firefox`.
2. Select **Load Temporary Add-on**.
3. Choose the current `dist\local-music-library-player-<version>-firefox-zen.xpi`.
4. Open the extension's **Manage** page, then **Preferences**.
5. Choose **Zen** if the browser mode is not already correct, then test the
   connection.

Temporary add-ons are removed when Firefox or Zen restarts. Permanent
installation requires a Mozilla-signed package. The normal **Install Add-on
From File** action rejects unsigned XPIs and may call them corrupt.

## Queue playback

Build a queue in the dashboard and choose **Play / open in browser**. The
extension creates one provider tab and reuses it for next and previous tracks.
The first playback may need a manual Play click because of browser autoplay
rules. Live videos, unknown durations, unavailable media, ads, or provider and
privacy extensions can also prevent automatic playback. The dashboard keeps
manual Next available.

## Fix a local connection problem

Use exactly `http://127.0.0.1:8765`, not HTTPS. The extension can fall back to
`localhost` for the player WebSocket. If the options page says HTTP pairing
worked but the WebSocket timed out:

1. Confirm `MusicLibrary.bat` is running and the dashboard opens at the loopback
   address.
2. Reload the extension after installing an updated package.
3. In Zen or Firefox, disable HTTPS-Only or HTTPS-upgrade for the loopback
   address. Disable any extension that rewrites local HTTP requests.
4. Set the browser mode to **Zen** or **Firefox** explicitly, then use **Test
   connection**.

HTTP pairing can still support saved-status checks, browser saves, and ratings
when the player WebSocket is unavailable. Queue playback needs the WebSocket.
The dashboard shows whether a browser extension has an active player session.

## Privacy and permissions

The extension contacts only the configured loopback Music Library server. It
sends provider URLs, page titles, uploader text, ratings, and queue commands to
that local app. It has no developer-operated backend and does not send data to
a cloud service.

- `tabs` lets the extension reuse and repair its designated player tab.
- `storage` remembers the local server, pairing token, browser label, player
  tab, and widget position.
- YouTube and Bilibili host access lets it read video page details and add the
  saved-status controls.
- Loopback access lets it communicate with the local dashboard.

For store metadata and reviewer steps, see [STORE_LISTING.md](STORE_LISTING.md).
For release checks, see [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md).
