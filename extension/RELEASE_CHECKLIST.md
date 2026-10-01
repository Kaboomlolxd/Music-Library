# Extension release checklist

## Local checks

- [ ] Bump `extension/manifest.json` to a new patch/minor version.
- [ ] Run `PackMusicLibraryExtension.ps1`.
- [ ] Run `python tools/release_check.py`.
- [ ] Run `node --check extension/background.js`.
- [ ] Run `node --check extension/content.js`.
- [ ] Run `node --check extension/options.js`.
- [ ] Run `python -m unittest`.
- [ ] Test pairing, Test connection, Forget this browser, Save to library, rating, and queue playback in Chrome.
- [ ] Test the staged XPI in Firefox/Zen temporary loading.
- [ ] Confirm no SQLite database, cookie file, token file, or `.env` file is in either package.

## Store submission

- [ ] Upload the Chrome ZIP to the Chrome Web Store developer dashboard.
- [ ] Upload the Firefox XPI to Mozilla Add-ons for signing.
- [ ] Use the copy in `STORE_LISTING.md` for the listing and privacy text.
- [ ] Add store screenshots and support URL.
- [ ] Include reviewer instructions from `STORE_LISTING.md`.
- [ ] Verify the published version matches the local manifest.
