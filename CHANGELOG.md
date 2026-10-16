# Changelog

## Unreleased

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
