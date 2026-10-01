"""Domain service boundaries for the local library.

The SQLite repository remains the source of truth, while these small facades
keep API handlers from growing direct knowledge of every table.  They are
intentionally dependency-light so callers can replace them in tests.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .database import LibraryDatabase


class MetadataService:
    def __init__(self, database: LibraryDatabase) -> None:
        self.database = database

    def get(self, track_id: int) -> dict[str, Any]:
        return self.database.get_track_metadata(track_id)

    def update(self, track_id: int, values: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
        return self.database.update_track_metadata(track_id, values, **kwargs)

    def bulk_preview(self, track_ids: Iterable[int], values: Mapping[str, Any]) -> dict[str, Any]:
        return self.database.bulk_metadata_preview(track_ids, values)

    def bulk_update(self, track_ids: Iterable[int], values: Mapping[str, Any]) -> dict[str, Any]:
        return self.database.bulk_update_metadata(track_ids, values)


class PlaylistService:
    def __init__(self, database: LibraryDatabase) -> None:
        self.database = database

    def set_operation(self, playlist_ids: Iterable[int], operation: str, name: str) -> dict[str, Any]:
        return self.database.playlist_set_operation(playlist_ids, operation=operation, name=name)

    def snapshot(self, playlist_id: int, name: str | None = None) -> dict[str, Any]:
        return self.database.snapshot_playlist(playlist_id, name=name)

    def restore_snapshot(self, snapshot_id: int) -> dict[str, Any]:
        return self.database.restore_playlist_snapshot(snapshot_id)


class RelationshipService:
    def __init__(self, database: LibraryDatabase) -> None:
        self.database = database

    def suggest(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self.database.suggest_track_relations(**kwargs)

    def review(self, relation_id: int, status: str) -> dict[str, Any]:
        return self.database.review_relation(relation_id, status=status)

