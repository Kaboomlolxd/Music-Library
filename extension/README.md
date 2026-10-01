# Local Music Library Player extension

This WebExtension lets the local Music Library dashboard reuse exactly one
designated normal provider tab in each browser profile. It observes the
provider page's real HTML5 video element and reports verified completion to the
local app. It does not download media, capture audio, bypass ads/privacy
extensions, use provider credentials, or open a tab for every queue item.
When paired, it also shows a small saved-status widget on YouTube and Bilibili
pages. The widget reports whether the current video is already in the local
library, offers **Save to library** for new videos, and includes a rating box
directly in the card for saved videos. On YouTube/Bilibili home, search, and
list pages, saved videos receive a green **★ Saved** pill on the thumbnail and
a thin green card outline. Layouts without a usable thumbnail show the pill
beside the title instead. Hovering the pill shows the local playlist and rating
when those values are available.
Browser saves are placed in separate local playlists named **YouTube browser
saves** and **Bilibili browser saves**. The widget sits below the provider's
top navigation, can be dragged by its heading, remembers its position, and does
not intercept clicks outside its own controls.

## Make the install package

Double-click `PackMusicLibraryExtension.bat` in the project folder. It creates:

- `dist\local-music-library-player-<version>-chrome.zip`
- `dist\local-music-library-player-<version>-firefox-zen.xpi`

The package builder produces browser-specific manifests: the Chrome archive
uses Chrome's Manifest V3 service worker and the Firefox/Zen XPI uses Firefox's
current background-script fallback. Chrome-family browsers still require
loading the unpacked `extension` folder in Developer mode. The `.xpi` can be
selected for temporary loading in Zen/Firefox. A permanently installed
Firefox/Zen extension needs Mozilla signing; that cannot be bypassed by
packaging it differently.

## Pair it

1. Start the local manager and open its dashboard.
2. Build the package with `PackMusicLibraryExtension.bat`, then install it as
   described below.
3. The extension automatically reads the pairing token from the local app and
   detects Firefox/Zen or Chrome once per browser profile. That browser-family
   value is remembered locally. The Options page can still be used to change
   the server address or profile label manually, test the connection, or forget
   this browser's pairing.
4. Build a queue and click **Open in Library Player**. The extension creates
   one tab once, then updates that same tab for next/previous tracks.

Autoplay can require the initial Play click on YouTube/Bilibili. Live streams,
unknown durations, unavailable media, and provider/ad/privacy extension blocks
are reported to the dashboard and leave **Next** as a manual action.

## Chrome

1. Open `chrome://extensions`.
2. Turn on **Developer mode**.
3. Click **Load unpacked** and select this `extension` folder.
4. Click **Details** → **Extension options** to pair it.

Chrome does not allow local `.zip` files to be permanently installed directly;
that is a browser security rule. Keep the extracted project folder where it is
and use **Load unpacked** whenever you want to update the extension.

## Zen / Firefox

1. Open `about:debugging#/runtime/this-firefox`.
2. Click **Load Temporary Add-on**.
3. Select `dist\local-music-library-player-<version>-firefox-zen.xpi`, or
   select this folder's `manifest.json`.
4. Open the extension's **Manage** page → **Preferences** to pair it.

Firefox temporary add-ons disappear after restarting the browser. A signed
Firefox build can use the same Manifest V3 source later; this private local
release intentionally does not require publishing it.

Do **not** use Zen/Firefox's normal **Install Add-on From File** action for
this local XPI: normal installation requires an AMO/Mozilla signature and can
misleadingly show “appears to be corrupt” for an unsigned development package.

## Zen local connection troubleshooting

Start `MusicLibrary.bat` first, leave its terminal open, and use exactly
`http://127.0.0.1:8765` in the extension—**not** `https://` and not `wss://`.
Normally no token entry is needed: the extension reads `/api/health`
automatically and stores the local token in extension storage. The Options page
lets you override the remembered browser connection mode (including Zen) and
tries both `127.0.0.1` and `localhost` for the WebSocket after HTTP pairing
succeeds.
If the terminal prints `WARNING: Invalid HTTP request received`, Zen's
HTTPS-Only/HTTPS-upgrading feature or another extension is changing the local
connection into HTTPS. Turn off HTTPS-Only mode for `127.0.0.1` and disable any
HTTPS-upgrade extension for this local address, then save again. The extension
now checks the HTTP and WebSocket connection before it claims the profile has
been paired. If the optional live-player WebSocket still fails, the Options
page saves the HTTP pairing anyway so saved-status checks, browser saves, and
ratings can continue working; reload the updated extension after changing its
permissions.
