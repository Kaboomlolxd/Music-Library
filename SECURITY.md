# Security policy

## Supported versions

The latest tagged release is supported. Older releases may contain outdated
provider adapters or browser-extension permissions and should be upgraded.

## Local-only threat model

The dashboard binds to `127.0.0.1` and stores the library in the user's local
data directory. The pairing token authorizes the browser extension's local
WebSocket. Keep the machine and browser profile trusted; this project does not
provide a cloud account or remote authentication boundary.

## Reporting a problem

Please report security problems privately through the repository's security
contact or private advisory workflow. Include the release version, operating
system, reproduction steps, and whether the issue affects the desktop app,
extension, or legacy relay. Do not include cookies, pairing tokens, databases,
or exported private playlists in a report.
