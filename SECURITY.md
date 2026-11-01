# Security policy

## Supported versions

Support follows the latest published release. Older releases may contain
outdated provider adapters, browser permissions, or migration code. Upgrade to
the latest release before reporting a problem.

## Local-only design

The dashboard binds to `127.0.0.1` and stores the library in the selected
local data directory. The browser extension communicates with that loopback
server through HTTP and an authenticated local WebSocket. The pairing token
authorizes the extension connection.

This project does not provide a cloud account, remote dashboard, or remote
authentication boundary. Treat the computer, operating-system account, browser
profile, pairing token, database, exports, and provider cookies as trusted local
data. Do not expose port `8765` outside the machine.

Use **Settings → Rotate pairing token** if you suspect a token was copied. Pair
each browser profile again after rotation. Use **Forget this browser** in the
extension options to clear that profile's local pairing and player-tab state.

The app intentionally does not accept provider passwords or cookie files
through the dashboard API. Keep provider cookies in the browser and do not put
them in source control, issue reports, package archives, or screenshots.

## Report a problem

For a security issue, use a private GitHub security advisory or contact the
repository owner through GitHub before opening a public issue. Include:

- the release version;
- Windows version and browser name;
- whether the issue affects the desktop app, extension, or `bili2yt` relay;
- a minimal reproduction and redacted logs.

Do not include pairing tokens, cookies, database files, private playlist URLs,
account credentials, or unredacted exports. If a report contains sensitive
data, delete it from the report and rotate affected credentials or tokens.

## Release hygiene

Before publishing, run `python tools/release_check.py` and inspect both archive
contents. Release packages must not include databases, cookies, token files,
`.env` files, or `__pycache__` directories. See
[extension/RELEASE_CHECKLIST.md](extension/RELEASE_CHECKLIST.md).
