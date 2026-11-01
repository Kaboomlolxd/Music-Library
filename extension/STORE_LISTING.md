# Store listing draft

## Name

Local Music Library Player

## Short description

Save YouTube and Bilibili links to a local music library and play its queue in one reused browser tab.

## Full description

Local Music Library Player connects YouTube and Bilibili pages to the Local
Music Library dashboard on your computer.

The extension can:

- Check whether a video is in your local library.
- Save a video link without downloading media.
- Rate saved videos.
- Add a small save control to unsaved videos in search and listing pages.
- Show a saved marker beside a saved video title.
- Reuse one normal provider tab for queue playback and next/previous controls.
- Reconnect to that player tab after a browser restart or when the tab closes.

The extension sends provider URLs, titles, uploader text, ratings, and queue
commands only to the loopback Music Library server configured on the same
computer. It does not upload media, create remote playlists, capture audio,
read provider passwords, or send data to a developer-operated server. The
desktop app must be running for saved checks, saves, ratings, and queue
controls.

## Permissions

- `tabs` allows the extension to reuse its designated player tab.
- `storage` stores the loopback server address, pairing token, browser label,
  player tab, and widget position.
- YouTube and Bilibili host access allows video-page details and saved-status
  controls.
- Loopback access allows connection to the local dashboard.

## Reviewer steps

1. Start the desktop app with `MusicLibrary.ps1` and leave it running.
2. Open the extension options. The extension should detect the loopback server
   and pairing token. Select **Zen** if testing in Zen and automatic detection
   chose Firefox.
3. Open a YouTube or Bilibili video page. Use **Save to library** to test an
   item that is not already saved, or update the rating of a saved item.
4. In the dashboard, build a queue and select **Play / open in browser**.
5. Confirm the extension reuses one provider tab as the queue advances.

Provider account credentials are not required by the extension. Reviewers do
not need to download media. For local development and troubleshooting, see
[README.md](README.md).

## Privacy statement

This extension has no remote backend. It sends provider URLs, page titles,
uploader text, ratings, and queue-control messages only to the user's
configured loopback Music Library server. The local app stores library data in
SQLite on the user's computer. The extension does not sell, share, or retain
data on a developer-controlled server.
