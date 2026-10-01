"""Backfill missing track creators using the existing yt-dlp lookup path."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from music_library.database import LibraryDatabase, default_data_dir
from music_library.providers import adapter_for_name
from bili2yt import SourceExtractor


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--cookies-from-browser")
    parser.add_argument("--delay", type=float, default=0.25)
    args = parser.parse_args()

    data_dir = args.data_dir.expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "library.sqlite3"
    backup = data_dir / "library.sqlite3.before-creator-backfill"
    if db_path.exists() and not backup.exists():
        shutil.copy2(db_path, backup)
        print(f"Backup: {backup}", flush=True)

    db = LibraryDatabase(data_dir)
    rows = db.list_tracks(include_hidden=True, limit=100000)
    targets = [
        row for row in rows
        if not str(row.get("creator") or "").strip()
        or str(row.get("creator") or "").strip().casefold() == "unknown creator"
    ]
    print(f"Creator backfill: {len(targets)} tracks to inspect", flush=True)
    if not targets:
        db.close()
        return 0

    cookies = None
    if args.cookies_from_browser:
        import bili2yt

        cookies = bili2yt.parse_cookies_from_browser(args.cookies_from_browser)
    extractor = SourceExtractor(
        cookies=cookies,
        request_delay=max(0.0, args.delay),
        metadata_delay=max(0.0, args.delay),
        retries=2,
    )
    updated = failed = unchanged = 0
    try:
        for index, row in enumerate(targets, 1):
            url = str(row["url"])
            try:
                info = extractor._extract(url, flat=False, single=True, request_delay=args.delay)
                adapter = adapter_for_name(str(row["provider"]))
                creator = adapter.normalize_creator(info) if adapter else ""
                if creator.casefold() == "unknown creator":
                    unchanged += 1
                    print(f"[{index}/{len(targets)}] no creator: {row['title']}", flush=True)
                    continue
                safe = {
                    key: info[key]
                    for key in (
                        "uploader", "uploader_id", "channel", "channel_id",
                        "artist", "album_artist", "creator",
                    )
                    if info.get(key) is not None
                }
                db.update_creator(int(row["id"]), creator, source={"creator_backfill": safe})
                updated += 1
                print(f"[{index}/{len(targets)}] {creator} — {row['title']}", flush=True)
            except Exception as exc:
                failed += 1
                print(f"[{index}/{len(targets)}] failed: {row['title']} :: {exc}", file=sys.stderr, flush=True)
            if args.delay:
                time.sleep(args.delay)
    finally:
        extractor.close()
        db.close()
    print(f"Done: {updated} updated, {unchanged} still unknown, {failed} failed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
