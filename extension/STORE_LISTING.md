# Store listing draft

## Name

Local Music Library Player

## Short description

Save YouTube and Bilibili links to a local music library and reuse one browser tab for queue playback.

## Full description

Local Music Library Player connects YouTube and Bilibili pages to the local Music Library desktop app.

Use it to:

- See whether a video is already saved in your local library.
- Save a video to the local library without downloading media.
- Rate saved videos.
- Mark saved videos clearly on home, search, channel, and list pages.
- Reuse one normal provider tab for queue playback and next/previous navigation.
- Recover the player tab after a browser restart or a closed tab.

The extension talks only to the loopback Music Library server running on the same computer. It does not upload media, create remote playlists, capture audio, read provider passwords, or send data to a cloud service.

The desktop app must be running for saving, ratings, status checks, and queue controls.

## Permissions explanation

- `tabs`: reuse and repair the one designated player tab.
- `storage`: remember the local server, pairing token, browser label, player tab, and widget position.
- YouTube/Bilibili host access: show saved-state markers and the current-video library widget.
- Loopback access: communicate with the local Music Library app.

## Reviewer instructions

1. Start the desktop app with `MusicLibrary.ps1`.
2. Open the extension options and use the automatically detected loopback server.
3. Open a YouTube or Bilibili video page.
4. The extension shows the saved-state widget. Use "Save to library" to test a new item.
5. In the desktop dashboard, build a queue and choose "Play / open in browser".
6. The extension reuses one provider tab for queue navigation.

No provider account credentials are required by the extension. The reviewer does not need to download media.

## Privacy statement

This extension has no remote backend. It sends provider URLs, page titles, uploader text, ratings, and queue-control messages only to the user's configured loopback Music Library server. The local app stores the library in SQLite on the user's computer. The extension does not sell, share, or retain data on a developer-controlled server.
