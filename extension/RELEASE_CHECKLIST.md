# Extension release checklist

Use this checklist after changing the extension or desktop version. Run the
commands from the repository root.

## Version and package checks

- [ ] Set the same major, minor, and patch version in `pyproject.toml` and
  `extension/manifest.json`.
- [ ] Update `CHANGELOG.md` and `README.md` for the release.
- [ ] Run `PackMusicLibraryExtension.ps1`.
- [ ] Confirm `dist` contains only the current Chrome ZIP and Firefox/Zen XPI.
- [ ] Run `python tools/release_check.py`.
- [ ] Check that both archives contain `icons/icon-128.png`, with forward
  slashes in the archive path.
- [ ] Confirm neither archive contains a database, cookie file, pairing token,
  `.env` file, or `__pycache__` directory.
- [ ] Record SHA-256 checksums for the desktop and extension artifacts.
- [ ] If Mozilla requests source, provide the readable extension files and
  packaging scripts. Explain that packaging changes only the Firefox manifest
  fallback and metadata; it does not bundle or minify code.

## Code checks

- [ ] Run `node --check extension/background.js`.
- [ ] Run `node --check extension/content.js`.
- [ ] Run `node --check extension/options.js`.
- [ ] Run `python -m unittest -v`.
- [ ] Check the desktop source with `python -m compileall -q music_library`.

## Clean installation rehearsal

- [ ] Start with a fresh Windows data directory.
- [ ] Start `MusicLibrary.bat` and confirm the dashboard opens at
  `http://127.0.0.1:8765`.
- [ ] Confirm an existing database migrates and remains readable.
- [ ] Import a small YouTube playlist and Bilibili series.
- [ ] Stop and resume an import, then confirm the job appears under the right
  Job History section.
- [ ] Create, validate, restore, and roll back a backup.
- [ ] Pair Chrome and Firefox/Zen profiles.
- [ ] Test saved status, save to library, rating, and profile forgetting.
- [ ] Build queues from All Library, a playlist, a pool, and selected tracks.
- [ ] Test Play, Pause, Stop, Previous, Next, and manual Next fallback.
- [ ] Test YouTube and Bilibili pages, including autoplay and unmute recovery.
- [ ] Test unavailable videos, live videos, reconnects, and browser restarts.

## Subscription and creator checks

- [ ] Create an auto-add subscription and confirm new tracks enter its target
  playlist.
- [ ] Create a review-only subscription and confirm new tracks appear in the
  subscription page without playlist membership.
- [ ] Confirm unread badges, new-song lists, Mark read, Refresh now, and
  Pause/Resume.
- [ ] Confirm scheduled checks run when Settings uses Every 3 hours or Daily.
- [ ] Filter and sort the Creators / producers page.
- [ ] Open a creator's library tracks and subscription dialog.

## Store submission

- [ ] Upload the Chrome ZIP to the Chrome Web Store dashboard.
- [ ] Upload the Firefox/Zen XPI to Mozilla Add-ons for signing.
- [ ] Use `STORE_LISTING.md` for the listing, reviewer steps, and privacy text.
- [ ] Add current screenshots and the support URL.
- [ ] Verify the published version matches both local manifests.
- [ ] Publish the desktop ZIP, checksums, changelog, and setup instructions in
  the GitHub release.
