"""Background metadata importer built on the existing safe yt-dlp discovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import bili2yt

from .database import LibraryDatabase
from .providers import ProviderAdapter, adapter_for_name, adapter_for_url


ProgressCallback = Callable[[dict[str, Any]], None]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ImportOptions:
    """Options intentionally shared with the old source-discovery command."""

    min_views: int | None = None
    include_unknown_views: bool = False
    max_items: int | None = None
    cookies_from_browser: str | None = None
    cookies_file: str | None = None
    request_delay: float = 1.0
    metadata_delay: float | None = None
    retries: int = 3
    youtube_popular: bool = False
    refresh_views: bool = False
    sync_policy: str = "append_only"

    def source_options(self) -> dict[str, Any]:
        return {
            "min_views": self.min_views,
            "include_unknown_views": self.include_unknown_views,
            "max_items": self.max_items,
            "cookies_from_browser": self.cookies_from_browser,
            "cookies_file": self.cookies_file,
            "request_delay": self.request_delay,
            "metadata_delay": self.metadata_delay,
            "retries": self.retries,
            "youtube_popular": self.youtube_popular,
            "refresh_views": self.refresh_views,
            "sync_policy": self.sync_policy,
        }


@dataclass
class ImportResult:
    source_urls: list[str]
    target_playlist_id: int
    discovered_count: int = 0
    added_memberships: int = 0
    existing_memberships: int = 0
    tracks_upserted: int = 0
    removed_memberships: int = 0
    preserved_local_removals: int = 0
    skipped_without_id: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_urls": self.source_urls,
            "target_playlist_id": self.target_playlist_id,
            "discovered_count": self.discovered_count,
            "added_memberships": self.added_memberships,
            "existing_memberships": self.existing_memberships,
            "tracks_upserted": self.tracks_upserted,
            "removed_memberships": self.removed_memberships,
            "preserved_local_removals": self.preserved_local_removals,
            "skipped_without_id": self.skipped_without_id,
        }


class LibraryImporter:
    """Import provider links without requesting media formats or downloads."""

    def __init__(
        self,
        database: LibraryDatabase,
        *,
        extractor_factory: Callable[..., bili2yt.SourceExtractor] = bili2yt.SourceExtractor,
    ) -> None:
        self.database = database
        self.extractor_factory = extractor_factory

    @staticmethod
    def _validated_urls(urls: Iterable[str]) -> list[str]:
        cleaned = [" ".join(str(url).split()).strip() for url in urls]
        result = [url for url in cleaned if url]
        if not result:
            raise ValueError("Enter at least one YouTube or Bilibili URL")
        unsupported = [url for url in result if adapter_for_url(url) is None]
        if unsupported:
            raise ValueError(f"Only YouTube and Bilibili URLs are supported: {unsupported[0]}")
        return result

    @staticmethod
    def _source_urls(urls: list[str], options: ImportOptions) -> list[str]:
        if not options.youtube_popular:
            return urls
        return [
            bili2yt.youtube_popular_source(url) if adapter_for_url(url).name == "youtube" else url
            for url in urls
        ]

    @staticmethod
    def _safe_source_metadata(
        raw: Mapping[str, Any],
        *,
        source_url: str,
        original_source_urls: list[str],
    ) -> dict[str, Any]:
        """Store useful import context, never yt-dlp's potentially huge formats."""
        keys = (
            "id",
            "title",
            "uploader",
            "uploader_id",
            "uploader_url",
            "channel",
            "channel_id",
            "channel_url",
            "playlist_uploader",
            "playlist_uploader_id",
            "playlist_channel",
            "playlist_channel_id",
            "playlist",
            "playlist_id",
            "playlist_index",
            "upload_date",
            "timestamp",
            "availability",
            "live_status",
        )
        metadata = {key: raw[key] for key in keys if raw.get(key) is not None}
        metadata["discovered_from"] = source_url
        metadata["requested_sources"] = original_source_urls
        return metadata

    @staticmethod
    def _adapter_for_item(item: bili2yt.SourceItem) -> ProviderAdapter:
        adapter = adapter_for_name(item.source_kind) or adapter_for_url(item.url)
        if adapter is None:
            raise ValueError(f"Unsupported provider for {item.url}")
        return adapter

    def import_urls(
        self,
        urls: Iterable[str],
        *,
        target_playlist_id: int,
        options: ImportOptions | None = None,
        progress: ProgressCallback | None = None,
    ) -> ImportResult:
        """Discover sources then atomically upsert their links one at a time.

        `SourceExtractor.discover()` retains its existing retry/cookie behavior.
        Setting ``refresh_views`` invokes the existing lightweight metadata pass
        but does not filter out videos; normal imports avoid that extra work.
        """
        actual_options = options or ImportOptions()
        if actual_options.sync_policy not in {
            "append_only", "mirror", "mirror_preserve_local_removals"
        }:
            raise ValueError("Sync policy must be append_only, mirror, or mirror_preserve_local_removals")
        original_urls = self._validated_urls(urls)
        self.database.get_playlist(target_playlist_id)
        source_urls = self._source_urls(original_urls, actual_options)
        result = ImportResult(source_urls=original_urls, target_playlist_id=target_playlist_id)

        cookies = bili2yt.parse_cookies_from_browser(actual_options.cookies_from_browser)
        cookies_file = Path(actual_options.cookies_file).expanduser() if actual_options.cookies_file else None
        if cookies_file is not None and not cookies_file.is_file():
            raise ValueError("Cookie file was not found")

        def report(kind: str, **data: Any) -> None:
            if progress:
                progress({"type": kind, **data})

        # Passing a 0 threshold makes the old extractor obtain view metadata
        # while include_unknown_views preserves every discovered item.
        discovery_min_views = actual_options.min_views
        include_unknown = actual_options.include_unknown_views
        if actual_options.refresh_views and discovery_min_views is None:
            discovery_min_views = 0
            include_unknown = True

        report("import_progress", stage="discovering", total_sources=len(source_urls))
        extractor = self.extractor_factory(
            cookies=cookies,
            cookies_file=cookies_file,
            request_delay=actual_options.request_delay,
            metadata_delay=actual_options.metadata_delay,
            retries=actual_options.retries,
        )
        try:
            for source_index, (original_url, source_url) in enumerate(
                zip(original_urls, source_urls), start=1
            ):
                source_started_at = _utc_now()
                adapter = adapter_for_url(original_url)
                assert adapter is not None
                source_discovered = 0
                source_added = 0
                source_seen_at = _utc_now()
                seen_track_ids: set[int] = set()
                source_id = self.database.ensure_source(
                    url=original_url,
                    provider=adapter.name,
                    target_playlist_id=target_playlist_id,
                    options=actual_options.source_options(),
                )
                try:
                    # Discover one source at a time. This makes source history
                    # truthful and lets a future refresh repeat exactly its
                    # saved URL/options/target playlist.
                    items = extractor.discover(
                        [source_url],
                        min_views=discovery_min_views,
                        include_unknown_views=include_unknown,
                        max_items=actual_options.max_items,
                        enrich_missing_metadata=actual_options.refresh_views,
                    )
                    source_discovered = len(items)
                    result.discovered_count += source_discovered
                    report(
                        "import_progress",
                        stage="saving",
                        source=original_url,
                        source_index=source_index,
                        discovered_count=source_discovered,
                    )
                    for index, item in enumerate(items, start=1):
                        item_adapter = self._adapter_for_item(item)
                        remote_id = item.source_id or item_adapter.canonical_id_from_url(item.url)
                        if not remote_id:
                            result.skipped_without_id.append(item.url)
                            continue
                        track = self.database.upsert_track(
                            provider=item_adapter.name,
                            remote_id=remote_id,
                            url=item_adapter.playback_url(item.url, remote_id),
                            title=item.title,
                            creator=item_adapter.normalize_creator(item.raw),
                            view_count=item.view_count,
                            duration=item.duration,
                            source=self._safe_source_metadata(
                                item.raw,
                                source_url=original_url,
                                original_source_urls=[original_url],
                            ),
                        )
                        # Provider credits are suggestions only.  Keep them
                        # separate from user edits so later refreshes cannot
                        # overwrite an active local override.
                        raw = item.raw if isinstance(item.raw, dict) else {}
                        provider_values = {
                            key: raw.get(key)
                            for key in (
                                "artist", "album", "album_artist", "composer", "featured_artists",
                                "release_year", "genre", "tags", "thumbnail", "thumbnail_url",
                                "track_number", "disc_number", "release_date", "original_release_date",
                                "label", "catalog_number", "country", "language", "explicit", "version",
                                "is_live", "is_acoustic", "is_instrumental", "bpm", "musical_key",
                                "mood", "energy", "isrc", "upc", "musicbrainz_recording_id",
                                "musicbrainz_release_id", "discogs_release_id", "wikidata_id",
                                "lyrics_url", "copyright", "license",
                            )
                            if raw.get(key) not in (None, "")
                        }
                        if "thumbnail" in provider_values and "artwork_url" not in provider_values:
                            provider_values["artwork_url"] = provider_values.pop("thumbnail")
                        if provider_values:
                            self.database.refresh_provider_metadata(int(track["id"]), provider_values, source=item_adapter.name)
                        track_id = int(track["id"])
                        seen_track_ids.add(track_id)
                        provenance = self.database.source_membership(source_id, track_id)
                        if (
                            provenance
                            and provenance.get("deleted_at") is not None
                            and actual_options.sync_policy == "mirror_preserve_local_removals"
                        ):
                            self.database.mark_source_membership(
                                source_id,
                                track_id,
                                playlist_track_id=int(provenance["playlist_track_id"])
                                if provenance.get("playlist_track_id") is not None else None,
                                owns_membership=bool(provenance.get("owns_membership")),
                                seen_at=source_seen_at,
                            )
                            result.preserved_local_removals += 1
                            added = False
                        else:
                            if provenance and provenance.get("deleted_at") is not None:
                                self.database.restore_membership(int(provenance["playlist_track_id"]))
                                membership = {
                                    "id": int(provenance["playlist_track_id"]),
                                }
                                added = True
                            else:
                                membership, added = self.database.add_track_to_playlist(
                                    target_playlist_id, track_id
                                )
                            # An entry newly inserted by this import is source-owned.
                            # Existing entries are source-owned only when another
                            # source already established provenance for that entry.
                            owns = bool(added)
                            if not added:
                                linked = self.database._rows(
                                    "SELECT 1 FROM source_memberships "
                                    "WHERE playlist_track_id = ? AND owns_membership = 1 LIMIT 1",
                                    (membership["id"],),
                                )
                                owns = bool(linked)
                            self.database.mark_source_membership(
                                source_id,
                                track_id,
                                playlist_track_id=int(membership["id"]),
                                owns_membership=owns,
                                seen_at=source_seen_at,
                            )
                        result.tracks_upserted += 1
                        if added:
                            source_added += 1
                            result.added_memberships += 1
                        else:
                            result.existing_memberships += 1
                        report(
                            "import_progress",
                            stage="saving",
                            source=original_url,
                            completed=index,
                            discovered_count=source_discovered,
                            title=item.title,
                        )
                    reconciled = self.database.reconcile_source_memberships(
                        source_id,
                        seen_track_ids=seen_track_ids,
                        seen_at=source_seen_at,
                        policy=actual_options.sync_policy,
                    )
                    result.removed_memberships += reconciled["removed"]
                except Exception as exc:
                    self.database.record_source_run(
                        url=original_url,
                        provider=adapter.name,
                        target_playlist_id=target_playlist_id,
                        options=actual_options.source_options(),
                        started_at=source_started_at,
                        discovered_count=source_discovered,
                        added_count=source_added,
                        error=str(exc),
                    )
                    raise
                self.database.record_source(
                    url=original_url,
                    provider=adapter.name,
                    target_playlist_id=target_playlist_id,
                    options=actual_options.source_options(),
                    count=source_added,
                )
                self.database.record_source_run(
                    url=original_url,
                    provider=adapter.name,
                    target_playlist_id=target_playlist_id,
                    options=actual_options.source_options(),
                    started_at=source_started_at,
                    discovered_count=source_discovered,
                    added_count=source_added,
                )
        except Exception as exc:
            raise
        finally:
            extractor.close()
        report("import_finished", result=result.as_dict())
        return result

    def discover_candidates(
        self,
        urls: Iterable[str],
        *,
        options: ImportOptions | None = None,
        page_size: int = 100,
        seen: Iterable[str] = (),
        progress: ProgressCallback | None = None,
    ) -> list[dict[str, Any]]:
        """Discover link/metadata candidates without writing tracks.

        This deliberately reuses the same extractor and cutoff/cookie options
        as imports, then applies provider-ID exclusion at the repository
        boundary.  It never downloads media or creates playlist memberships.
        """
        actual_options = options or ImportOptions()
        original_urls = self._validated_urls(urls)
        bounded_size = max(1, min(int(page_size), 500))
        cookies = bili2yt.parse_cookies_from_browser(actual_options.cookies_from_browser)
        cookies_file = Path(actual_options.cookies_file).expanduser() if actual_options.cookies_file else None
        if cookies_file is not None and not cookies_file.is_file():
            raise ValueError("Cookie file was not found")
        extractor = self.extractor_factory(
            cookies_from_browser=cookies,
            cookies_file=cookies_file,
            request_delay=actual_options.request_delay,
            metadata_delay=actual_options.metadata_delay,
            retries=actual_options.retries,
        )
        existing = {(str(row["provider"]), str(row["remote_id"])) for row in self.database._rows("SELECT provider, remote_id FROM tracks")}
        seen_keys = {str(value) for value in seen}
        candidates: list[dict[str, Any]] = []
        for source_index, source_url in enumerate(original_urls, start=1):
            adapter = adapter_for_url(source_url)
            if adapter is None:
                continue
            items = extractor.discover(
                [source_url],
                min_views=actual_options.min_views,
                include_unknown_views=actual_options.include_unknown_views,
                max_items=bounded_size,
                enrich_missing_metadata=actual_options.refresh_views,
            )
            for item in items:
                item_adapter = self._adapter_for_item(item)
                remote_id = item.source_id or item_adapter.canonical_id_from_url(item.url)
                if not remote_id:
                    continue
                key = f"{item_adapter.name}:{remote_id}"
                if (item_adapter.name, str(remote_id)) in existing or key in seen_keys:
                    continue
                raw = item.raw if isinstance(item.raw, dict) else {}
                candidates.append({
                    "provider": item_adapter.name,
                    "remote_id": str(remote_id),
                    "url": item_adapter.playback_url(item.url, str(remote_id)),
                    "title": item.title,
                    "uploader": item_adapter.normalize_creator(raw),
                    "view_count": item.view_count,
                    "duration": item.duration,
                    "source_url": source_url,
                    "provenance": {"source_url": source_url, "source_index": source_index},
                    "availability": raw.get("availability") or raw.get("live_status"),
                })
                if len(candidates) >= bounded_size:
                    extractor.close()
                    return candidates
            if progress:
                progress({"type": "discover_progress", "source": source_url, "completed": source_index, "discovered_count": len(candidates)})
        extractor.close()
        return candidates

    def refresh_source(
        self,
        source_id: int,
        *,
        progress: ProgressCallback | None = None,
    ) -> ImportResult:
        """Re-run one saved source with the options and destination it stored."""
        source = self.database.get_source(source_id)
        saved = dict(source.get("options") or {})
        option_keys = ImportOptions.__dataclass_fields__
        options = ImportOptions(**{key: saved[key] for key in option_keys if key in saved})
        return self.import_urls(
            [str(source["url"])],
            target_playlist_id=int(source["target_playlist_id"]),
            options=options,
            progress=progress,
        )
