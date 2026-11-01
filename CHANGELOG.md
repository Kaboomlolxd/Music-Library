# Changelog

## Unreleased

- Fixed Job History so active jobs, completed jobs, and paused or failed issues appear in separate sections. Job events refresh the view while work runs.
- Added a Subscriptions page with auto-add and review-only groups, unread counts, new-song lists, manual refresh, and per-source pause, read, and mode controls.
- Subscription new-song detection is source-specific, so a track already in the library can still be new to one subscription. Review-only imports update track metadata without adding playlist membership.
- Added a Creators / producers page with name filtering, song-count and view sorting, library shortcuts, and subscription shortcuts.
- Added subscription notification fields through an additive database migration. New databases default scheduled checks to every three hours; existing schedule preferences remain unchanged.
- Documented the All Library queue source, large-selection deletion check, range selection, pairing behavior, extension install steps, and subscription controls.
- Stopped full dashboard reloads for connection, queue, and export notifications. Batched library-change notifications and slowed reconnect attempts while the server is offline.
- Added Shift-click range selection and deselection in the library, with a partial-selection indicator.
- Added an explicit All Library queue source that includes unloaded tracks and can be saved as a dynamic pool.
- Enabled recoverable deletion from All Library and added a second, typed-count confirmation for selections of more than 50 songs.
- Fixed dashboard startup after the sidebar menu redesign, which prevented saved tracks and existing playlists from loading.
- Made playlist shortcuts open the library view, added a filter reset, and kept load errors visible outside Imports.

## 0.5.1 - 2026-10-02

- Reduced provider-page decoration to one hover-revealed saved marker beside each video title.
- Added a small unsaved save button to YouTube and Bilibili result cards.
- Made queue playback controls retry while provider tabs and content scripts finish loading.
- Added provider playback retries and best-effort Bilibili unmute handling.
- Separated active jobs from recent paused or failed job issues in the dashboard.
- Rebuilt Chrome and Firefox/Zen packages with POSIX archive paths for store validation.
- Simplified the README and grouped secondary dashboard controls into menus.
- Reworked the dashboard styling with square panels, angular buttons, and sharper borders.

## 0.5.0 - 2026-10-01

- Added Unicode-preserving, candidate-blocked metadata relation suggestions.
- Added relation evidence, bounded relation neighborhoods, and relation smart playlists.
- Added track-level relation exploration from the dashboard.
- Aligned the desktop and browser extension versions.
- Hardened release packaging so `dist/` contains only the current archives.
- Added public release, security, and license documentation.

## 0.4.1

- Added the current Chrome and Firefox/Zen extension packaging workflow.
- Added durable imports, subscriptions, backups, bulk editing, and local fingerprint review.
