"""SQLite persistence for the local music library.

The database deliberately uses only the Python standard library. It stays
portable, is simple to back up, and keeps browser storage out of the picture.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import sqlite3
import subprocess
import threading
import tempfile
import zipfile
import hashlib
import csv
import re
import unicodedata
from difflib import SequenceMatcher
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from .queueing import unique_tracks

SCHEMA_VERSION = 3


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def dt_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def default_data_dir() -> Path:
    root = os.environ.get("MUSIC_LIBRARY_DATA_DIR")
    if root:
        return Path(root).expanduser()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "LocalMusicLibrary"
    return Path.home() / ".local-music-library"


class LibraryDatabase:
    """Thread-safe SQLite repository and library-domain operations."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = (data_dir or default_data_dir()).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "library.sqlite3"
        self._lock = threading.RLock()
        self._conn = self._open_connection()
        # Keep a recoverable safety copy before the first additive migration.
        if self.path.exists() and self.path.stat().st_size > 0:
            try:
                current_version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
                has_tracks = self._conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tracks'"
                ).fetchone()
                safety = self.data_dir / (
                    f"library.sqlite3.before-migration-v{current_version}-to-v{SCHEMA_VERSION}"
                )
                if has_tracks and current_version < SCHEMA_VERSION and not safety.exists():
                    self._conn.commit()
                    snapshot = sqlite3.connect(safety)
                    try:
                        self._conn.backup(snapshot)
                    finally:
                        snapshot.close()
            except sqlite3.DatabaseError:
                pass
        self._init_schema()

    def _open_connection(self, path: Path | None = None) -> sqlite3.Connection:
        connection = sqlite3.connect(path or self.path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _init_schema(self) -> None:
        with self.transaction() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tracks (
                    id INTEGER PRIMARY KEY,
                    provider TEXT NOT NULL,
                    remote_id TEXT NOT NULL,
                    url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    creator TEXT,
                    view_count INTEGER,
                    duration REAL,
                    source_json TEXT NOT NULL DEFAULT '{}',
                    fetched_at TEXT NOT NULL,
                    hidden_at TEXT,
                    UNIQUE(provider, remote_id)
                );
                CREATE INDEX IF NOT EXISTS idx_tracks_lookup ON tracks(provider, remote_id);
                CREATE INDEX IF NOT EXISTS idx_tracks_creator ON tracks(creator);
                CREATE INDEX IF NOT EXISTS idx_tracks_hidden ON tracks(hidden_at);

                CREATE TABLE IF NOT EXISTS playlists (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    kind TEXT NOT NULL DEFAULT 'manual',
                    query_json TEXT NOT NULL DEFAULT '{}',
                    archived_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS playlist_tracks (
                    id INTEGER PRIMARY KEY,
                    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
                    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
                    position INTEGER NOT NULL,
                    duplicate_copy INTEGER NOT NULL DEFAULT 0,
                    deleted_at TEXT,
                    deleted_position INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_playlist_tracks_active
                    ON playlist_tracks(playlist_id, deleted_at, position);
                CREATE INDEX IF NOT EXISTS idx_playlist_tracks_track
                    ON playlist_tracks(track_id, deleted_at);

                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY,
                    url TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    target_playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
                    options_json TEXT NOT NULL DEFAULT '{}',
                    last_imported_at TEXT,
                    imported_count INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(url, target_playlist_id)
                );
                CREATE TABLE IF NOT EXISTS source_runs (
                    id INTEGER PRIMARY KEY,
                    source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
                    source_url TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    target_playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
                    options_json TEXT NOT NULL DEFAULT '{}',
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    discovered_count INTEGER NOT NULL DEFAULT 0,
                    added_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_source_runs_source
                    ON source_runs(source_id, started_at DESC);
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    target_playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
                    options_json TEXT NOT NULL DEFAULT '{}',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_sync_at TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(url, target_playlist_id)
                );
                CREATE INDEX IF NOT EXISTS idx_subscriptions_enabled
                    ON subscriptions(enabled, last_sync_at);
                CREATE TABLE IF NOT EXISTS ratings (
                    track_id INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
                    value REAL NOT NULL CHECK(value >= 0 AND value <= 10),
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pools (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    selection_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS queue_items (
                    position INTEGER PRIMARY KEY,
                    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS playback_state (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    current_position INTEGER NOT NULL DEFAULT -1,
                    mode TEXT NOT NULL DEFAULT 'true',
                    include_repeats INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS playback_history (
                    id INTEGER PRIMARY KEY,
                    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
                    played_at TEXT NOT NULL,
                    reason TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS extension_sessions (
                    profile TEXT PRIMARY KEY,
                    tab_id INTEGER,
                    url TEXT,
                    status TEXT NOT NULL DEFAULT 'disconnected',
                    updated_at TEXT NOT NULL
                );
                """
            )
            conn.execute(
                """
                INSERT INTO playback_state(id, current_position, mode, include_repeats, updated_at)
                VALUES(1, -1, 'true', 0, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (_utc_now(),),
            )
            conn.execute(
                "INSERT INTO settings(key, value) VALUES('update_schedule', 'manual') ON CONFLICT(key) DO NOTHING"
            )
            for key, value in (
                ("backup_retention_daily", "7"),
                ("backup_retention_weekly", "8"),
                ("backup_retention_monthly", "12"),
                ("backup_retention_long_term", "2"),
            ):
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO NOTHING",
                    (key, value),
                )
            # Forward-compatible migrations.  Existing installations keep
            # their data; each migration is additive and records its version
            # in SQLite's built-in user_version field.
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(tracks)").fetchall()}
            if "uploader" not in columns:
                conn.execute("ALTER TABLE tracks ADD COLUMN uploader TEXT")
                conn.execute("UPDATE tracks SET uploader = creator WHERE uploader IS NULL")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tracks_uploader ON tracks(uploader)")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS track_metadata (
                    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
                    field TEXT NOT NULL,
                    provider_value TEXT,
                    user_value TEXT,
                    override_active INTEGER NOT NULL DEFAULT 0,
                    source TEXT,
                    confidence REAL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(track_id, field)
                );
                CREATE INDEX IF NOT EXISTS idx_track_metadata_field ON track_metadata(field, override_active);
                CREATE TABLE IF NOT EXISTS metadata_audit (
                    id INTEGER PRIMARY KEY,
                    operation_id TEXT NOT NULL,
                    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
                    field TEXT NOT NULL,
                    before_json TEXT NOT NULL,
                    after_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_metadata_audit_operation ON metadata_audit(operation_id);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    job_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    label TEXT,
                    source_urls_json TEXT NOT NULL DEFAULT '[]',
                    options_json TEXT NOT NULL DEFAULT '{}',
                    current_source TEXT,
                    current_item TEXT,
                    completed_count INTEGER NOT NULL DEFAULT 0,
                    total_count INTEGER NOT NULL DEFAULT 0,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    next_retry_at TEXT,
                    error_class TEXT,
                    error TEXT,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, updated_at DESC);
                CREATE TABLE IF NOT EXISTS job_items (
                    id INTEGER PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    source_url TEXT,
                    item_key TEXT,
                    title TEXT,
                    status TEXT NOT NULL DEFAULT 'queued',
                    error_class TEXT,
                    error TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_job_items_job ON job_items(job_id, status);
                CREATE TABLE IF NOT EXISTS job_logs (
                    id INTEGER PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    level TEXT NOT NULL DEFAULT 'info',
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS discover_sessions (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    sources_json TEXT NOT NULL,
                    candidates_json TEXT NOT NULL DEFAULT '[]',
                    seen_json TEXT NOT NULL DEFAULT '[]',
                    page_size INTEGER NOT NULL DEFAULT 100,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT
                );
                CREATE TABLE IF NOT EXISTS backups (
                    id TEXT PRIMARY KEY,
                    path TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'manual',
                    manifest_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    checksum TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS playlist_snapshots (
                    id INTEGER PRIMARY KEY,
                    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
                    name TEXT,
                    tracks_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_playlist_snapshots_playlist
                    ON playlist_snapshots(playlist_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS track_relations (
                    id INTEGER PRIMARY KEY,
                    from_track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
                    to_track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
                    relation_type TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'proposed',
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    source TEXT NOT NULL DEFAULT 'local',
                    created_at TEXT NOT NULL,
                    reviewed_at TEXT,
                    UNIQUE(from_track_id, to_track_id, relation_type)
                );
                CREATE INDEX IF NOT EXISTS idx_track_relations_status
                    ON track_relations(status, confidence DESC);
                CREATE INDEX IF NOT EXISTS idx_track_relations_from
                    ON track_relations(from_track_id);
                CREATE INDEX IF NOT EXISTS idx_track_relations_to
                    ON track_relations(to_track_id);
                CREATE TABLE IF NOT EXISTS review_items (
                    id INTEGER PRIMARY KEY,
                    kind TEXT NOT NULL,
                    track_id INTEGER REFERENCES tracks(id) ON DELETE CASCADE,
                    relation_id INTEGER REFERENCES track_relations(id) ON DELETE CASCADE,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_review_items_status
                    ON review_items(status, created_at DESC);
                CREATE TABLE IF NOT EXISTS artists (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS releases (
                    id INTEGER PRIMARY KEY,
                    title TEXT NOT NULL,
                    normalized_title TEXT NOT NULL,
                    release_date TEXT,
                    album_artist TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(normalized_title, release_date)
                );
                CREATE TABLE IF NOT EXISTS track_artists (
                    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
                    artist_id INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
                    role TEXT NOT NULL DEFAULT 'primary',
                    PRIMARY KEY(track_id, artist_id, role)
                );
                CREATE TABLE IF NOT EXISTS track_releases (
                    track_id INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
                    release_id INTEGER NOT NULL REFERENCES releases(id) ON DELETE CASCADE,
                    disc_number INTEGER,
                    track_number INTEGER
                );
                CREATE TABLE IF NOT EXISTS saved_searches (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    query_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS source_memberships (
                    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
                    playlist_track_id INTEGER REFERENCES playlist_tracks(id) ON DELETE SET NULL,
                    owns_membership INTEGER NOT NULL DEFAULT 0,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    missing_since TEXT,
                    PRIMARY KEY(source_id, track_id)
                );
                CREATE INDEX IF NOT EXISTS idx_source_memberships_playlist_track
                    ON source_memberships(playlist_track_id, missing_since);
                CREATE INDEX IF NOT EXISTS idx_source_memberships_track
                    ON source_memberships(track_id, missing_since);
                CREATE TABLE IF NOT EXISTS track_fingerprints (
                    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
                    algorithm TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    duration REAL,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(track_id, algorithm)
                );
                CREATE INDEX IF NOT EXISTS idx_track_fingerprints_lookup
                    ON track_fingerprints(algorithm, fingerprint);
                """
            )
            if version < SCHEMA_VERSION:
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def _rows(self, statement: str, values: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            result = []
            for row in self._conn.execute(statement, tuple(values)).fetchall():
                item = dict(row)
                if "creator" in item and "uploader" not in item:
                    item["uploader"] = item.get("creator")
                result.append(item)
            return result

    def stats(self) -> dict[str, int | str]:
        with self._lock:
            return {
                "tracks": int(self._conn.execute("SELECT COUNT(*) FROM tracks WHERE hidden_at IS NULL").fetchone()[0]),
                "playlists": int(self._conn.execute("SELECT COUNT(*) FROM playlists WHERE archived_at IS NULL").fetchone()[0]),
                "data_dir": str(self.data_dir),
            }

    def extension_token(self) -> str:
        with self.transaction() as conn:
            existing = conn.execute("SELECT value FROM settings WHERE key = 'extension_token'").fetchone()
            if existing:
                return str(existing["value"])
            import secrets

            token = secrets.token_urlsafe(24)
            conn.execute("INSERT INTO settings(key, value) VALUES('extension_token', ?)", (token,))
            return token

    def rotate_extension_token(self) -> str:
        import secrets
        token = secrets.token_urlsafe(24)
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES('extension_token', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (token,),
            )
            conn.execute("UPDATE extension_sessions SET status='disconnected', tab_id=NULL, url=NULL")
        return token

    def create_playlist(self, name: str, *, kind: str = "manual", query: Mapping[str, Any] | None = None) -> dict[str, Any]:
        cleaned = " ".join(name.split()).strip()
        if not cleaned:
            raise ValueError("Playlist name cannot be empty")
        with self.transaction() as conn:
            try:
                cursor = conn.execute(
                    "INSERT INTO playlists(name, kind, query_json, created_at) VALUES(?, ?, ?, ?)",
                    (cleaned, kind, json.dumps(query or {}), _utc_now()),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"A playlist named {cleaned!r} already exists") from exc
            row = conn.execute("SELECT * FROM playlists WHERE id = ?", (cursor.lastrowid,)).fetchone()
            return dict(row)

    def get_playlist(self, playlist_id: int) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
        if row is None:
            raise ValueError("Playlist was not found")
        return dict(row)

    def update_playlist(
        self,
        playlist_id: int,
        *,
        name: str | None = None,
        archived: bool | None = None,
        query: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = self.get_playlist(playlist_id)
        values: list[Any] = []
        assignments: list[str] = []
        if name is not None:
            cleaned = " ".join(name.split()).strip()
            if not cleaned:
                raise ValueError("Playlist name cannot be empty")
            assignments.append("name = ?")
            values.append(cleaned)
        if archived is not None:
            assignments.append("archived_at = ?")
            values.append(_utc_now() if archived else None)
        if query is not None:
            assignments.append("query_json = ?")
            values.append(json.dumps(dict(query), ensure_ascii=False))
        if not assignments:
            return current
        values.append(playlist_id)
        with self.transaction() as conn:
            try:
                conn.execute(
                    f"UPDATE playlists SET {', '.join(assignments)} WHERE id = ?",
                    values,
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("A playlist with that name already exists") from exc
        return self.get_playlist(playlist_id)

    def list_playlists(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        archived_clause = "" if include_archived else "WHERE p.archived_at IS NULL"
        rows = self._rows(
            f"""
            SELECT p.*, COUNT(pt.id) AS active_count
            FROM playlists p
            LEFT JOIN playlist_tracks pt ON pt.playlist_id = p.id AND pt.deleted_at IS NULL
            {archived_clause}
            GROUP BY p.id
            ORDER BY p.name COLLATE NOCASE
            """
        )
        for row in rows:
            row["query"] = json.loads(row.pop("query_json"))
            if row["kind"] == "smart":
                row["active_count"] = len(self.smart_playlist_tracks(row["query"]))
        return rows

    def archive_playlist(self, playlist_id: int, *, archived: bool) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE playlists SET archived_at = ? WHERE id = ?",
                (_utc_now() if archived else None, playlist_id),
            )

    def upsert_track(
        self,
        *,
        provider: str,
        remote_id: str,
        url: str,
        title: str,
        creator: str | None = None,
        uploader: str | None = None,
        view_count: int | None = None,
        duration: float | None = None,
        source: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not remote_id:
            raise ValueError("Imported media needs a stable provider ID")
        with self.transaction() as conn:
            canonical_uploader = " ".join(str(uploader if uploader is not None else creator or "").split()).strip() or None
            conn.execute(
                """
                INSERT INTO tracks(provider, remote_id, url, title, creator, uploader, view_count, duration, source_json, fetched_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, remote_id) DO UPDATE SET
                    url = excluded.url,
                    title = excluded.title,
                    creator = COALESCE(excluded.creator, tracks.creator),
                    uploader = COALESCE(excluded.uploader, tracks.uploader, tracks.creator),
                    view_count = COALESCE(excluded.view_count, tracks.view_count),
                    duration = COALESCE(excluded.duration, tracks.duration),
                    source_json = excluded.source_json,
                    fetched_at = excluded.fetched_at
                """,
                (
                    provider,
                    remote_id,
                    url,
                    title,
                    canonical_uploader,
                    canonical_uploader,
                    view_count,
                    duration,
                    json.dumps(source or {}, ensure_ascii=False),
                    _utc_now(),
                ),
            )
            row = conn.execute(
                "SELECT * FROM tracks WHERE provider = ? AND remote_id = ?",
                (provider, remote_id),
            ).fetchone()
            result = dict(row)
            result["uploader"] = result.get("uploader") or result.get("creator")
            return result

    def update_creator(self, track_id: int, creator: str, *, source: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Fill or correct a track creator without changing its link/membership."""
        clean = " ".join(str(creator).split()).strip()
        if not clean:
            raise ValueError("Creator cannot be empty")
        with self.transaction() as conn:
            row = conn.execute("SELECT source_json FROM tracks WHERE id = ?", (track_id,)).fetchone()
            if row is None:
                raise ValueError(f"Track {track_id} was not found")
            merged_source: dict[str, Any] = {}
            try:
                parsed = json.loads(row["source_json"] or "{}")
                if isinstance(parsed, dict):
                    merged_source.update(parsed)
            except (TypeError, ValueError):
                pass
            if source:
                merged_source.update(dict(source))
            conn.execute(
                "UPDATE tracks SET creator = ?, uploader = ?, source_json = ?, fetched_at = ? WHERE id = ?",
                (clean, clean, json.dumps(merged_source, ensure_ascii=False), _utc_now(), track_id),
            )
            updated = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
            assert updated is not None
            result = dict(updated)
            result["uploader"] = result.get("uploader") or result.get("creator")
            return result

    def add_track_to_playlist(self, playlist_id: int, track_id: int, *, allow_duplicate: bool = False) -> tuple[dict[str, Any], bool]:
        with self.transaction() as conn:
            if not allow_duplicate:
                existing = conn.execute(
                    """
                    SELECT * FROM playlist_tracks
                    WHERE playlist_id = ? AND track_id = ? AND deleted_at IS NULL
                    ORDER BY position LIMIT 1
                    """,
                    (playlist_id, track_id),
                ).fetchone()
                if existing:
                    return dict(existing), False
            position = int(
                conn.execute(
                    "SELECT COALESCE(MAX(position), -1) + 1 FROM playlist_tracks WHERE playlist_id = ? AND deleted_at IS NULL",
                    (playlist_id,),
                ).fetchone()[0]
            )
            cursor = conn.execute(
                """
                INSERT INTO playlist_tracks(playlist_id, track_id, position, duplicate_copy)
                VALUES(?, ?, ?, ?)
                """,
                (playlist_id, track_id, position, int(allow_duplicate)),
            )
            row = conn.execute("SELECT * FROM playlist_tracks WHERE id = ?", (cursor.lastrowid,)).fetchone()
            return dict(row), True

    def add_tracks_to_playlist(
        self,
        playlist_id: int,
        track_ids: Iterable[int],
        *,
        allow_duplicate: bool = False,
    ) -> dict[str, int]:
        """Add several existing tracks to a playlist in one transaction."""
        self.get_playlist(playlist_id)
        ids = list(dict.fromkeys(int(track_id) for track_id in track_ids))
        if not ids:
            return {"added": 0, "existing": 0}
        with self.transaction() as conn:
            existing_ids = {
                int(row["track_id"])
                for row in conn.execute(
                    """
                    SELECT track_id FROM playlist_tracks
                    WHERE playlist_id = ? AND deleted_at IS NULL
                    """,
                    (playlist_id,),
                )
            }
            position = int(
                conn.execute(
                    "SELECT COALESCE(MAX(position), -1) + 1 FROM playlist_tracks WHERE playlist_id = ? AND deleted_at IS NULL",
                    (playlist_id,),
                ).fetchone()[0]
            )
            rows: list[tuple[int, int, int, int]] = []
            added = 0
            existing = 0
            for track_id in ids:
                if conn.execute("SELECT 1 FROM tracks WHERE id = ?", (track_id,)).fetchone() is None:
                    raise ValueError(f"Track {track_id} was not found")
                if track_id in existing_ids and not allow_duplicate:
                    existing += 1
                    continue
                rows.append((playlist_id, track_id, position, int(allow_duplicate)))
                position += 1
                added += 1
                if not allow_duplicate:
                    existing_ids.add(track_id)
            conn.executemany(
                """
                INSERT INTO playlist_tracks(playlist_id, track_id, position, duplicate_copy)
                VALUES(?, ?, ?, ?)
                """,
                rows,
            )
        return {"added": added, "existing": existing}

    def record_source(self, *, url: str, provider: str, target_playlist_id: int, options: Mapping[str, Any], count: int) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO sources(url, provider, target_playlist_id, options_json, last_imported_at, imported_count)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(url, target_playlist_id) DO UPDATE SET
                    provider = excluded.provider,
                    options_json = excluded.options_json,
                    last_imported_at = excluded.last_imported_at,
                    imported_count = sources.imported_count + excluded.imported_count
                """,
                (url, provider, target_playlist_id, json.dumps(dict(options)), _utc_now(), count),
            )

    def ensure_source(
        self, *, url: str, provider: str, target_playlist_id: int, options: Mapping[str, Any]
    ) -> int:
        """Return a stable source ID without claiming that an import completed."""
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO sources(url, provider, target_playlist_id, options_json, imported_count)
                VALUES(?, ?, ?, ?, 0)
                ON CONFLICT(url, target_playlist_id) DO UPDATE SET
                    provider = excluded.provider,
                    options_json = excluded.options_json
                """,
                (url, provider, target_playlist_id, json.dumps(dict(options), ensure_ascii=False)),
            )
            row = conn.execute(
                "SELECT id FROM sources WHERE url = ? AND target_playlist_id = ?",
                (url, target_playlist_id),
            ).fetchone()
            assert row is not None
            return int(row["id"])

    def source_membership(self, source_id: int, track_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT sm.*, pt.deleted_at, pt.playlist_id
                FROM source_memberships sm
                LEFT JOIN playlist_tracks pt ON pt.id = sm.playlist_track_id
                WHERE sm.source_id = ? AND sm.track_id = ?
                """,
                (source_id, track_id),
            ).fetchone()
        return dict(row) if row else None

    def mark_source_membership(
        self,
        source_id: int,
        track_id: int,
        *,
        playlist_track_id: int | None,
        owns_membership: bool,
        seen_at: str,
    ) -> None:
        """Record per-source provenance without taking ownership of manual entries."""
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO source_memberships(
                    source_id, track_id, playlist_track_id, owns_membership,
                    first_seen_at, last_seen_at, missing_since
                ) VALUES(?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(source_id, track_id) DO UPDATE SET
                    playlist_track_id = COALESCE(excluded.playlist_track_id, source_memberships.playlist_track_id),
                    owns_membership = MAX(source_memberships.owns_membership, excluded.owns_membership),
                    last_seen_at = excluded.last_seen_at,
                    missing_since = NULL
                """,
                (
                    source_id,
                    track_id,
                    playlist_track_id,
                    int(owns_membership),
                    seen_at,
                    seen_at,
                ),
            )

    def reconcile_source_memberships(
        self,
        source_id: int,
        *,
        seen_track_ids: Iterable[int],
        seen_at: str,
        policy: str,
    ) -> dict[str, int]:
        """Reconcile only playlist entries that provider sources actually own."""
        normalized_policy = str(policy or "append_only").strip().lower()
        if normalized_policy not in {"append_only", "mirror", "mirror_preserve_local_removals"}:
            raise ValueError(f"Unsupported source synchronization policy: {policy}")
        seen_ids = set(int(value) for value in seen_track_ids)
        if normalized_policy == "append_only":
            return {"missing": 0, "removed": 0}
        removed = 0
        with self.transaction() as conn:
            rows = conn.execute(
                """
                SELECT sm.*, s.target_playlist_id
                FROM source_memberships sm
                JOIN sources s ON s.id = sm.source_id
                WHERE sm.source_id = ?
                """,
                (source_id,),
            ).fetchall()
            for row in rows:
                if int(row["track_id"]) in seen_ids:
                    continue
                conn.execute(
                    "UPDATE source_memberships SET missing_since = COALESCE(missing_since, ?) "
                    "WHERE source_id = ? AND track_id = ?",
                    (seen_at, source_id, row["track_id"]),
                )
                membership_id = row["playlist_track_id"]
                if not membership_id or not bool(row["owns_membership"]):
                    continue
                # A second current source keeps the shared membership alive.
                other = conn.execute(
                    """
                    SELECT 1
                    FROM source_memberships other
                    JOIN sources other_source ON other_source.id = other.source_id
                    WHERE other.playlist_track_id = ?
                      AND other.source_id != ?
                      AND other.missing_since IS NULL
                      AND other.owns_membership = 1
                      AND other_source.target_playlist_id = ?
                    LIMIT 1
                    """,
                    (membership_id, source_id, row["target_playlist_id"]),
                ).fetchone()
                if other:
                    continue
                changed = conn.execute(
                    """
                    UPDATE playlist_tracks
                    SET deleted_at = ?, deleted_position = position
                    WHERE id = ? AND deleted_at IS NULL
                    """,
                    (seen_at, membership_id),
                ).rowcount
                removed += int(changed or 0)
        return {"missing": sum(1 for row in rows if int(row["track_id"]) not in seen_ids), "removed": removed}

    def get_source(self, source_id: int) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT s.*, p.name AS target_playlist_name
                FROM sources s JOIN playlists p ON p.id = s.target_playlist_id
                WHERE s.id = ?
                """,
                (source_id,),
            ).fetchone()
        if row is None:
            raise ValueError("Source was not found")
        result = dict(row)
        result["options"] = json.loads(result.pop("options_json"))
        return result

    def record_source_run(
        self,
        *,
        url: str,
        provider: str,
        target_playlist_id: int,
        options: Mapping[str, Any],
        started_at: str,
        discovered_count: int,
        added_count: int,
        error: str | None = None,
    ) -> None:
        """Keep a readable refresh/import audit trail alongside the source."""
        with self.transaction() as conn:
            source = conn.execute(
                "SELECT id FROM sources WHERE url = ? AND target_playlist_id = ?",
                (url, target_playlist_id),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO source_runs(
                    source_id, source_url, provider, target_playlist_id, options_json,
                    started_at, finished_at, discovered_count, added_count, error
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source["id"] if source else None,
                    url,
                    provider,
                    target_playlist_id,
                    json.dumps(dict(options), ensure_ascii=False),
                    started_at,
                    _utc_now(),
                    discovered_count,
                    added_count,
                    error,
                ),
            )

    def list_sources(self) -> list[dict[str, Any]]:
        rows = self._rows(
            """
            SELECT s.*, p.name AS target_playlist_name
            FROM sources s JOIN playlists p ON p.id = s.target_playlist_id
            ORDER BY s.last_imported_at DESC
            """
        )
        for row in rows:
            row["options"] = json.loads(row.pop("options_json"))
        return rows

    def create_subscription(
        self,
        *,
        name: str,
        url: str,
        provider: str,
        target_playlist_id: int,
        options: Mapping[str, Any],
    ) -> dict[str, Any]:
        cleaned_name = " ".join(str(name or "").split()).strip() or url
        self.get_playlist(target_playlist_id)
        with self.transaction() as conn:
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO subscriptions(name, url, provider, target_playlist_id, options_json, created_at)
                    VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (cleaned_name, url, provider, target_playlist_id, json.dumps(dict(options), ensure_ascii=False), _utc_now()),
                )
            except sqlite3.IntegrityError:
                row = conn.execute(
                    "SELECT * FROM subscriptions WHERE url = ? AND target_playlist_id = ?",
                    (url, target_playlist_id),
                ).fetchone()
                if row is None:
                    raise ValueError("That subscription already exists") from None
                return self._subscription_row(row)
            row = conn.execute("SELECT * FROM subscriptions WHERE id = ?", (cursor.lastrowid,)).fetchone()
            assert row is not None
            return self._subscription_row(row)

    @staticmethod
    def _subscription_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["options"] = json.loads(result.pop("options_json") or "{}")
        result["enabled"] = bool(result["enabled"])
        return result

    def get_subscription(self, subscription_id: int) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT s.*, p.name AS target_playlist_name
                FROM subscriptions s JOIN playlists p ON p.id = s.target_playlist_id
                WHERE s.id = ?
                """,
                (subscription_id,),
            ).fetchone()
        if row is None:
            raise ValueError("Subscription was not found")
        return self._subscription_row(row)

    def list_subscriptions(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT s.*, p.name AS target_playlist_name
                FROM subscriptions s JOIN playlists p ON p.id = s.target_playlist_id
                ORDER BY s.enabled DESC, s.name COLLATE NOCASE, s.id
                """
            ).fetchall()
        return [self._subscription_row(row) for row in rows]

    def update_subscription(
        self,
        subscription_id: int,
        *,
        enabled: bool | None = None,
        last_sync_at: str | None = None,
        last_error: str | None = None,
    ) -> dict[str, Any]:
        self.get_subscription(subscription_id)
        assignments: list[str] = []
        values: list[Any] = []
        if enabled is not None:
            assignments.append("enabled = ?")
            values.append(int(enabled))
        if last_sync_at is not None:
            assignments.append("last_sync_at = ?")
            values.append(last_sync_at)
        if last_error is not None:
            assignments.append("last_error = ?")
            values.append(last_error)
        elif last_sync_at is not None:
            assignments.append("last_error = NULL")
        if assignments:
            values.append(subscription_id)
            with self.transaction() as conn:
                conn.execute(f"UPDATE subscriptions SET {', '.join(assignments)} WHERE id = ?", values)
        return self.get_subscription(subscription_id)

    def delete_subscription(self, subscription_id: int) -> None:
        with self.transaction() as conn:
            cursor = conn.execute("DELETE FROM subscriptions WHERE id = ?", (subscription_id,))
            if cursor.rowcount == 0:
                raise ValueError("Subscription was not found")

    def track_status(self, *, provider: str, remote_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT t.id, t.title, t.creator, t.uploader, t.provider, t.url, r.value AS rating,
                       GROUP_CONCAT(DISTINCT p.name) AS playlists
                FROM tracks t
                LEFT JOIN ratings r ON r.track_id = t.id
                LEFT JOIN playlist_tracks pt ON pt.track_id = t.id AND pt.deleted_at IS NULL
                LEFT JOIN playlists p ON p.id = pt.playlist_id AND p.archived_at IS NULL
                WHERE t.provider = ? AND t.remote_id = ? AND t.hidden_at IS NULL
                GROUP BY t.id
                """,
                (provider, remote_id),
            ).fetchone()
        if row is None:
            return {"saved": False}
        result = dict(row)
        result["uploader"] = result.get("uploader") or result.get("creator")
        result["saved"] = True
        return result

    def list_creators(self) -> list[dict[str, Any]]:
        rows = self._rows(
            """
            SELECT creator, provider, COUNT(*) AS track_count,
                   SUM(CASE WHEN r.value IS NOT NULL THEN 1 ELSE 0 END) AS rated_count,
                   COALESCE(SUM(view_count), 0) AS total_views,
                   MAX(fetched_at) AS last_seen
            FROM tracks t
            LEFT JOIN ratings r ON r.track_id = t.id
            WHERE t.hidden_at IS NULL AND creator IS NOT NULL AND TRIM(creator) <> ''
            GROUP BY creator, provider
            ORDER BY creator COLLATE NOCASE, provider
            """
        )
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = str(row["creator"])
            result = grouped.setdefault(
                key,
                {
                    "name": key,
                    "uploader": key,
                    "track_count": 0,
                    "rated_count": 0,
                    "total_views": 0,
                    "providers": [],
                    "url": None,
                    "last_seen": row["last_seen"],
                },
            )
            result["track_count"] += int(row["track_count"])
            result["rated_count"] += int(row["rated_count"] or 0)
            result["total_views"] += int(row["total_views"] or 0)
            if row["provider"] not in result["providers"]:
                result["providers"].append(row["provider"])
        with self._lock:
            track_rows = self._conn.execute(
                "SELECT creator, source_json FROM tracks WHERE hidden_at IS NULL AND creator IS NOT NULL"
            ).fetchall()
        for row in track_rows:
            result = grouped.get(str(row["creator"]))
            if not result or result["url"]:
                continue
            try:
                source = json.loads(row["source_json"] or "{}")
            except (TypeError, ValueError):
                source = {}
            result["url"] = source.get("channel_url") or source.get("uploader_url")
        return list(grouped.values())

    def list_source_runs(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return self._rows(
            """
            SELECT sr.*, p.name AS target_playlist_name
            FROM source_runs sr
            JOIN playlists p ON p.id = sr.target_playlist_id
            ORDER BY sr.started_at DESC
            LIMIT ?
            """,
            (max(1, min(limit, 1000)),),
        )

    def track_count(
        self,
        *,
        query: str = "",
        creator: str = "",
        playlist_id: int | None = None,
        include_hidden: bool = False,
        min_rating: float | None = None,
        provider: str | None = None,
        artist: str = "",
        album: str = "",
        genre: str = "",
        availability: str | None = None,
        min_duration: float | None = None,
        max_duration: float | None = None,
        min_views: int | None = None,
        max_views: int | None = None,
        has_override: bool | None = None,
    ) -> int:
        clauses = ["1 = 1"]
        values: list[Any] = []
        if not include_hidden:
            clauses.append("t.hidden_at IS NULL")
        if query.strip():
            clauses.append("""(t.title LIKE ? OR COALESCE(t.creator, '') LIKE ? OR
                EXISTS(SELECT 1 FROM track_metadata tm WHERE tm.track_id=t.id AND
                (COALESCE(tm.user_value, tm.provider_value, '') LIKE ?)))""")
            like = f"%{query.strip()}%"
            values.extend((like, like, like))
        if creator.strip():
            clauses.append("COALESCE(t.creator, '') LIKE ?")
            values.append(f"%{creator.strip()}%")
        if playlist_id is not None:
            clauses.append(
                "EXISTS(SELECT 1 FROM playlist_tracks pt2 WHERE pt2.playlist_id = ? AND pt2.track_id = t.id AND pt2.deleted_at IS NULL)"
            )
            values.append(playlist_id)
        if min_rating is not None:
            clauses.append(
                "EXISTS(SELECT 1 FROM ratings r2 WHERE r2.track_id = t.id AND r2.value >= ?)"
            )
            values.append(min_rating)
        if provider:
            clauses.append("t.provider = ?")
            values.append(provider)
        for field, needle in (("artist", artist), ("album", album), ("genre", genre)):
            if str(needle or "").strip():
                clauses.append(
                    "EXISTS(SELECT 1 FROM track_metadata tmf WHERE tmf.track_id=t.id AND tmf.field=? "
                    "AND COALESCE(tmf.user_value, tmf.provider_value, '') LIKE ?)"
                )
                values.extend((field, f"%{str(needle).strip()}%"))
        if availability:
            clauses.append(
                "EXISTS(SELECT 1 FROM track_metadata tma WHERE tma.track_id=t.id AND tma.field IN ('availability','status') "
                "AND COALESCE(tma.user_value, tma.provider_value, '') = ?)"
            )
            values.append(str(availability))
        if min_duration is not None:
            clauses.append("COALESCE(t.duration, 0) >= ?"); values.append(float(min_duration))
        if max_duration is not None:
            clauses.append("COALESCE(t.duration, 0) <= ?"); values.append(float(max_duration))
        if min_views is not None:
            clauses.append("COALESCE(t.view_count, 0) >= ?"); values.append(int(min_views))
        if max_views is not None:
            clauses.append("COALESCE(t.view_count, 0) <= ?"); values.append(int(max_views))
        if has_override is not None:
            if has_override:
                clauses.append("EXISTS(SELECT 1 FROM track_metadata tmo WHERE tmo.track_id=t.id AND tmo.override_active = 1)")
            else:
                clauses.append("NOT EXISTS(SELECT 1 FROM track_metadata tmo WHERE tmo.track_id=t.id AND tmo.override_active = 1)")
        with self._lock:
            return int(
                self._conn.execute(
                    f"SELECT COUNT(*) FROM tracks t WHERE {' AND '.join(clauses)}", values
                ).fetchone()[0]
            )

    def list_tracks(
        self,
        *,
        query: str = "",
        creator: str = "",
        playlist_id: int | None = None,
        include_hidden: bool = False,
        min_rating: float | None = None,
        provider: str | None = None,
        sort: str = "creator",
        order: str = "asc",
        limit: int = 500,
        offset: int = 0,
        artist: str = "",
        album: str = "",
        genre: str = "",
        availability: str | None = None,
        min_duration: float | None = None,
        max_duration: float | None = None,
        min_views: int | None = None,
        max_views: int | None = None,
        has_override: bool | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["1 = 1"]
        values: list[Any] = []
        if not include_hidden:
            clauses.append("t.hidden_at IS NULL")
        if query.strip():
            clauses.append("""(t.title LIKE ? OR COALESCE(t.creator, '') LIKE ? OR
                EXISTS(SELECT 1 FROM track_metadata tm WHERE tm.track_id=t.id AND
                COALESCE(tm.user_value, tm.provider_value, '') LIKE ?))""")
            like = f"%{query.strip()}%"
            values.extend((like, like, like))
        if creator.strip():
            clauses.append("COALESCE(t.creator, '') LIKE ?")
            values.append(f"%{creator.strip()}%")
        if playlist_id is not None:
            clauses.append(
                "EXISTS(SELECT 1 FROM playlist_tracks pt2 WHERE pt2.playlist_id = ? AND pt2.track_id = t.id AND pt2.deleted_at IS NULL)"
            )
            values.append(playlist_id)
        if min_rating is not None:
            clauses.append("COALESCE(r.value, -1) >= ?")
            values.append(min_rating)
        if provider:
            clauses.append("t.provider = ?")
            values.append(provider)
        for field, needle in (("artist", artist), ("album", album), ("genre", genre)):
            if str(needle or "").strip():
                clauses.append(
                    "EXISTS(SELECT 1 FROM track_metadata tmf WHERE tmf.track_id=t.id AND tmf.field=? "
                    "AND COALESCE(tmf.user_value, tmf.provider_value, '') LIKE ?)"
                )
                values.extend((field, f"%{str(needle).strip()}%"))
        if availability:
            clauses.append(
                "EXISTS(SELECT 1 FROM track_metadata tma WHERE tma.track_id=t.id AND tma.field IN ('availability','status') "
                "AND COALESCE(tma.user_value, tma.provider_value, '') = ?)"
            )
            values.append(str(availability))
        if min_duration is not None:
            clauses.append("COALESCE(t.duration, 0) >= ?"); values.append(float(min_duration))
        if max_duration is not None:
            clauses.append("COALESCE(t.duration, 0) <= ?"); values.append(float(max_duration))
        if min_views is not None:
            clauses.append("COALESCE(t.view_count, 0) >= ?"); values.append(int(min_views))
        if max_views is not None:
            clauses.append("COALESCE(t.view_count, 0) <= ?"); values.append(int(max_views))
        if has_override is not None:
            if has_override:
                clauses.append("EXISTS(SELECT 1 FROM track_metadata tmo WHERE tmo.track_id=t.id AND tmo.override_active = 1)")
            else:
                clauses.append("NOT EXISTS(SELECT 1 FROM track_metadata tmo WHERE tmo.track_id=t.id AND tmo.override_active = 1)")
        sort_columns = {
            "creator": "COALESCE(t.creator, '') COLLATE NOCASE",
            "uploader": "COALESCE(t.uploader, t.creator, '') COLLATE NOCASE",
            "title": "t.title COLLATE NOCASE",
            "provider": "t.provider COLLATE NOCASE",
            "views": "COALESCE(t.view_count, -1)",
            "rating": "COALESCE(r.value, -1)",
            "artist": "COALESCE((SELECT COALESCE(user_value, provider_value) FROM track_metadata WHERE track_id=t.id AND field='artist' LIMIT 1), '') COLLATE NOCASE",
            "album": "COALESCE((SELECT COALESCE(user_value, provider_value) FROM track_metadata WHERE track_id=t.id AND field='album' LIMIT 1), '') COLLATE NOCASE",
            "genre": "COALESCE((SELECT COALESCE(user_value, provider_value) FROM track_metadata WHERE track_id=t.id AND field='genre' LIMIT 1), '') COLLATE NOCASE",
            "duration": "COALESCE(t.duration, -1)",
            "least_recently_played": "COALESCE((SELECT MAX(h.played_at) FROM playback_history h WHERE h.track_id=t.id), '')",
            "last_played": "COALESCE((SELECT MAX(h.played_at) FROM playback_history h WHERE h.track_id=t.id), '')",
            "date_added": "t.fetched_at",
        }
        sort_expression = sort_columns.get(str(sort).lower(), sort_columns["creator"])
        direction = "DESC" if str(order).lower() == "desc" else "ASC"
        values.extend((max(1, min(limit, 2000)), max(0, offset)))
        items = self._rows(
            f"""
            SELECT t.*, r.value AS rating,
                   (SELECT MAX(h.played_at) FROM playback_history h WHERE h.track_id=t.id) AS last_played_at,
                   GROUP_CONCAT(DISTINCT p.name) AS playlists
            FROM tracks t
            LEFT JOIN ratings r ON r.track_id = t.id
            LEFT JOIN playlist_tracks pt ON pt.track_id = t.id AND pt.deleted_at IS NULL
            LEFT JOIN playlists p ON p.id = pt.playlist_id AND p.archived_at IS NULL
            WHERE {' AND '.join(clauses)}
            GROUP BY t.id
            ORDER BY {sort_expression} {direction}, t.title COLLATE NOCASE, t.id
            LIMIT ? OFFSET ?
            """,
            values,
        )
        return [self._decorate_track(item) for item in items]

    def playlist_tracks(self, playlist_id: int, *, include_deleted: bool = False) -> list[dict[str, Any]]:
        deleted_clause = "" if include_deleted else "AND pt.deleted_at IS NULL"
        items = self._rows(
            f"""
            SELECT pt.id AS membership_id, pt.position, pt.duplicate_copy, pt.deleted_at,
                   t.*, r.value AS rating,
                   (SELECT MAX(h.played_at) FROM playback_history h WHERE h.track_id=t.id) AS last_played_at
            FROM playlist_tracks pt
            JOIN tracks t ON t.id = pt.track_id
            LEFT JOIN ratings r ON r.track_id = t.id
            WHERE pt.playlist_id = ? {deleted_clause}
            ORDER BY COALESCE(pt.deleted_position, pt.position), pt.id
            """,
            (playlist_id,),
        )
        return [self._decorate_track(item) for item in items]

    def playlist_track_page(
        self,
        playlist_id: int,
        *,
        query: str = "",
        creator: str = "",
        provider: str | None = None,
        min_rating: float | None = None,
        sort: str = "position",
        order: str = "asc",
        limit: int = 250,
        offset: int = 0,
        artist: str = "",
        album: str = "",
        genre: str = "",
        availability: str | None = None,
        min_duration: float | None = None,
        max_duration: float | None = None,
        min_views: int | None = None,
        max_views: int | None = None,
        has_override: bool | None = None,
    ) -> dict[str, Any]:
        """Return a filtered page for both manual and dynamic smart playlists."""
        playlist = self.get_playlist(playlist_id)
        bounded_limit = max(1, min(limit, 2000))
        bounded_offset = max(0, offset)
        if playlist["kind"] == "smart":
            selection = json.loads(playlist["query_json"])
            tracks = self.smart_playlist_tracks(selection)
            needle = query.casefold().strip()
            if needle:
                tracks = [
                    track
                    for track in tracks
                    if needle in str(track["title"]).casefold()
                    or needle in str(track.get("creator") or "").casefold()
                ]
            creator_needle = creator.casefold().strip()
            if creator_needle:
                tracks = [
                    track
                    for track in tracks
                    if creator_needle in str(track.get("creator") or "").casefold()
                ]
            if provider:
                tracks = [track for track in tracks if track["provider"] == provider]
            if min_rating is not None:
                tracks = [track for track in tracks if (track.get("rating") or -1) >= min_rating]
            for field, needle in (("artist", artist), ("album", album), ("genre", genre)):
                if str(needle or "").strip():
                    tracks = [track for track in tracks if str(track.get(field) or "").casefold().find(str(needle).casefold()) >= 0]
            if availability:
                tracks = [track for track in tracks if str(track.get("availability") or track.get("status") or "") == str(availability)]
            if min_duration is not None:
                tracks = [track for track in tracks if float(track.get("duration") or 0) >= float(min_duration)]
            if max_duration is not None:
                tracks = [track for track in tracks if float(track.get("duration") or 0) <= float(max_duration)]
            if min_views is not None:
                tracks = [track for track in tracks if int(track.get("view_count") or 0) >= int(min_views)]
            if max_views is not None:
                tracks = [track for track in tracks if int(track.get("view_count") or 0) <= int(max_views)]
            if has_override is not None:
                tracks = [track for track in tracks if any(track.get("metadata_overrides", {}).values()) == bool(has_override)]
            self._sort_track_dicts(tracks, sort=sort, order=order)
            total = len(tracks)
            return {"items": tracks[bounded_offset : bounded_offset + bounded_limit], "total": total}

        clauses = ["pt.playlist_id = ?", "pt.deleted_at IS NULL"]
        values: list[Any] = [playlist_id]
        if query.strip():
            clauses.append("""(t.title LIKE ? OR COALESCE(t.creator, '') LIKE ? OR
                EXISTS(SELECT 1 FROM track_metadata tm WHERE tm.track_id=t.id
                AND COALESCE(tm.user_value, tm.provider_value, '') LIKE ?))""")
            like = f"%{query.strip()}%"
            values.extend((like, like, like))
        if creator.strip():
            clauses.append("COALESCE(t.creator, '') LIKE ?")
            values.append(f"%{creator.strip()}%")
        if provider:
            clauses.append("t.provider = ?")
            values.append(provider)
        if min_rating is not None:
            clauses.append("COALESCE(r.value, -1) >= ?")
            values.append(min_rating)
        for field, needle in (("artist", artist), ("album", album), ("genre", genre)):
            if str(needle or "").strip():
                clauses.append(
                    "EXISTS(SELECT 1 FROM track_metadata tmf WHERE tmf.track_id=t.id AND tmf.field=? "
                    "AND COALESCE(tmf.user_value, tmf.provider_value, '') LIKE ?)"
                )
                values.extend((field, f"%{str(needle).strip()}%"))
        if availability:
            clauses.append(
                "EXISTS(SELECT 1 FROM track_metadata tma WHERE tma.track_id=t.id AND tma.field IN ('availability','status') "
                "AND COALESCE(tma.user_value, tma.provider_value, '') = ?)"
            )
            values.append(str(availability))
        if min_duration is not None:
            clauses.append("COALESCE(t.duration, 0) >= ?"); values.append(float(min_duration))
        if max_duration is not None:
            clauses.append("COALESCE(t.duration, 0) <= ?"); values.append(float(max_duration))
        if min_views is not None:
            clauses.append("COALESCE(t.view_count, 0) >= ?"); values.append(int(min_views))
        if max_views is not None:
            clauses.append("COALESCE(t.view_count, 0) <= ?"); values.append(int(max_views))
        if has_override is not None:
            if has_override:
                clauses.append("EXISTS(SELECT 1 FROM track_metadata tmo WHERE tmo.track_id=t.id AND tmo.override_active = 1)")
            else:
                clauses.append("NOT EXISTS(SELECT 1 FROM track_metadata tmo WHERE tmo.track_id=t.id AND tmo.override_active = 1)")
        count_values = list(values)
        values.extend((bounded_limit, bounded_offset))
        sort_columns = {
            "position": "pt.position",
            "creator": "COALESCE(t.creator, '') COLLATE NOCASE",
            "title": "t.title COLLATE NOCASE",
            "provider": "t.provider COLLATE NOCASE",
            "views": "COALESCE(t.view_count, -1)",
            "rating": "COALESCE(r.value, -1)",
            "uploader": "COALESCE(t.uploader, t.creator, '') COLLATE NOCASE",
            "artist": "COALESCE((SELECT COALESCE(user_value, provider_value) FROM track_metadata WHERE track_id=t.id AND field='artist' LIMIT 1), '') COLLATE NOCASE",
            "album": "COALESCE((SELECT COALESCE(user_value, provider_value) FROM track_metadata WHERE track_id=t.id AND field='album' LIMIT 1), '') COLLATE NOCASE",
            "genre": "COALESCE((SELECT COALESCE(user_value, provider_value) FROM track_metadata WHERE track_id=t.id AND field='genre' LIMIT 1), '') COLLATE NOCASE",
            "duration": "COALESCE(t.duration, -1)",
            "least_recently_played": "COALESCE((SELECT MAX(h.played_at) FROM playback_history h WHERE h.track_id=t.id), '')",
            "last_played": "COALESCE((SELECT MAX(h.played_at) FROM playback_history h WHERE h.track_id=t.id), '')",
            "date_added": "t.fetched_at",
        }
        sort_expression = sort_columns.get(str(sort).lower(), sort_columns["position"])
        direction = "DESC" if str(order).lower() == "desc" else "ASC"
        items = self._rows(
            f"""
            SELECT pt.id AS membership_id, pt.position, pt.duplicate_copy, pt.deleted_at,
                   t.*, r.value AS rating,
                   (SELECT MAX(h.played_at) FROM playback_history h WHERE h.track_id=t.id) AS last_played_at
            FROM playlist_tracks pt
            JOIN tracks t ON t.id = pt.track_id
            LEFT JOIN ratings r ON r.track_id = t.id
            WHERE {' AND '.join(clauses)}
            ORDER BY {sort_expression} {direction}, pt.id
            LIMIT ? OFFSET ?
            """,
            values,
        )
        with self._lock:
            total = int(
                self._conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM playlist_tracks pt
                    JOIN tracks t ON t.id = pt.track_id
                    LEFT JOIN ratings r ON r.track_id = t.id
                    WHERE {' AND '.join(clauses)}
                    """,
                    count_values,
                ).fetchone()[0]
            )
        return {"items": [self._decorate_track(item) for item in items], "total": total}

    @staticmethod
    def _sort_track_dicts(tracks: list[dict[str, Any]], *, sort: str, order: str) -> None:
        key_map = {
            "creator": lambda item: str(item.get("creator") or "").casefold(),
            "uploader": lambda item: str(item.get("uploader") or item.get("creator") or "").casefold(),
            "title": lambda item: str(item.get("title") or "").casefold(),
            "provider": lambda item: str(item.get("provider") or "").casefold(),
            "views": lambda item: item.get("view_count") if item.get("view_count") is not None else -1,
            "rating": lambda item: item.get("rating") if item.get("rating") is not None else -1,
            "artist": lambda item: str(item.get("artist") or "").casefold(),
            "album": lambda item: str(item.get("album") or "").casefold(),
            "genre": lambda item: str(item.get("genre") or "").casefold(),
            "duration": lambda item: item.get("duration") if item.get("duration") is not None else -1,
            "date_added": lambda item: str(item.get("fetched_at") or ""),
            # Empty timestamps sort before real timestamps in ascending order,
            # which gives “never played first, oldest play next”.
            "least_recently_played": lambda item: str(item.get("last_played_at") or ""),
            "last_played": lambda item: str(item.get("last_played_at") or ""),
        }
        key = key_map.get(str(sort).lower(), key_map["title"])
        tracks.sort(key=key, reverse=str(order).lower() == "desc")

    def get_track(self, track_id: int) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT t.*, r.value AS rating
                FROM tracks t LEFT JOIN ratings r ON r.track_id = t.id
                WHERE t.id = ?
                """,
                (track_id,),
            ).fetchone()
        if row is None:
            raise ValueError("Track was not found")
        return self._decorate_track(dict(row))

    _METADATA_FIELDS = (
        "artist", "album", "album_artist", "composer", "featured_artists",
        "release_year", "genre", "tags", "notes", "aliases", "artwork_url",
        "availability", "status", "track_number", "disc_number", "release_date",
        "original_release_date", "label", "catalog_number", "country", "language",
        "explicit", "version", "is_live", "is_acoustic", "is_instrumental",
        "bpm", "musical_key", "mood", "energy", "isrc", "upc", "musicbrainz_recording_id",
        "musicbrainz_release_id", "discogs_release_id", "wikidata_id", "lyrics_url",
        "copyright", "license", "play_count", "skip_count", "last_played_at",
    )

    def _decorate_track(self, track: dict[str, Any]) -> dict[str, Any]:
        track["uploader"] = track.get("uploader") or track.get("creator")
        try:
            metadata = self.get_track_metadata(int(track["id"]))
        except Exception:
            metadata = {}
        for field, value in metadata.get("effective", {}).items():
            track[field] = value
        track["metadata_overrides"] = metadata.get("overrides", {})
        return track

    def get_track_metadata(self, track_id: int) -> dict[str, Any]:
        self.get_track_base(track_id)
        rows = self._rows(
            "SELECT field, provider_value, user_value, override_active, source, confidence, updated_at "
            "FROM track_metadata WHERE track_id = ? ORDER BY field", (track_id,)
        )
        effective: dict[str, Any] = {}
        overrides: dict[str, bool] = {}
        fields: dict[str, dict[str, Any]] = {}
        for row in rows:
            value = row["user_value"] if row["override_active"] else row["provider_value"]
            if row["field"] in {"tags", "aliases"} and isinstance(value, str):
                try:
                    value = json.loads(value)
                except (TypeError, ValueError):
                    value = [item.strip() for item in value.split(",") if item.strip()]
            effective[row["field"]] = value
            overrides[row["field"]] = bool(row["override_active"])
            fields[row["field"]] = row
        return {"track_id": track_id, "effective": effective, "overrides": overrides, "fields": fields}

    def get_track_base(self, track_id: int) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
        if row is None:
            raise ValueError("Track was not found")
        return dict(row)

    def update_track_metadata(
        self, track_id: int, values: Mapping[str, Any], *,
        source: str = "user", activate_override: bool = True, operation_id: str | None = None,
    ) -> dict[str, Any]:
        self.get_track_base(track_id)
        import uuid
        op_id = operation_id or uuid.uuid4().hex
        now = _utc_now()
        with self.transaction() as conn:
            for field, raw_value in values.items():
                if field not in self._METADATA_FIELDS:
                    continue
                before = conn.execute(
                    "SELECT * FROM track_metadata WHERE track_id = ? AND field = ?", (track_id, field)
                ).fetchone()
                before_dict = dict(before) if before else {}
                value = raw_value
                if field in {"tags", "aliases"} and isinstance(value, (list, tuple, set)):
                    value = json.dumps([str(v) for v in value], ensure_ascii=False)
                elif value is not None and not isinstance(value, (str, int, float)):
                    value = str(value)
                conn.execute(
                    """
                    INSERT INTO track_metadata(track_id, field, provider_value, user_value, override_active, source, confidence, updated_at)
                    VALUES(?, ?, COALESCE((SELECT provider_value FROM track_metadata WHERE track_id=? AND field=?), NULL), ?, ?, ?, NULL, ?)
                    ON CONFLICT(track_id, field) DO UPDATE SET user_value=excluded.user_value,
                        override_active=excluded.override_active, source=excluded.source, updated_at=excluded.updated_at
                    """,
                    (track_id, field, track_id, field, value, int(activate_override), source, now),
                )
                after = conn.execute(
                    "SELECT * FROM track_metadata WHERE track_id = ? AND field = ?", (track_id, field)
                ).fetchone()
                conn.execute(
                    "INSERT INTO metadata_audit(operation_id, track_id, field, before_json, after_json, created_at) VALUES(?, ?, ?, ?, ?, ?)",
                    (op_id, track_id, field, json.dumps(before_dict, ensure_ascii=False), json.dumps(dict(after) if after else {}, ensure_ascii=False), now),
                )
        return self.get_track(track_id)

    def refresh_provider_metadata(self, track_id: int, values: Mapping[str, Any], *, source: str = "provider", confidence: float | None = None) -> dict[str, Any]:
        self.get_track_base(track_id)
        with self.transaction() as conn:
            for field, raw_value in values.items():
                if field not in self._METADATA_FIELDS:
                    continue
                value = json.dumps(raw_value, ensure_ascii=False) if field in {"tags", "aliases"} and isinstance(raw_value, (list, tuple, set)) else raw_value
                conn.execute(
                    """
                    INSERT INTO track_metadata(track_id, field, provider_value, override_active, source, confidence, updated_at)
                    VALUES(?, ?, ?, 0, ?, ?, ?)
                    ON CONFLICT(track_id, field) DO UPDATE SET provider_value=excluded.provider_value,
                        source=excluded.source, confidence=excluded.confidence, updated_at=excluded.updated_at
                    """,
                    (track_id, field, value, source, confidence, _utc_now()),
                )
        return self.get_track(track_id)

    def undo_metadata(self, operation_id: str) -> int:
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM metadata_audit WHERE operation_id = ? ORDER BY id DESC", (operation_id,)
            ).fetchall()
            for row in rows:
                before = json.loads(row["before_json"] or "{}")
                if not before:
                    conn.execute("DELETE FROM track_metadata WHERE track_id = ? AND field = ?", (row["track_id"], row["field"]))
                else:
                    conn.execute(
                        """INSERT INTO track_metadata(track_id, field, provider_value, user_value, override_active, source, confidence, updated_at)
                        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(track_id, field) DO UPDATE SET provider_value=excluded.provider_value,
                        user_value=excluded.user_value, override_active=excluded.override_active,
                        source=excluded.source, confidence=excluded.confidence, updated_at=excluded.updated_at""",
                        (row["track_id"], row["field"], before.get("provider_value"), before.get("user_value"),
                         before.get("override_active", 0), before.get("source"), before.get("confidence"), _utc_now()),
                    )
            return len(rows)

    @staticmethod
    def _metadata_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    value = parsed
                else:
                    value = value.split(",")
            except (TypeError, ValueError):
                value = value.split(",")
        if not isinstance(value, (list, tuple, set)):
            value = [value]
        return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))

    def _bulk_metadata_plan(
        self,
        track_ids: Iterable[int],
        *,
        values: Mapping[str, Any] | None = None,
        operations: Iterable[Mapping[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        ids = list(dict.fromkeys(int(i) for i in track_ids))
        operation_list = [dict(item) for item in (operations or [])]
        operation_list.extend(
            {"type": "set", "field": field, "value": value}
            for field, value in dict(values or {}).items()
        )
        if not operation_list:
            raise ValueError("Add at least one bulk metadata operation")

        copy_cache: dict[int, dict[str, Any]] = {}
        plans: list[dict[str, Any]] = []
        for track_id in ids:
            track = self.get_track(track_id)
            working = {field: track.get(field) for field in self._METADATA_FIELDS}
            before = dict(working)
            overrides = dict(track.get("metadata_overrides") or {})
            target_override = dict(overrides)
            touched: set[str] = set()
            for operation in operation_list:
                kind = str(operation.get("type") or "set").strip().lower()
                field = str(operation.get("field") or "").strip()
                if kind == "copy_from_track":
                    source_id = int(operation.get("source_track_id") or 0)
                    if source_id == track_id:
                        continue
                    source_track = copy_cache.setdefault(source_id, self.get_track(source_id))
                    fields = operation.get("fields") or self._METADATA_FIELDS
                    for copied_field in fields:
                        copied_field = str(copied_field)
                        if copied_field in self._METADATA_FIELDS:
                            working[copied_field] = source_track.get(copied_field)
                            target_override[copied_field] = True
                            touched.add(copied_field)
                    continue
                if field not in self._METADATA_FIELDS:
                    raise ValueError(f"Unsupported metadata field: {field or '(missing)'}")
                current = working.get(field)
                if kind == "set":
                    working[field] = operation.get("value")
                    target_override[field] = bool(operation.get("override", True))
                elif kind == "clear":
                    working[field] = None
                    target_override[field] = True
                elif kind in {"set_refresh", "provider_refresh"}:
                    # Enabling provider refresh disables the local override;
                    # disabling it freezes the current effective value.
                    target_override[field] = not bool(operation.get("enabled", True))
                elif kind == "find_replace":
                    working[field] = str(current or "").replace(
                        str(operation.get("find") or ""), str(operation.get("replacement") or "")
                    )
                    target_override[field] = True
                elif kind == "regex_replace":
                    flags = re.IGNORECASE if operation.get("ignore_case") else 0
                    try:
                        working[field] = re.sub(
                            str(operation.get("pattern") or ""),
                            str(operation.get("replacement") or ""),
                            str(current or ""),
                            flags=flags,
                        )
                    except re.error as exc:
                        raise ValueError(f"Invalid regular expression: {exc}") from exc
                    target_override[field] = True
                elif kind in {"exact_normalize", "normalize_exact"}:
                    mapping = operation.get("mapping")
                    if isinstance(mapping, Mapping):
                        key = str(current or "")
                        if key in mapping:
                            working[field] = mapping[key]
                    elif str(current or "") == str(operation.get("match") or ""):
                        working[field] = operation.get("replacement")
                    target_override[field] = True
                elif kind in {"tags_add", "tags_remove", "tags_replace"}:
                    if field not in {"tags", "aliases"}:
                        raise ValueError(f"{kind} can only be used with tags or aliases")
                    requested = self._metadata_list(operation.get("values"))
                    current_values = self._metadata_list(current)
                    if kind == "tags_add":
                        working[field] = list(dict.fromkeys(current_values + requested))
                    elif kind == "tags_remove":
                        removals = {value.casefold() for value in requested}
                        working[field] = [value for value in current_values if value.casefold() not in removals]
                    else:
                        working[field] = requested
                    target_override[field] = True
                else:
                    raise ValueError(f"Unsupported bulk operation: {kind}")
                touched.add(field)
            changed = {
                field: working[field]
                for field in touched
                if working[field] != before.get(field)
                or target_override.get(field, False) != overrides.get(field, False)
            }
            if changed:
                plans.append({
                    "track_id": track_id,
                    "title": track.get("title"),
                    "before": {field: before.get(field) for field in changed},
                    "after": changed,
                    "override_active": {
                        field: bool(target_override.get(field, True)) for field in changed
                    },
                })
        return plans

    def bulk_metadata_preview(
        self,
        track_ids: Iterable[int],
        values: Mapping[str, Any] | None = None,
        *,
        operations: Iterable[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        plans = self._bulk_metadata_plan(track_ids, values=values, operations=operations)
        return {"count": len(plans), "items": plans}

    def bulk_update_metadata(
        self,
        track_ids: Iterable[int],
        values: Mapping[str, Any] | None = None,
        *,
        operations: Iterable[Mapping[str, Any]] | None = None,
        source: str = "user",
    ) -> dict[str, Any]:
        import uuid
        plans = self._bulk_metadata_plan(track_ids, values=values, operations=operations)
        op_id = uuid.uuid4().hex
        for plan in plans:
            active_values = {
                field: value
                for field, value in plan["after"].items()
                if plan["override_active"][field]
            }
            provider_values = {
                field: value
                for field, value in plan["after"].items()
                if not plan["override_active"][field]
            }
            if active_values:
                self.update_track_metadata(
                    int(plan["track_id"]), active_values, source=source,
                    activate_override=True, operation_id=op_id,
                )
            if provider_values:
                self.update_track_metadata(
                    int(plan["track_id"]), provider_values, source=source,
                    activate_override=False, operation_id=op_id,
                )
        return {
            "operation_id": op_id,
            "count": len(plans),
            "items": [self.get_track(int(item["track_id"])) for item in plans],
        }

    @staticmethod
    def _normalize_music_text(value: Any) -> str:
        """Normalize searchable music text without destroying its script.

        Older releases transliterated to ASCII and removed all bracketed text.
        That made CJK/Cyrillic titles disappear and discarded useful mix labels.
        Unicode case folding keeps the matcher lightweight while retaining the
        original language for evidence and aliases.
        """
        text = unicodedata.normalize("NFKC", str(value or "")).casefold()
        text = re.sub(r"\b(feat(?:uring)?|ft)\.?\b", " ", text)
        text = re.sub(r"\b(official|audio|video|lyrics?|hd|hq|mv)\b", " ", text)
        text = "".join(" " if unicodedata.category(char).startswith(("P", "S")) else char for char in text)
        return " ".join(text.split())

    @classmethod
    def _music_tokens(cls, value: Any) -> set[str]:
        """Return Unicode word tokens plus CJK character bigrams."""
        normalized = cls._normalize_music_text(value)
        tokens = set(re.findall(r"[^\W_]+", normalized, flags=re.UNICODE))
        compact = "".join(tokens for tokens in re.findall(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]+", normalized))
        for index in range(max(0, len(compact) - 1)):
            tokens.add(compact[index:index + 2])
        return {token for token in tokens if len(token) > 1 or token.isalnum()}

    @staticmethod
    def _relation_markers(title: str) -> set[str]:
        lowered = str(title or "").casefold()
        markers = (
            ("remix", "remix_of"), ("rework", "remix_of"), ("edit", "alternate_mix_of"),
            ("mix", "alternate_mix_of"), ("cover", "cover_of"), ("live", "live_version_of"),
            ("acoustic", "alternate_mix_of"), ("instrumental", "instrumental_version_of"),
            ("sped up", "alternate_mix_of"), ("slowed", "alternate_mix_of"),
            ("nightcore", "alternate_mix_of"), ("remaster", "alternate_mix_of"),
        )
        return {relation for marker, relation in markers if marker in lowered}

    @classmethod
    def _relation_marker(cls, title: str) -> str | None:
        lowered = str(title or "").casefold()
        priority = (
            ("remix", "remix_of"), ("rework", "remix_of"), ("cover", "cover_of"),
            ("live", "live_version_of"), ("acoustic", "alternate_mix_of"),
            ("instrumental", "instrumental_version_of"), ("sped up", "alternate_mix_of"),
            ("slowed", "alternate_mix_of"), ("nightcore", "alternate_mix_of"),
            ("remaster", "alternate_mix_of"), ("edit", "alternate_mix_of"), ("mix", "alternate_mix_of"),
        )
        for marker, relation in priority:
            if marker in lowered:
                return relation
        return None

    def save_track_fingerprint(
        self,
        track_id: int,
        *,
        algorithm: str,
        fingerprint: str,
        duration: float | None = None,
        source: str = "user_supplied",
    ) -> dict[str, Any]:
        """Store only an explicit fingerprint; imports never call this method."""
        self.get_track_base(track_id)
        normalized_algorithm = " ".join(str(algorithm or "").split()).strip().lower()
        normalized_fingerprint = str(fingerprint or "").strip()
        if not normalized_algorithm or not normalized_fingerprint:
            raise ValueError("Algorithm and fingerprint are required")
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO track_fingerprints(track_id, algorithm, fingerprint, duration, source, created_at)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(track_id, algorithm) DO UPDATE SET
                    fingerprint=excluded.fingerprint, duration=excluded.duration,
                    source=excluded.source, created_at=excluded.created_at
                """,
                (
                    track_id, normalized_algorithm, normalized_fingerprint,
                    float(duration) if duration is not None else None,
                    str(source or "user_supplied"), _utc_now(),
                ),
            )
            row = conn.execute(
                "SELECT * FROM track_fingerprints WHERE track_id = ? AND algorithm = ?",
                (track_id, normalized_algorithm),
            ).fetchone()
        assert row is not None
        return dict(row)

    def fingerprint_local_file(
        self, track_id: int, file_path: Path, *, fpcalc_path: str | None = None
    ) -> dict[str, Any]:
        """Run fpcalc only for a path explicitly supplied by the user."""
        path = file_path.expanduser().resolve()
        if not path.is_file():
            raise ValueError("The local audio file was not found")
        executable = fpcalc_path or shutil.which("fpcalc")
        if not executable:
            raise ValueError(
                "fpcalc is not installed. Install Chromaprint/AcoustID fpcalc, "
                "then retry with an explicit local audio file."
            )
        try:
            completed = subprocess.run(
                [str(executable), "-json", str(path)],
                capture_output=True,
                text=True,
                check=True,
                timeout=180,
            )
            payload = json.loads(completed.stdout)
        except subprocess.TimeoutExpired as exc:
            raise ValueError("fpcalc timed out while reading the local file") from exc
        except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            detail = getattr(exc, "stderr", "") or str(exc)
            raise ValueError(f"fpcalc could not fingerprint the local file: {detail}") from exc
        fingerprint = payload.get("fingerprint")
        if not fingerprint:
            raise ValueError("fpcalc returned no fingerprint")
        return self.save_track_fingerprint(
            track_id,
            algorithm="chromaprint",
            fingerprint=str(fingerprint),
            duration=float(payload["duration"]) if payload.get("duration") is not None else None,
            source=f"local_file:{path}",
        )

    def list_track_fingerprints(self, track_id: int | None = None) -> list[dict[str, Any]]:
        if track_id is None:
            return self._rows(
                "SELECT * FROM track_fingerprints ORDER BY algorithm, track_id"
            )
        self.get_track_base(track_id)
        return self._rows(
            "SELECT * FROM track_fingerprints WHERE track_id = ? ORDER BY algorithm",
            (track_id,),
        )

    @staticmethod
    def _fingerprint_similarity(left: str, right: str) -> float:
        if left == right:
            return 1.0
        # Raw integer fingerprints can be compared positionally. Encoded
        # Chromaprint strings remain exact-match only to avoid false claims.
        if re.fullmatch(r"[\d,\s-]+", left) and re.fullmatch(r"[\d,\s-]+", right):
            left_values = [value for value in re.split(r"\s*,\s*", left) if value]
            right_values = [value for value in re.split(r"\s*,\s*", right) if value]
            return SequenceMatcher(None, left_values, right_values).ratio()
        return 0.0

    def suggest_track_relations(self, *, limit: int = 500, min_confidence: float = 0.65) -> list[dict[str, Any]]:
        """Suggest explainable relations using indexed metadata candidates.

        Candidate blocking keeps this bounded for long libraries.  The matcher
        deliberately produces review suggestions only; it never merges tracks.
        """
        tracks = self.list_tracks(limit=50000)
        track_by_id = {int(track["id"]): track for track in tracks}
        fingerprints = self.list_track_fingerprints()
        fingerprint_groups: dict[str, list[dict[str, Any]]] = {}
        for fingerprint in fingerprints:
            fingerprint_groups.setdefault(str(fingerprint["algorithm"]), []).append(fingerprint)
        token_index: dict[str, set[int]] = {}
        for track in tracks:
            fields = [track.get("title"), track.get("artist"), track.get("creator"), track.get("uploader"), track.get("album")]
            aliases = track.get("aliases") or []
            if isinstance(aliases, str):
                aliases = [aliases]
            fields.extend(aliases)
            for token in set().union(*(self._music_tokens(field) for field in fields)):
                token_index.setdefault(token, set()).add(int(track["id"]))
        suggestions: list[dict[str, Any]] = []
        seen_pairs: set[tuple[int, int, str]] = set()
        for algorithm, members in fingerprint_groups.items():
            for index, left_fp in enumerate(members):
                for right_fp in members[index + 1:]:
                    left_id, right_id = int(left_fp["track_id"]), int(right_fp["track_id"])
                    if left_id == right_id or left_id not in track_by_id or right_id not in track_by_id:
                        continue
                    similarity = self._fingerprint_similarity(
                        str(left_fp["fingerprint"]), str(right_fp["fingerprint"])
                    )
                    if similarity < max(0.72, min_confidence):
                        continue
                    relation_type = "same_recording" if similarity >= 0.98 else "possible_cover"
                    pair = (min(left_id, right_id), max(left_id, right_id), relation_type)
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    suggestions.append({
                        "from_track_id": pair[0],
                        "to_track_id": pair[1],
                        "relation_type": relation_type,
                        "confidence": round(0.99 if similarity >= 0.98 else similarity, 3),
                        "evidence": {
                            "fingerprint_algorithm": algorithm,
                            "fingerprint_similarity": round(similarity, 4),
                            "local_opt_in": True,
                        },
                        "from": track_by_id[pair[0]],
                        "to": track_by_id[pair[1]],
                    })
                    if len(suggestions) >= max(1, min(limit, 2000)):
                        return suggestions
        def best_similarity(left_values: list[str], right_values: list[str]) -> float:
            pairs = ((left, right) for left in left_values if left for right in right_values if right)
            return max((SequenceMatcher(None, left, right).ratio() for left, right in pairs), default=0.0)

        for left in tracks:
            left_id = int(left["id"])
            left_title = self._normalize_music_text(left.get("title"))
            left_title_tokens = self._music_tokens(left.get("title"))
            left_aliases = left.get("aliases") or []
            if isinstance(left_aliases, str):
                left_aliases = [left_aliases]
            left_title_values = [left_title] + [self._normalize_music_text(value) for value in left_aliases]
            candidate_ids: set[int] = set()
            for token in left_title_tokens:
                members = token_index.get(token, set())
                if len(members) <= 250:
                    candidate_ids.update(members)
            for right_id in sorted(candidate_ids):
                if right_id <= left_id or right_id not in track_by_id:
                    continue
                right = track_by_id[right_id]
                right_title = self._normalize_music_text(right.get("title"))
                right_aliases = right.get("aliases") or []
                if isinstance(right_aliases, str):
                    right_aliases = [right_aliases]
                right_title_values = [right_title] + [self._normalize_music_text(value) for value in right_aliases]
                title_similarity = best_similarity(left_title_values, right_title_values)
                left_artist = self._normalize_music_text(left.get("artist") or left.get("creator") or left.get("uploader"))
                right_artist = self._normalize_music_text(right.get("artist") or right.get("creator") or right.get("uploader"))
                artist_similarity = SequenceMatcher(None, left_artist, right_artist).ratio() if left_artist and right_artist else 0.0
                artist_match = bool(left_artist and right_artist and left_artist == right_artist)
                album_similarity = SequenceMatcher(
                    None, self._normalize_music_text(left.get("album")), self._normalize_music_text(right.get("album"))
                ).ratio()
                duration_delta = None
                if left.get("duration") is not None and right.get("duration") is not None:
                    duration_delta = abs(float(left["duration"]) - float(right["duration"]))
                duration_score = 0.0 if duration_delta is None else max(0.0, 1.0 - min(duration_delta, 30.0) / 30.0)
                marker_left = self._relation_markers(str(left.get("title") or ""))
                marker_right = self._relation_markers(str(right.get("title") or ""))
                marker_overlap = bool(marker_left & marker_right)
                token_overlap = len(left_title_tokens & self._music_tokens(right.get("title"))) / max(1, len(left_title_tokens | self._music_tokens(right.get("title"))))
                confidence = (title_similarity * 0.52) + (artist_similarity * 0.18) + (album_similarity * 0.08) + (duration_score * 0.12) + (token_overlap * 0.10)
                if left.get("provider") != right.get("provider") and artist_match and duration_delta is not None and duration_delta <= 8:
                    confidence += 0.06
                if (marker_left or marker_right) and title_similarity >= 0.5 and token_overlap >= 0.2:
                    confidence = max(confidence, 0.72)
                confidence = min(0.99, confidence)
                if confidence < min_confidence:
                    continue
                relation_type = self._relation_marker(str(right.get("title") or "")) or self._relation_marker(str(left.get("title") or ""))
                if relation_type == "cover_of":
                    relation_type = "cover_of"
                elif relation_type:
                    relation_type = relation_type
                elif artist_match and title_similarity >= 0.9 and (duration_delta is None or duration_delta <= 8):
                    relation_type = "same_recording"
                elif title_similarity >= 0.82 and (duration_delta is None or duration_delta <= 12):
                    relation_type = "possible_duplicate"
                else:
                    relation_type = "related_title"
                pair = (left_id, right_id, relation_type)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                evidence = {
                    "normalized_title": left_title,
                    "title_similarity": round(title_similarity, 4),
                    "artist_similarity": round(artist_similarity, 4),
                    "artist_match": artist_match,
                    "album_similarity": round(album_similarity, 4),
                    "duration_delta": duration_delta,
                    "provider_match": left.get("provider") == right.get("provider"),
                    "version_markers": sorted(marker_left | marker_right),
                    "token_overlap": round(token_overlap, 4),
                    "explanation": f"title similarity {title_similarity:.2f}; artist {'matches' if artist_match else 'differs'}; "
                                   f"duration {duration_delta:.1f}s apart" if duration_delta is not None else
                                   f"title similarity {title_similarity:.2f}; artist {'matches' if artist_match else 'differs'}",
                }
                suggestions.append({
                    "from_track_id": left_id, "to_track_id": right_id, "relation_type": relation_type,
                    "confidence": round(confidence, 3), "evidence": evidence,
                    "from": left, "to": right,
                })
                if len(suggestions) >= max(1, min(limit, 2000)):
                    return suggestions
        return suggestions

    def relation_neighborhood(
        self, track_id: int, *, max_depth: int = 2, min_confidence: float = 0.65,
        include_proposed: bool = False, relation_types: Iterable[str] = (),
    ) -> dict[str, Any]:
        self.get_track_base(track_id)
        depth_limit = max(1, min(int(max_depth), 5))
        allowed_types = {str(value) for value in relation_types if str(value)}
        statuses = ("accepted", "proposed") if include_proposed else ("accepted",)
        nodes: dict[int, dict[str, Any]] = {track_id: self.get_track(track_id)}
        edges: list[dict[str, Any]] = []
        frontier = {track_id}
        seen_edges: set[int] = set()
        for depth in range(1, depth_limit + 1):
            if not frontier:
                break
            placeholders = ",".join("?" for _ in frontier)
            status_placeholders = ",".join("?" for _ in statuses)
            rows = self._rows(
                f"""SELECT * FROM track_relations
                    WHERE status IN ({status_placeholders}) AND confidence >= ?
                    AND (from_track_id IN ({placeholders}) OR to_track_id IN ({placeholders}))
                    ORDER BY confidence DESC, id""",
                (*statuses, float(min_confidence), *frontier, *frontier),
            )
            next_frontier: set[int] = set()
            for row in rows:
                if allowed_types and row["relation_type"] not in allowed_types:
                    continue
                relation_id = int(row["id"])
                if relation_id in seen_edges:
                    continue
                seen_edges.add(relation_id)
                left_id, right_id = int(row["from_track_id"]), int(row["to_track_id"])
                for node_id in (left_id, right_id):
                    if node_id not in nodes:
                        nodes[node_id] = self.get_track(node_id)
                        next_frontier.add(node_id)
                edge = dict(row)
                edge["evidence"] = json.loads(edge.pop("evidence_json") or "{}")
                edge["depth"] = depth
                edges.append(edge)
            frontier = next_frontier
        return {"root_track_id": track_id, "nodes": list(nodes.values()), "edges": edges}

    def save_relation_suggestions(self, suggestions: Iterable[Mapping[str, Any]], *, source: str = "local") -> int:
        created = 0
        with self.transaction() as conn:
            for suggestion in suggestions:
                try:
                    cur = conn.execute(
                        """INSERT INTO track_relations(from_track_id, to_track_id, relation_type, confidence, status, evidence_json, source, created_at)
                        VALUES(?, ?, ?, ?, 'proposed', ?, ?, ?)
                        ON CONFLICT(from_track_id, to_track_id, relation_type) DO UPDATE SET
                          confidence=excluded.confidence, evidence_json=excluded.evidence_json, source=excluded.source""",
                        (
                            int(suggestion["from_track_id"]), int(suggestion["to_track_id"]),
                            str(suggestion["relation_type"]), float(suggestion.get("confidence", 0)),
                            json.dumps(dict(suggestion.get("evidence") or {}), ensure_ascii=False), source, _utc_now(),
                        ),
                    )
                    created += max(0, cur.rowcount)
                except (KeyError, TypeError, ValueError, sqlite3.IntegrityError):
                    continue
        return created

    def list_relations(self, *, status: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        clause = "WHERE r.status = ?" if status else ""
        values: tuple[Any, ...] = (status, max(1, min(limit, 2000))) if status else (max(1, min(limit, 2000)),)
        rows = self._rows(
            f"""SELECT r.*, a.title AS from_title, b.title AS to_title,
                a.provider AS from_provider, b.provider AS to_provider
                FROM track_relations r
                JOIN tracks a ON a.id=r.from_track_id JOIN tracks b ON b.id=r.to_track_id
                {clause} ORDER BY r.confidence DESC, r.created_at DESC LIMIT ?""",
            values,
        )
        for row in rows:
            row["evidence"] = json.loads(row.pop("evidence_json") or "{}")
        return rows

    def review_relation(self, relation_id: int, *, status: str) -> dict[str, Any]:
        if status not in {"proposed", "accepted", "rejected", "ignored"}:
            raise ValueError("Invalid relation review status")
        with self.transaction() as conn:
            cur = conn.execute("UPDATE track_relations SET status=?, reviewed_at=? WHERE id=?", (status, _utc_now(), relation_id))
            if cur.rowcount != 1:
                raise ValueError("Relation was not found")
        return next(item for item in self.list_relations(limit=2000) if int(item["id"]) == relation_id)

    def list_review_items(self, *, status: str = "open", limit: int = 500) -> list[dict[str, Any]]:
        clause = "" if status in {"", "all", None} else "WHERE ri.status = ?"
        values: tuple[Any, ...] = (status, max(1, min(limit, 2000))) if clause else (max(1, min(limit, 2000)),)
        rows = self._rows(
            f"""SELECT ri.*, t.title, t.provider, t.uploader
                FROM review_items ri LEFT JOIN tracks t ON t.id=ri.track_id
                {clause} ORDER BY ri.created_at DESC LIMIT ?""",
            values,
        )
        for row in rows:
            row["payload"] = json.loads(row.pop("payload_json") or "{}")
        return rows

    def create_review_item(self, *, kind: str, track_id: int | None = None, relation_id: int | None = None, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        with self.transaction() as conn:
            if relation_id is not None:
                existing = conn.execute(
                    "SELECT * FROM review_items WHERE kind = ? AND relation_id = ? AND status = 'open' ORDER BY id DESC LIMIT 1",
                    (kind, relation_id),
                ).fetchone()
                if existing:
                    result = dict(existing)
                    result["payload"] = json.loads(result.pop("payload_json") or "{}")
                    return result
            cur = conn.execute(
                "INSERT INTO review_items(kind, track_id, relation_id, payload_json, created_at) VALUES(?, ?, ?, ?, ?)",
                (kind, track_id, relation_id, json.dumps(dict(payload or {}), ensure_ascii=False), _utc_now()),
            )
            row = conn.execute("SELECT * FROM review_items WHERE id=?", (cur.lastrowid,)).fetchone()
            return dict(row)

    def resolve_review_item(self, item_id: int, *, status: str = "resolved") -> dict[str, Any]:
        with self.transaction() as conn:
            cur = conn.execute("UPDATE review_items SET status=?, resolved_at=? WHERE id=?", (status, _utc_now(), item_id))
            if cur.rowcount != 1:
                raise ValueError("Review item was not found")
            row = conn.execute("SELECT * FROM review_items WHERE id=?", (item_id,)).fetchone()
            result = dict(row); result["payload"] = json.loads(result.pop("payload_json") or "{}"); return result

    def rebuild_music_entities(self) -> dict[str, int]:
        """Materialize artist/release groupings from effective track metadata."""
        created_artists = created_releases = linked_artists = linked_releases = 0
        tracks = self.list_tracks(limit=5000)
        with self.transaction() as conn:
            for track in tracks:
                artist_names = []
                if track.get("artist"): artist_names.extend([v.strip() for v in re.split(r",|;|&", str(track["artist"])) if v.strip()])
                if track.get("featured_artists"): artist_names.extend([v.strip() for v in re.split(r",|;|&", str(track["featured_artists"])) if v.strip()])
                for name in dict.fromkeys(artist_names):
                    normalized = self._normalize_music_text(name)
                    if not normalized: continue
                    cur = conn.execute("INSERT OR IGNORE INTO artists(name, normalized_name, created_at) VALUES(?, ?, ?)", (name, normalized, _utc_now()))
                    created_artists += max(0, cur.rowcount)
                    artist_id = conn.execute("SELECT id FROM artists WHERE normalized_name=?", (normalized,)).fetchone()["id"]
                    conn.execute("INSERT OR IGNORE INTO track_artists(track_id, artist_id, role) VALUES(?, ?, ?)", (track["id"], artist_id, "featured" if track.get("featured_artists") and name in str(track["featured_artists"]) else "primary"))
                    linked_artists += 1
                if track.get("album"):
                    title = str(track["album"]); normalized = self._normalize_music_text(title); release_date = track.get("release_date")
                    conn.execute("INSERT OR IGNORE INTO releases(title, normalized_title, release_date, album_artist, created_at) VALUES(?, ?, ?, ?, ?)", (title, normalized, release_date, track.get("album_artist"), _utc_now()))
                    release_id = conn.execute("SELECT id FROM releases WHERE normalized_title=? AND release_date IS ?", (normalized, release_date)).fetchone()["id"]
                    conn.execute("INSERT OR REPLACE INTO track_releases(track_id, release_id, disc_number, track_number) VALUES(?, ?, ?, ?)", (track["id"], release_id, track.get("disc_number"), track.get("track_number")))
                    created_releases += 1; linked_releases += 1
        return {"artists_created": created_artists, "artist_links": linked_artists, "releases_linked": linked_releases, "releases_seen": created_releases}

    def save_search(self, name: str, query: Mapping[str, Any]) -> dict[str, Any]:
        cleaned = " ".join(str(name or "").split()).strip()
        if not cleaned:
            raise ValueError("Search name cannot be empty")
        now = _utc_now()
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO saved_searches(name, query_json, created_at, updated_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET query_json=excluded.query_json, updated_at=excluded.updated_at""",
                (cleaned, json.dumps(dict(query), ensure_ascii=False), now, now),
            )
            row = conn.execute("SELECT * FROM saved_searches WHERE name=? COLLATE NOCASE", (cleaned,)).fetchone()
            result = dict(row); result["query"] = json.loads(result.pop("query_json")); return result

    def list_searches(self) -> list[dict[str, Any]]:
        rows = self._rows("SELECT * FROM saved_searches ORDER BY name COLLATE NOCASE")
        for row in rows:
            row["query"] = json.loads(row.pop("query_json") or "{}")
        return rows

    def delete_search(self, search_id: int) -> None:
        with self.transaction() as conn:
            if conn.execute("DELETE FROM saved_searches WHERE id=?", (search_id,)).rowcount != 1:
                raise ValueError("Saved search was not found")

    def list_jobs(self, *, limit: int = 100, status: str | None = None) -> list[dict[str, Any]]:
        clause = "WHERE status = ?" if status else ""
        rows = self._rows(f"SELECT * FROM jobs {clause} ORDER BY created_at DESC LIMIT ?", ((status, limit) if status else (limit,)))
        for row in rows:
            row["source_urls"] = json.loads(row.pop("source_urls_json") or "[]")
            row["options"] = json.loads(row.pop("options_json") or "{}")
            row["result"] = json.loads(row.pop("result_json") or "null")
        return rows

    def create_job(self, job_id: str, *, job_type: str, label: str = "", source_urls: Iterable[str] = (), options: Mapping[str, Any] | None = None, total_count: int = 0) -> dict[str, Any]:
        now = _utc_now()
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO jobs(id, job_type, status, label, source_urls_json, options_json, total_count, created_at, updated_at) VALUES(?, ?, 'queued', ?, ?, ?, ?, ?, ?)",
                (job_id, job_type, label, json.dumps(list(source_urls)), json.dumps(dict(options or {})), total_count, now, now),
            )
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            raise ValueError("Job was not found")
        result = dict(row)
        result["source_urls"] = json.loads(result.pop("source_urls_json") or "[]")
        result["options"] = json.loads(result.pop("options_json") or "{}")
        result["result"] = json.loads(result.pop("result_json") or "null")
        result["logs"] = self._rows("SELECT * FROM job_logs WHERE job_id = ? ORDER BY id", (job_id,))
        return result

    def update_job(self, job_id: str, **changes: Any) -> dict[str, Any]:
        self.get_job(job_id)
        allowed = {"status","current_source","current_item","completed_count","total_count","retry_count","next_retry_at","error_class","error","started_at","finished_at","label"}
        assignments=[]; values=[]
        for key, value in changes.items():
            if key in allowed:
                assignments.append(f"{key} = ?"); values.append(value)
        if "result" in changes:
            assignments.append("result_json = ?"); values.append(json.dumps(changes["result"], ensure_ascii=False))
        assignments.append("updated_at = ?"); values.append(_utc_now()); values.append(job_id)
        with self.transaction() as conn:
            conn.execute(f"UPDATE jobs SET {', '.join(assignments)} WHERE id = ?", values)
        return self.get_job(job_id)

    def add_job_log(self, job_id: str, message: str, *, level: str = "info") -> None:
        with self.transaction() as conn:
            conn.execute("INSERT INTO job_logs(job_id, level, message, created_at) VALUES(?, ?, ?, ?)", (job_id, level, message, _utc_now()))

    def delete_job(self, job_id: str) -> None:
        with self.transaction() as conn:
            if conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,)).rowcount == 0:
                raise ValueError("Job was not found")

    def create_discover_session(self, *, name: str, sources: Iterable[str], candidates: Iterable[Mapping[str, Any]] = (), page_size: int = 100, expires_at: str | None = None) -> dict[str, Any]:
        import uuid
        session_id = uuid.uuid4().hex
        bounded = max(1, min(int(page_size), 500))
        now = _utc_now()
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO discover_sessions(id, name, sources_json, candidates_json, seen_json, page_size, created_at, updated_at, expires_at) VALUES(?, ?, ?, ?, '[]', ?, ?, ?, ?)",
                (session_id, name, json.dumps(list(sources)), json.dumps(list(candidates), ensure_ascii=False), bounded, now, now, expires_at),
            )
        return self.get_discover_session(session_id)

    def get_discover_session(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM discover_sessions WHERE id = ?", (session_id,)).fetchone()
        if not row:
            raise ValueError("Discover session was not found")
        result = dict(row)
        result["sources"] = json.loads(result.pop("sources_json") or "[]")
        result["candidates"] = json.loads(result.pop("candidates_json") or "[]")
        result["seen"] = json.loads(result.pop("seen_json") or "[]")
        saved_ids = {(str(t["provider"]), str(t["remote_id"])) for t in self._rows("SELECT provider, remote_id FROM tracks")}
        original_candidates = list(result["candidates"])
        seen_keys = {str(v) for v in result["seen"]}
        result["candidates"] = [
            c for c in original_candidates
            if (str(c.get("provider")), str(c.get("remote_id"))) not in saved_ids
            and f"{c.get('provider')}:{c.get('remote_id')}" not in seen_keys
        ]
        result["counts"] = {
            "discovered": len(result["candidates"]),
            "already_saved": sum(1 for c in original_candidates if (str(c.get("provider")), str(c.get("remote_id"))) in saved_ids),
            "filtered": 0,
            "unavailable": sum(1 for c in result["candidates"] if c.get("availability") in {"unavailable", "private", "deleted"}),
            "failed": 0,
        }
        return result

    def update_discover_session(self, session_id: str, *, candidates: Iterable[Mapping[str, Any]], seen: Iterable[str] | None = None) -> dict[str, Any]:
        self.get_discover_session(session_id)
        with self.transaction() as conn:
            conn.execute(
                "UPDATE discover_sessions SET candidates_json = ?, seen_json = COALESCE(?, seen_json), updated_at = ? WHERE id = ?",
                (json.dumps(list(candidates), ensure_ascii=False), json.dumps(list(seen), ensure_ascii=False) if seen is not None else None, _utc_now(), session_id),
            )
        return self.get_discover_session(session_id)

    def delete_discover_session(self, session_id: str) -> None:
        with self.transaction() as conn:
            if conn.execute("DELETE FROM discover_sessions WHERE id = ?", (session_id,)).rowcount == 0:
                raise ValueError("Discover session was not found")

    def create_backup(self, *, kind: str = "manual") -> dict[str, Any]:
        exports = self.export_library()
        import uuid
        backup_id = f"{dt_stamp()}-{kind}-{uuid.uuid4().hex[:6]}"
        archive = self.data_dir / "backups"
        archive.mkdir(parents=True, exist_ok=True)
        path = archive / f"{backup_id}.zip"
        manifest = {
            "timestamp": _utc_now(), "schema_version": int(self._conn.execute("PRAGMA user_version").fetchone()[0]),
            "track_count": self.stats()["tracks"], "playlist_count": self.stats()["playlists"],
            "origin": kind,
        }
        db_copy = archive / f"{backup_id}.sqlite3"
        with self._lock:
            self._conn.commit()
            snapshot = sqlite3.connect(db_copy)
            try:
                self._conn.backup(snapshot)
            finally:
                snapshot.close()
        files = {
            "library.sqlite3": db_copy, "library.md": Path(exports["markdown"]),
            "library.txt": Path(exports["text"]), "library.json": Path(exports["json"]),
            "library.csv": Path(exports["csv"]), "library.m3u": Path(exports["m3u"]),
        }
        checksums = hashlib.sha256()
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, source in files.items():
                zf.write(source, arcname=name)
                checksums.update(source.read_bytes())
            manifest["checksum"] = checksums.hexdigest()
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        db_copy.unlink(missing_ok=True)
        with self.transaction() as conn:
            conn.execute("INSERT INTO backups(id, path, kind, manifest_json, created_at, checksum) VALUES(?, ?, ?, ?, ?, ?)", (backup_id, str(path), kind, json.dumps(manifest), manifest["timestamp"], manifest["checksum"]))
        return self.get_backup(backup_id)

    def get_backup(self, backup_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM backups WHERE id = ?", (backup_id,)).fetchone()
        if not row:
            raise ValueError("Backup was not found")
        result = dict(row); result["manifest"] = json.loads(result.pop("manifest_json")); return result

    def list_backups(self) -> list[dict[str, Any]]:
        return [self.get_backup(str(row["id"])) for row in self._rows("SELECT id FROM backups ORDER BY created_at DESC")]

    @staticmethod
    def _backup_datetime(backup: Mapping[str, Any]) -> datetime:
        value = str(backup.get("created_at") or backup.get("manifest", {}).get("timestamp") or "")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)

    def prune_backups(
        self,
        *,
        keep: int | None = None,
        daily: int = 7,
        weekly: int = 8,
        monthly: int = 12,
        long_term: int = 2,
    ) -> int:
        """Apply calendar-bucket retention, keeping one newest backup per bucket."""
        backups = self.list_backups()
        if keep is not None:
            # Compatibility with older callers while still retaining calendar
            # safety points instead of degrading to a flat newest-N policy.
            daily = max(daily, int(keep))
        limits = {
            "daily": max(0, int(daily)),
            "weekly": max(0, int(weekly)),
            "monthly": max(0, int(monthly)),
            "long_term": max(1, int(long_term)),
        }
        keep_ids: set[str] = set()
        buckets: dict[str, set[Any]] = {key: set() for key in limits}
        for backup in backups:
            if str(backup.get("kind") or "").lower() in {"pre-restore", "safety", "migration-safety"}:
                keep_ids.add(str(backup["id"]))
                continue
            created = self._backup_datetime(backup)
            iso = created.isocalendar()
            keys = {
                "daily": created.date().isoformat(),
                "weekly": (iso.year, iso.week),
                "monthly": (created.year, created.month),
                "long_term": (created.year, (created.month - 1) // 6),
            }
            for tier, bucket in keys.items():
                if len(buckets[tier]) < limits[tier] and bucket not in buckets[tier]:
                    buckets[tier].add(bucket)
                    keep_ids.add(str(backup["id"]))
        removed = 0
        for backup in backups:
            if str(backup["id"]) in keep_ids:
                continue
            Path(backup["path"]).unlink(missing_ok=True)
            with self.transaction() as conn:
                conn.execute("DELETE FROM backups WHERE id = ?", (backup["id"],))
            removed += 1
        return removed

    def validate_backup(self, backup_id: str) -> dict[str, Any]:
        backup = self.get_backup(backup_id); path = Path(backup["path"])
        if not path.exists(): return {"valid": False, "error": "Backup file is missing"}
        extracted: Path | None = None
        try:
            with zipfile.ZipFile(path) as zf:
                manifest = json.loads(zf.read("manifest.json"))
                valid = all(name in zf.namelist() for name in ("library.sqlite3", "manifest.json"))
                if valid and manifest.get("checksum"):
                    digest = hashlib.sha256()
                    for name in ("library.sqlite3", "library.md", "library.txt", "library.json", "library.csv", "library.m3u"):
                        if name in zf.namelist():
                            digest.update(zf.read(name))
                    valid = digest.hexdigest() == manifest["checksum"]
                if valid:
                    with tempfile.NamedTemporaryFile(
                        prefix="backup-validation-", suffix=".sqlite3",
                        dir=self.data_dir, delete=False,
                    ) as temporary:
                        extracted = Path(temporary.name)
                        temporary.write(zf.read("library.sqlite3"))
                    inspection = sqlite3.connect(extracted)
                    try:
                        integrity = str(inspection.execute("PRAGMA integrity_check").fetchone()[0])
                        foreign_key_errors = inspection.execute("PRAGMA foreign_key_check").fetchall()
                        valid = integrity.lower() == "ok" and not foreign_key_errors
                        if not valid:
                            return {
                                "valid": False,
                                "error": (
                                    f"Database integrity: {integrity}; "
                                    f"foreign-key errors: {len(foreign_key_errors)}"
                                ),
                                "manifest": manifest,
                            }
                    finally:
                        inspection.close()
            return {"valid": valid, "manifest": manifest}
        except Exception as exc:
            return {"valid": False, "error": str(exc)}
        finally:
            if extracted is not None:
                extracted.unlink(missing_ok=True)

    def restore_backup(self, backup_id: str, *, target_dir: Path | None = None) -> dict[str, Any]:
        check = self.validate_backup(backup_id)
        if not check.get("valid"):
            raise ValueError(check.get("error", "Backup is invalid"))
        backup = self.get_backup(backup_id)
        target = (target_dir or self.data_dir).expanduser().resolve()
        target.mkdir(parents=True, exist_ok=True)
        destination = target / "library.sqlite3"
        with tempfile.NamedTemporaryFile(
            prefix="library.restore-", suffix=".sqlite3", dir=target, delete=False
        ) as temporary:
            extracted = Path(temporary.name)
            with zipfile.ZipFile(backup["path"]) as zf:
                temporary.write(zf.read("library.sqlite3"))
        validation = sqlite3.connect(extracted)
        try:
            integrity = str(validation.execute("PRAGMA integrity_check").fetchone()[0])
            foreign_key_errors = validation.execute("PRAGMA foreign_key_check").fetchall()
            if integrity.lower() != "ok":
                raise ValueError(f"Backup database integrity check failed: {integrity}")
            if foreign_key_errors:
                raise ValueError(f"Backup database has {len(foreign_key_errors)} foreign-key errors")
        finally:
            validation.close()

        if destination.resolve() != self.path.resolve():
            rollback = target / "library.sqlite3.pre-restore"
            if destination.exists():
                rollback_tmp = target / "library.sqlite3.pre-restore.tmp"
                shutil.copy2(destination, rollback_tmp)
                os.replace(rollback_tmp, rollback)
            os.replace(extracted, destination)
            return {
                "restored": True,
                "rollback_path": str(rollback) if rollback.exists() else None,
                "backup_id": backup_id,
                "target": str(destination),
            }

        rollback = self.data_dir / "library.sqlite3.pre-restore"
        rollback_tmp = self.data_dir / "library.sqlite3.pre-restore.tmp"
        with self._lock:
            try:
                self._conn.commit()
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                rollback_connection = sqlite3.connect(rollback_tmp)
                try:
                    self._conn.backup(rollback_connection)
                finally:
                    rollback_connection.close()
                os.replace(rollback_tmp, rollback)
                self._conn.close()
                for suffix in ("-wal", "-shm"):
                    Path(str(self.path) + suffix).unlink(missing_ok=True)
                os.replace(extracted, self.path)
                self._conn = self._open_connection()
                self._init_schema()
            except Exception:
                with suppress(Exception):
                    self._conn.close()
                recovery_tmp = self.data_dir / "library.sqlite3.restore-rollback.tmp"
                if rollback.exists():
                    shutil.copy2(rollback, recovery_tmp)
                    os.replace(recovery_tmp, self.path)
                self._conn = self._open_connection()
                self._init_schema()
                raise
            finally:
                extracted.unlink(missing_ok=True)
                rollback_tmp.unlink(missing_ok=True)
        return {
            "restored": True,
            "rollback_path": str(rollback),
            "backup_id": backup_id,
            "target": str(self.path),
        }

    def rollback_restore(self) -> dict[str, Any]:
        """Restore the single explicit pre-restore copy, then consume it."""
        rollback = self.data_dir / "library.sqlite3.pre-restore"
        if not rollback.is_file():
            raise ValueError("No pre-restore rollback copy is available")
        validation = sqlite3.connect(rollback)
        try:
            if str(validation.execute("PRAGMA integrity_check").fetchone()[0]).lower() != "ok":
                raise ValueError("The pre-restore rollback copy failed integrity validation")
        finally:
            validation.close()
        with self._lock:
            replacement = self.data_dir / "library.sqlite3.rollback.tmp"
            try:
                self._conn.commit()
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self._conn.close()
                for suffix in ("-wal", "-shm"):
                    Path(str(self.path) + suffix).unlink(missing_ok=True)
                shutil.copy2(rollback, replacement)
                os.replace(replacement, self.path)
                self._conn = self._open_connection()
                self._init_schema()
                rollback.unlink(missing_ok=True)
            except Exception:
                replacement.unlink(missing_ok=True)
                with suppress(Exception):
                    self._conn.close()
                if rollback.exists():
                    shutil.copy2(rollback, replacement)
                    os.replace(replacement, self.path)
                self._conn = self._open_connection()
                self._init_schema()
                raise
        return {"rolled_back": True, "rollback_path": str(rollback), "target": str(self.path)}

    def remove_membership(self, membership_id: int) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE playlist_tracks
                SET deleted_at = ?, deleted_position = position
                WHERE id = ? AND deleted_at IS NULL
                """,
                (_utc_now(), membership_id),
            )

    def remove_memberships_for_tracks(self, playlist_id: int, track_ids: Iterable[int]) -> int:
        """Soft-remove all selected track memberships from one playlist."""
        self.get_playlist(playlist_id)
        ids = list(dict.fromkeys(int(track_id) for track_id in track_ids))
        if not ids:
            return 0
        placeholders = ", ".join("?" for _ in ids)
        with self.transaction() as conn:
            cursor = conn.execute(
                f"""
                UPDATE playlist_tracks
                SET deleted_at = ?, deleted_position = position
                WHERE playlist_id = ?
                  AND deleted_at IS NULL
                  AND track_id IN ({placeholders})
                """,
                (_utc_now(), playlist_id, *ids),
            )
            return int(cursor.rowcount)

    def restore_membership(self, membership_id: int) -> None:
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM playlist_tracks WHERE id = ?", (membership_id,)).fetchone()
            if not row:
                raise ValueError("Playlist entry was not found")
            if row["deleted_at"] is None:
                return
            # Restore at the remembered position and leave room for it. This
            # makes Undo a real undo instead of silently appending the song.
            position = int(row["deleted_position"] if row["deleted_position"] is not None else row["position"])
            conn.execute(
                """
                UPDATE playlist_tracks SET position = position + 1
                WHERE playlist_id = ? AND deleted_at IS NULL AND position >= ?
                """,
                (row["playlist_id"], position),
            )
            conn.execute(
                "UPDATE playlist_tracks SET deleted_at = NULL, position = ?, deleted_position = NULL WHERE id = ?",
                (position, membership_id),
            )

    def hide_track(self, track_id: int, *, hidden: bool) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE tracks SET hidden_at = ? WHERE id = ?", (_utc_now() if hidden else None, track_id))

    def rate_track(self, track_id: int, value: float | None) -> dict[str, Any]:
        self.get_track(track_id)
        if value is None:
            with self.transaction() as conn:
                conn.execute("DELETE FROM ratings WHERE track_id = ?", (track_id,))
            return self.get_track(track_id)
        if not math.isfinite(value) or not 0 <= value <= 10:
            raise ValueError("Rating must be between 0 and 10")
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO ratings(track_id, value, updated_at) VALUES(?, ?, ?)
                ON CONFLICT(track_id) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (track_id, value, _utc_now()),
            )
            row = conn.execute(
                "SELECT t.*, r.value AS rating FROM tracks t JOIN ratings r ON r.track_id = t.id WHERE t.id = ?",
                (track_id,),
            ).fetchone()
            return dict(row)

    def list_trash(self, *, limit: int = 500) -> dict[str, list[dict[str, Any]]]:
        limit = max(1, min(limit, 2000))
        memberships = self._rows(
            """
            SELECT pt.id AS membership_id, pt.position, pt.deleted_position, pt.deleted_at,
                   p.name AS playlist_name, t.*, r.value AS rating
            FROM playlist_tracks pt
            JOIN playlists p ON p.id = pt.playlist_id
            JOIN tracks t ON t.id = pt.track_id
            LEFT JOIN ratings r ON r.track_id = t.id
            WHERE pt.deleted_at IS NOT NULL
            ORDER BY pt.deleted_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        hidden = self._rows(
            """
            SELECT t.*, r.value AS rating
            FROM tracks t LEFT JOIN ratings r ON r.track_id = t.id
            WHERE t.hidden_at IS NOT NULL
            ORDER BY t.hidden_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return {"memberships": memberships, "hidden_tracks": hidden}

    def possible_duplicates(self, *, limit: int = 50) -> list[dict[str, Any]]:
        tracks = self._rows(
            """
            SELECT t.*, r.value AS rating FROM tracks t
            LEFT JOIN ratings r ON r.track_id = t.id
            WHERE t.hidden_at IS NULL
            ORDER BY lower(t.title), lower(COALESCE(t.creator, '')), t.provider
            """
        )
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for track in tracks:
            key = (" ".join(track["title"].casefold().split()), " ".join((track["creator"] or "").casefold().split()))
            grouped.setdefault(key, []).append(track)
        results: list[dict[str, Any]] = []
        for items in grouped.values():
            if len({item["provider"] for item in items}) > 1:
                results.append({"key": items[0]["title"], "tracks": items})
                if len(results) >= limit:
                    break
        return results

    def delete_pool(self, pool_id: int) -> None:
        with self.transaction() as conn:
            cursor = conn.execute("DELETE FROM pools WHERE id = ?", (pool_id,))
            if cursor.rowcount != 1:
                raise ValueError("Pool was not found")

    def save_pool(self, name: str, selection: Mapping[str, Any]) -> dict[str, Any]:
        cleaned = " ".join(name.split()).strip()
        if not cleaned:
            raise ValueError("Pool name cannot be empty")
        now = _utc_now()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO pools(name, selection_json, created_at, updated_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET selection_json = excluded.selection_json, updated_at = excluded.updated_at
                """,
                (cleaned, json.dumps(dict(selection)), now, now),
            )
            return dict(conn.execute("SELECT * FROM pools WHERE name = ? COLLATE NOCASE", (cleaned,)).fetchone())

    def list_pools(self) -> list[dict[str, Any]]:
        rows = self._rows("SELECT * FROM pools ORDER BY name COLLATE NOCASE")
        for row in rows:
            row["selection"] = json.loads(row.pop("selection_json"))
        return rows

    def _pool_tracks(self, selection: Mapping[str, Any]) -> list[dict[str, Any]]:
        playlist_ids = [int(value) for value in selection.get("playlist_ids", [])]
        chosen_track_ids = [int(value) for value in selection.get("track_ids", [])]
        include_repeats = bool(selection.get("include_repeats", False))
        provider = str(selection.get("provider") or "").strip() or None
        creator = str(selection.get("creator") or "").strip() or None
        min_rating = selection.get("min_rating")
        values: list[Any] = []
        clauses = ["t.hidden_at IS NULL"]
        source_order = "t.id"

        if playlist_ids and chosen_track_ids:
            playlist_placeholders = ",".join("?" for _ in playlist_ids)
            track_placeholders = ",".join("?" for _ in chosen_track_ids)
            # Restrict the LEFT JOIN itself to selected playlists. Otherwise an
            # explicitly selected track that belongs to other playlists would
            # be emitted once per unrelated membership.
            join = (
                "LEFT JOIN playlist_tracks pt ON pt.track_id = t.id "
                f"AND pt.deleted_at IS NULL AND pt.playlist_id IN ({playlist_placeholders})"
            )
            values.extend(playlist_ids)
            clauses.append(f"(t.id IN ({track_placeholders}) OR pt.playlist_id IS NOT NULL)")
            values.extend(chosen_track_ids)
            source_order = "CASE WHEN pt.playlist_id IS NULL THEN 1 ELSE 0 END, pt.playlist_id, pt.position, t.id"
        elif playlist_ids:
            placeholders = ",".join("?" for _ in playlist_ids)
            clauses.append(f"pt.playlist_id IN ({placeholders})")
            values.extend(playlist_ids)
            source_order = "pt.playlist_id, pt.position, pt.id"
            join = "JOIN playlist_tracks pt ON pt.track_id = t.id AND pt.deleted_at IS NULL"
        else:
            join = ""
        if chosen_track_ids and not playlist_ids:
            placeholders = ",".join("?" for _ in chosen_track_ids)
            clauses.append(f"t.id IN ({placeholders})")
            values.extend(chosen_track_ids)
        if provider:
            clauses.append("t.provider = ?")
            values.append(provider)
        if creator:
            clauses.append("lower(COALESCE(t.creator, '')) LIKE ?")
            values.append(f"%{creator.casefold()}%")
        if min_rating is not None:
            clauses.append("COALESCE(r.value, -1) >= ?")
            values.append(float(min_rating))
        tracks = self._rows(
            f"""
            SELECT t.*, r.value AS rating
            FROM tracks t
            {join}
            LEFT JOIN ratings r ON r.track_id = t.id
            WHERE {' AND '.join(clauses)}
            ORDER BY {source_order}
            """,
            values,
        )
        return unique_tracks(tracks, include_repeats=include_repeats)

    def pool_tracks(self, selection: Mapping[str, Any]) -> list[dict[str, Any]]:
        return self._pool_tracks(selection)

    @staticmethod
    def _rule_value(track: Mapping[str, Any], field: str) -> Any:
        value = track.get(field)
        if field == "uploader" and not value:
            value = track.get("creator")
        return value

    def _matches_rule(self, track: Mapping[str, Any], rule: Mapping[str, Any]) -> bool:
        if "all" in rule:
            return all(self._matches_rule(track, child) for child in (rule.get("all") or []))
        if "any" in rule:
            return any(self._matches_rule(track, child) for child in (rule.get("any") or []))
        if "none" in rule:
            return not any(self._matches_rule(track, child) for child in (rule.get("none") or []))
        field = str(rule.get("field") or "")
        op = str(rule.get("op") or "equals").casefold()
        actual = self._rule_value(track, field)
        expected = rule.get("value")
        if op == "exists":
            return (actual not in (None, "", [], {})) == bool(expected if expected is not None else True)
        if actual is None:
            return op in {"not_equals", "not_contains"}
        if isinstance(actual, (list, tuple, set)):
            actual_text = " ".join(str(v) for v in actual)
        else:
            actual_text = str(actual)
        left = actual_text.casefold()
        right = str(expected if expected is not None else "").casefold()
        if op in {"equals", "eq"}: return left == right
        if op in {"not_equals", "neq"}: return left != right
        if op == "contains": return right in left
        if op == "not_contains": return right not in left
        if op == "starts_with": return left.startswith(right)
        if op == "ends_with": return left.endswith(right)
        if op in {"in", "one_of"}: return left in {str(v).casefold() for v in (expected or [])}
        if op == "regex":
            try: return bool(re.search(str(expected or "")[:500], actual_text, re.IGNORECASE))
            except re.error: return False
        try:
            numeric_actual = float(actual)
            numeric_expected = float(expected)
            if op in {"gte", "ge"}: return numeric_actual >= numeric_expected
            if op in {"lte", "le"}: return numeric_actual <= numeric_expected
            if op == "gt": return numeric_actual > numeric_expected
            if op == "lt": return numeric_actual < numeric_expected
        except (TypeError, ValueError):
            pass
        return False

    def smart_playlist_tracks(self, definition: Mapping[str, Any]) -> list[dict[str, Any]]:
        # New rule DSL: {"rules": {"all": [...]}}.  Existing saved pool
        # selections remain supported for backwards compatibility.
        relation = definition.get("relation") if isinstance(definition, Mapping) else None
        if isinstance(relation, Mapping):
            result = self.relation_neighborhood(
                int(relation.get("root_track_id")),
                max_depth=int(relation.get("max_depth", 2)),
                min_confidence=float(relation.get("min_confidence", 0.65)),
                include_proposed=bool(relation.get("include_proposed", False)),
                relation_types=relation.get("relation_types") or (),
            )
            return [track for track in result["nodes"] if int(track["id"]) != int(result["root_track_id"])]
        rules = definition.get("rules") if isinstance(definition, Mapping) else None
        if rules is None:
            return self._pool_tracks(definition)
        tracks = self.list_tracks(limit=5000)
        return [track for track in tracks if self._matches_rule(track, rules)]

    def snapshot_playlist(self, playlist_id: int, *, name: str | None = None) -> dict[str, Any]:
        playlist = self.get_playlist(playlist_id)
        tracks = [int(item["track_id"]) for item in self._rows(
            "SELECT track_id FROM playlist_tracks WHERE playlist_id=? AND deleted_at IS NULL ORDER BY position, id", (playlist_id,)
        )]
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO playlist_snapshots(playlist_id, name, tracks_json, created_at) VALUES(?, ?, ?, ?)",
                (playlist_id, name or playlist["name"], json.dumps(tracks), _utc_now()),
            )
            row = conn.execute("SELECT * FROM playlist_snapshots WHERE id=?", (cur.lastrowid,)).fetchone()
            result = dict(row); result["tracks"] = tracks; return result

    def list_playlist_snapshots(self, playlist_id: int) -> list[dict[str, Any]]:
        rows = self._rows("SELECT * FROM playlist_snapshots WHERE playlist_id=? ORDER BY created_at DESC", (playlist_id,))
        for row in rows: row["tracks"] = json.loads(row.pop("tracks_json") or "[]")
        return rows

    def restore_playlist_snapshot(self, snapshot_id: int) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM playlist_snapshots WHERE id=?", (snapshot_id,)).fetchone()
        if not row: raise ValueError("Playlist snapshot was not found")
        playlist_id = int(row["playlist_id"]); track_ids = [int(v) for v in json.loads(row["tracks_json"] or "[]")]
        with self.transaction() as conn:
            conn.execute("UPDATE playlist_tracks SET deleted_at=?, deleted_position=position WHERE playlist_id=? AND deleted_at IS NULL", (_utc_now(), playlist_id))
            for position, track_id in enumerate(track_ids):
                conn.execute("INSERT INTO playlist_tracks(playlist_id, track_id, position, duplicate_copy) VALUES(?, ?, ?, 0)", (playlist_id, track_id, position))
        return self.get_playlist(playlist_id)

    def sort_playlist(self, playlist_id: int, *, field: str = "title", reverse: bool = False, snapshot: bool = True) -> dict[str, Any]:
        if snapshot:
            self.snapshot_playlist(playlist_id, name="Before sort")
        items = self.playlist_tracks(playlist_id)
        allowed = {"title", "uploader", "artist", "album", "genre", "duration", "view_count", "rating", "provider"}
        if field not in allowed: raise ValueError("Unsupported playlist sort field")
        items.sort(key=lambda item: (str(item.get(field) if item.get(field) is not None else "").casefold()), reverse=reverse)
        with self.transaction() as conn:
            for position, item in enumerate(items):
                conn.execute("UPDATE playlist_tracks SET position=? WHERE id=?", (position, item["membership_id"]))
        return self.get_playlist(playlist_id)

    def playlist_set_operation(self, playlist_ids: Iterable[int], *, operation: str, name: str) -> dict[str, Any]:
        ids = list(dict.fromkeys(int(v) for v in playlist_ids))
        if not ids: raise ValueError("Choose at least one playlist")
        ordered: list[int] = []
        members: list[set[int]] = []
        for playlist_id in ids:
            rows = self._rows("SELECT track_id FROM playlist_tracks WHERE playlist_id=? AND deleted_at IS NULL ORDER BY position, id", (playlist_id,))
            values = [int(row["track_id"]) for row in rows]; ordered.extend(values); members.append(set(values))
        if operation == "union": result_ids = list(dict.fromkeys(ordered))
        elif operation == "intersection": result_ids = [track_id for track_id in dict.fromkeys(ordered) if all(track_id in group for group in members)]
        elif operation in {"difference", "subtract"}:
            first_order = [int(row["track_id"]) for row in self._rows("SELECT track_id FROM playlist_tracks WHERE playlist_id=? AND deleted_at IS NULL ORDER BY position, id", (ids[0],))]
            result_ids = [track_id for track_id in first_order if all(track_id not in group for group in members[1:])]
        elif operation in {"symmetric_difference", "xor"}:
            counts: dict[int, int] = {}
            for group in members:
                for track_id in group: counts[track_id] = counts.get(track_id, 0) + 1
            result_ids = [track_id for track_id in dict.fromkeys(ordered) if counts.get(track_id) == 1]
        else: raise ValueError("Unsupported playlist set operation")
        playlist = self.create_playlist(name)
        with self.transaction() as conn:
            conn.executemany("INSERT INTO playlist_tracks(playlist_id, track_id, position, duplicate_copy) VALUES(?, ?, ?, 0)", [(playlist["id"], track_id, position) for position, track_id in enumerate(result_ids)])
        return self.get_playlist(int(playlist["id"]))

    def merge_playlists(self, playlist_ids: list[int], name: str) -> dict[str, Any]:
        if not playlist_ids:
            raise ValueError("Choose at least one playlist to merge")
        cleaned = " ".join(name.split()).strip()
        if not cleaned:
            raise ValueError("Playlist name cannot be empty")
        unique_source_ids = list(dict.fromkeys(int(value) for value in playlist_ids))
        with self.transaction() as conn:
            available = {
                int(row["id"])
                for row in conn.execute(
                    f"SELECT id FROM playlists WHERE id IN ({','.join('?' for _ in unique_source_ids)})",
                    unique_source_ids,
                ).fetchall()
            }
            missing = [playlist_id for playlist_id in unique_source_ids if playlist_id not in available]
            if missing:
                raise ValueError(f"Playlist {missing[0]} was not found")
            try:
                cursor = conn.execute(
                    "INSERT INTO playlists(name, kind, query_json, created_at) VALUES(?, 'manual', '{}', ?)",
                    (cleaned, _utc_now()),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"A playlist named {cleaned!r} already exists") from exc
            merged_id = int(cursor.lastrowid)
            position = 0
            seen: set[int] = set()
            for playlist_id in unique_source_ids:
                rows = conn.execute(
                    """
                    SELECT track_id FROM playlist_tracks
                    WHERE playlist_id = ? AND deleted_at IS NULL
                    ORDER BY position, id
                    """,
                    (playlist_id,),
                ).fetchall()
                for row in rows:
                    track_id = int(row["track_id"])
                    if track_id in seen:
                        continue
                    seen.add(track_id)
                    conn.execute(
                        """
                        INSERT INTO playlist_tracks(playlist_id, track_id, position, duplicate_copy)
                        VALUES(?, ?, ?, 0)
                        """,
                        (merged_id, track_id, position),
                    )
                    position += 1
            row = conn.execute("SELECT * FROM playlists WHERE id = ?", (merged_id,)).fetchone()
            return dict(row)

    def set_queue(self, tracks: Iterable[Mapping[str, Any]], *, mode: str, include_repeats: bool) -> dict[str, Any]:
        track_ids = [int(track["id"]) for track in tracks]
        with self.transaction() as conn:
            conn.execute("DELETE FROM queue_items")
            conn.executemany(
                "INSERT INTO queue_items(position, track_id) VALUES(?, ?)",
                list(enumerate(track_ids)),
            )
            conn.execute(
                """
                UPDATE playback_state
                SET current_position = ?, mode = ?, include_repeats = ?, updated_at = ?
                WHERE id = 1
                """,
                (0 if track_ids else -1, mode, int(include_repeats), _utc_now()),
            )
        return self.queue_state()

    def clear_queue(self) -> dict[str, Any]:
        """Discard only the transient queue; tracks and history remain intact."""
        return self.set_queue((), mode="true", include_repeats=False)

    def reorder_queue(self, positions: list[int]) -> dict[str, Any]:
        with self.transaction() as conn:
            rows = conn.execute("SELECT track_id FROM queue_items ORDER BY position").fetchall()
            ids = [int(rows[p]["track_id"]) for p in positions if 0 <= int(p) < len(rows)]
            if len(ids) != len(rows):
                ids.extend(int(row["track_id"]) for index, row in enumerate(rows) if index not in positions)
            conn.execute("DELETE FROM queue_items")
            conn.executemany("INSERT INTO queue_items(position, track_id) VALUES(?, ?)", list(enumerate(ids)))
            state = conn.execute("SELECT current_position FROM playback_state WHERE id=1").fetchone()
            current = min(max(int(state["current_position"]), 0), len(ids)-1) if ids else -1
            conn.execute("UPDATE playback_state SET current_position=?, updated_at=? WHERE id=1", (current, _utc_now()))
        return self.queue_state()

    def remove_queue_item(self, position: int) -> dict[str, Any]:
        with self.transaction() as conn:
            conn.execute("DELETE FROM queue_items WHERE position = ?", (int(position),))
            rows = conn.execute("SELECT track_id FROM queue_items ORDER BY position").fetchall()
            conn.execute("DELETE FROM queue_items")
            conn.executemany("INSERT INTO queue_items(position, track_id) VALUES(?, ?)", list(enumerate([int(r["track_id"]) for r in rows])))
            state = int(conn.execute("SELECT current_position FROM playback_state WHERE id=1").fetchone()["current_position"])
            conn.execute("UPDATE playback_state SET current_position=?, updated_at=? WHERE id=1", (min(state, len(rows)-1) if rows else -1, _utc_now()))
        return self.queue_state()

    def queue_state(self, *, preview_limit: int | None = None, preview_offset: int = 0) -> dict[str, Any]:
        """Return current item plus an optionally bounded queue preview.

        The queue itself remains durable in SQLite. Dashboard and WebSocket
        callers use a preview so a large imported library does not repeatedly
        serialize every queued song.
        """
        bounded_offset = max(0, preview_offset)
        limit_clause = ""
        values: list[Any] = []
        if preview_limit is not None:
            bounded_limit = max(1, min(preview_limit, 500))
            limit_clause = "LIMIT ? OFFSET ?"
            values.extend((bounded_limit, bounded_offset))
        rows = self._rows(
            """
            SELECT qi.position, t.*, r.value AS rating
            FROM queue_items qi
            JOIN tracks t ON t.id = qi.track_id
            LEFT JOIN ratings r ON r.track_id = t.id
            ORDER BY qi.position
            """ + limit_clause,
            values,
        )
        with self._lock:
            state = dict(self._conn.execute("SELECT * FROM playback_state WHERE id = 1").fetchone())
            total = int(self._conn.execute("SELECT COUNT(*) FROM queue_items").fetchone()[0])
        current_position = int(state["current_position"])
        current = next((track for track in rows if track["position"] == current_position), None)
        if current is None and current_position >= 0:
            current_rows = self._rows(
                """
                SELECT qi.position, t.*, r.value AS rating
                FROM queue_items qi
                JOIN tracks t ON t.id = qi.track_id
                LEFT JOIN ratings r ON r.track_id = t.id
                WHERE qi.position = ?
                """,
                (current_position,),
            )
            current = current_rows[0] if current_rows else None
        return {
            "items": rows,
            "total": total,
            "offset": bounded_offset,
            "current_position": current_position,
            "current": current,
            "mode": state["mode"],
            "include_repeats": bool(state["include_repeats"]),
        }

    def move_queue(self, delta: int, *, reason: str) -> dict[str, Any]:
        with self.transaction() as conn:
            state = conn.execute("SELECT current_position FROM playback_state WHERE id = 1").fetchone()
            total = int(conn.execute("SELECT COUNT(*) FROM queue_items").fetchone()[0])
            if not total:
                return self.queue_state()
            current_position = int(state["current_position"])
            position = min(max(current_position + delta, 0), total - 1)
            current_track = conn.execute(
                "SELECT track_id FROM queue_items WHERE position = ?",
                (current_position,),
            ).fetchone()
            conn.execute(
                "UPDATE playback_state SET current_position = ?, updated_at = ? WHERE id = 1",
                (position, _utc_now()),
            )
            if current_track and position != current_position:
                conn.execute(
                    "INSERT INTO playback_history(track_id, played_at, reason) VALUES(?, ?, ?)",
                    (current_track["track_id"], _utc_now(), reason),
                )
        return self.queue_state()

    def record_current_play(self, *, reason: str = "play") -> None:
        with self.transaction() as conn:
            row = conn.execute(
                """SELECT qi.track_id FROM playback_state ps
                JOIN queue_items qi ON qi.position=ps.current_position WHERE ps.id=1"""
            ).fetchone()
            if row:
                conn.execute(
                    "INSERT INTO playback_history(track_id, played_at, reason) VALUES(?, ?, ?)",
                    (row["track_id"], _utc_now(), reason),
                )

    def list_playback_history(self, *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        bounded_limit = max(1, min(limit, 1000))
        bounded_offset = max(0, offset)
        items = self._rows(
            """
            SELECT h.*, t.title, t.creator, t.provider, t.url
            FROM playback_history h
            JOIN tracks t ON t.id = h.track_id
            ORDER BY h.played_at DESC, h.id DESC
            LIMIT ? OFFSET ?
            """,
            (bounded_limit, bounded_offset),
        )
        with self._lock:
            total = int(self._conn.execute("SELECT COUNT(*) FROM playback_history").fetchone()[0])
        return {"items": items, "total": total, "limit": bounded_limit, "offset": bounded_offset}

    def update_extension_session(
        self,
        *,
        profile: str,
        tab_id: int | None,
        status: str,
        url: str | None,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO extension_sessions(profile, tab_id, url, status, updated_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(profile) DO UPDATE SET
                    tab_id = excluded.tab_id,
                    url = excluded.url,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (profile, tab_id, url, status, _utc_now()),
            )

    def extension_sessions(self) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT * FROM extension_sessions ORDER BY updated_at DESC"
        )

    def export_library(self) -> dict[str, str]:
        playlists = self.list_playlists(include_archived=True)
        markdown: list[str] = ["# Local Music Library", ""]
        plain: list[str] = ["Local Music Library", ""]
        for playlist in playlists:
            status = " (archived)" if playlist["archived_at"] else ""
            markdown.append(f"## {playlist['name']}{status}")
            plain.extend((f"[{playlist['name']}{status}]",))
            for item in self.playlist_tracks(int(playlist["id"]), include_deleted=True):
                flags = []
                if item["deleted_at"]:
                    flags.append("removed")
                if item["hidden_at"]:
                    flags.append("hidden")
                if item["rating"] is not None:
                    flags.append(f"rating {item['rating']:g}/10")
                if item["view_count"] is not None:
                    flags.append(f"{item['view_count']:,} views")
                suffix = f" — {', '.join(flags)}" if flags else ""
                uploader = item.get("uploader") or item.get("creator") or "Unknown uploader"
                markdown.append(f"- [{uploader} — {item['title']}]({item['url']}) [{item['provider']}]{suffix}")
                plain.append(f"- {uploader} — {item['title']} ({item['provider']}) {item['url']}{suffix}")
            markdown.append("")
            plain.append("")
        markdown_path = self.data_dir / "library.md"
        text_path = self.data_dir / "library.txt"
        markdown_path.write_text("\n".join(markdown).rstrip() + "\n", encoding="utf-8")
        text_path.write_text("\n".join(plain).rstrip() + "\n", encoding="utf-8")
        all_tracks = self.list_tracks(limit=2000, include_hidden=True)
        json_path = self.data_dir / "library.json"
        csv_path = self.data_dir / "library.csv"
        m3u_path = self.data_dir / "library.m3u"
        json_path.write_text(json.dumps({"playlists": playlists, "tracks": all_tracks}, ensure_ascii=False, indent=2), encoding="utf-8")
        columns = ["id", "provider", "remote_id", "url", "title", "uploader", "artist", "album", "genre", "duration", "view_count"]
        with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore"); writer.writeheader(); writer.writerows(all_tracks)
        m3u_path.write_text("#EXTM3U\n" + "\n".join(str(track["url"]) for track in all_tracks if track.get("url")) + "\n", encoding="utf-8")
        return {"markdown": str(markdown_path), "text": str(text_path), "json": str(json_path), "csv": str(csv_path), "m3u": str(m3u_path)}
