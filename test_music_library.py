import tempfile
import unittest
import json
import time
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import bili2yt
from music_library.app import create_app
from music_library.database import LibraryDatabase
from music_library.importer import ImportOptions, ImportResult, LibraryImporter
from music_library.providers import adapter_for_url
from music_library.queueing import true_shuffle, unique_tracks, variety_shuffle


def add_track(database, *, provider="youtube", remote_id="id", title="Title", creator="Creator", view_count=123, duration=180):
    return database.upsert_track(
        provider=provider,
        remote_id=remote_id,
        url=f"https://example.invalid/{remote_id}",
        title=title,
        creator=creator,
        view_count=view_count,
        duration=duration,
    )


class TestProviders(unittest.TestCase):
    def test_provider_ids_and_creator_fallbacks(self):
        youtube = adapter_for_url("https://youtu.be/abc123?t=1")
        bili = adapter_for_url("https://www.bilibili.com/video/BV1xx411c7mD")
        self.assertEqual(youtube.canonical_id_from_url("https://www.youtube.com/shorts/abc123?x=1"), "abc123")
        self.assertEqual(bili.canonical_id_from_url("https://www.bilibili.com/video/BV1xx411c7mD?p=2"), "BV1xx411c7mD")
        self.assertEqual(youtube.normalize_creator({}), "Unknown creator")
        self.assertEqual(youtube.normalize_creator({"playlist_channel": "Playlist Owner"}), "Unknown creator")
        self.assertEqual(bili.normalize_creator({"uploader": "  Bili Creator  "}), "Bili Creator")
        self.assertIsNone(adapter_for_url("https://example.com/video"))


class TestLibraryDatabase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = LibraryDatabase(Path(self.temp.name))

    def tearDown(self):
        self.database.close()
        self.temp.cleanup()

    def test_exact_dedupe_membership_copies_and_possible_duplicate_review(self):
        playlist = self.database.create_playlist("One")
        first = add_track(self.database, remote_id="same", title="Same Song", creator="Artist")
        same = add_track(self.database, remote_id="same", title="Updated same Song", creator="Artist")
        bili = add_track(
            self.database,
            provider="bilibili",
            remote_id="BVsame",
            title="Updated same Song",
            creator="Artist",
        )
        self.assertEqual(first["id"], same["id"])
        _, added = self.database.add_track_to_playlist(playlist["id"], first["id"])
        self.assertTrue(added)
        _, added = self.database.add_track_to_playlist(playlist["id"], first["id"])
        self.assertFalse(added)
        _, added = self.database.add_track_to_playlist(playlist["id"], first["id"], allow_duplicate=True)
        self.assertTrue(added)
        self.assertEqual(len(self.database.playlist_tracks(playlist["id"])), 2)
        self.assertEqual(
            {item["id"] for item in self.database.possible_duplicates()[0]["tracks"]},
            {first["id"], bili["id"]},
        )

    def test_bulk_playlist_actions_add_and_soft_remove_selected_tracks(self):
        source = self.database.create_playlist("Source")
        target = self.database.create_playlist("Target")
        tracks = [
            add_track(self.database, remote_id=f"bulk-{index}", title=f"Bulk {index}")
            for index in range(3)
        ]
        for track in tracks:
            self.database.add_track_to_playlist(source["id"], track["id"])
        added = self.database.add_tracks_to_playlist(target["id"], [track["id"] for track in tracks])
        self.assertEqual(added, {"added": 3, "existing": 0})
        repeated = self.database.add_tracks_to_playlist(target["id"], [tracks[0]["id"], tracks[1]["id"]])
        self.assertEqual(repeated, {"added": 0, "existing": 2})
        removed = self.database.remove_memberships_for_tracks(source["id"], [tracks[0]["id"], tracks[2]["id"]])
        self.assertEqual(removed, 2)
        self.assertEqual(
            [item["id"] for item in self.database.playlist_tracks(source["id"])],
            [tracks[1]["id"]],
        )
        self.assertEqual(len(self.database.playlist_tracks(target["id"])), 3)

    def test_soft_delete_restore_merge_and_exports_persist(self):
        one = self.database.create_playlist("First")
        two = self.database.create_playlist("Second")
        a = add_track(self.database, remote_id="a", title="A")
        b = add_track(self.database, remote_id="b", title="B")
        c = add_track(self.database, remote_id="c", title="C")
        first_membership, _ = self.database.add_track_to_playlist(one["id"], a["id"])
        self.database.add_track_to_playlist(one["id"], b["id"])
        self.database.add_track_to_playlist(two["id"], b["id"])
        self.database.add_track_to_playlist(two["id"], c["id"])
        self.database.remove_membership(first_membership["id"])
        self.assertEqual(self.database.list_trash()["memberships"][0]["membership_id"], first_membership["id"])
        self.database.restore_membership(first_membership["id"])
        merged = self.database.merge_playlists([one["id"], two["id"]], "Merged")
        self.assertEqual(
            [row["remote_id"] for row in self.database.playlist_tracks(merged["id"])],
            ["a", "b", "c"],
        )
        self.database.rate_track(a["id"], 7.5)
        self.database.hide_track(c["id"], hidden=True)
        exports = self.database.export_library()
        markdown = Path(exports["markdown"]).read_text(encoding="utf-8")
        plain = Path(exports["text"]).read_text(encoding="utf-8")
        self.assertIn("rating 7.5/10", markdown)
        self.assertIn("hidden", plain)
        self.database.close()
        self.database = LibraryDatabase(Path(self.temp.name))
        self.assertEqual(self.database.get_track(a["id"])["rating"], 7.5)

    def test_pools_and_queue_unique_and_variety_spacing(self):
        playlist = self.database.create_playlist("Queue")
        tracks = [
            add_track(self.database, remote_id=f"a{number}", title=f"A{number}", creator="A")
            for number in range(3)
        ] + [
            add_track(self.database, remote_id=f"b{number}", title=f"B{number}", creator="B")
            for number in range(3)
        ] + [add_track(self.database, remote_id="c", title="C", creator="C")]
        for track in tracks:
            self.database.add_track_to_playlist(playlist["id"], track["id"])
        self.database.add_track_to_playlist(playlist["id"], tracks[0]["id"], allow_duplicate=True)
        pool = self.database.pool_tracks({"playlist_ids": [playlist["id"]], "include_repeats": False})
        self.assertEqual(len(pool), 7)
        repeats = self.database.pool_tracks({"playlist_ids": [playlist["id"]], "include_repeats": True})
        self.assertEqual(len(repeats), 8)
        outside = add_track(self.database, remote_id="outside", title="Outside", creator="Outside")
        combined = self.database.pool_tracks(
            {"playlist_ids": [playlist["id"]], "track_ids": [outside["id"]], "include_repeats": False}
        )
        self.assertEqual({track["id"] for track in combined}, {track["id"] for track in pool} | {outside["id"]})
        shuffled = true_shuffle(pool, seed=5)
        self.assertEqual({track["id"] for track in shuffled}, {track["id"] for track in pool})
        varied = variety_shuffle(pool, cooldown=1, seed=4)
        for left, right in zip(varied, varied[1:]):
            self.assertNotEqual(left["creator"], right["creator"])
        self.database.set_queue(shuffled, mode="true", include_repeats=False)
        self.assertEqual(self.database.queue_state()["current"]["id"], shuffled[0]["id"])
        self.database.move_queue(1, reason="test")
        self.assertEqual(self.database.queue_state()["current"]["id"], shuffled[1]["id"])

    def test_smart_playlists_filters_pagination_creator_and_history(self):
        manual = self.database.create_playlist("Manual")
        smart = self.database.create_playlist("Rated", kind="smart", query={"min_rating": 8})
        alpha = add_track(self.database, remote_id="alpha", title="Alpha", creator="Alpha Artist")
        beta = add_track(self.database, remote_id="beta", title="Beta", creator="Beta Artist")
        gamma = add_track(self.database, remote_id="gamma", title="Gamma", creator="Alpha Artist")
        for track in (alpha, beta, gamma):
            self.database.add_track_to_playlist(manual["id"], track["id"])
        self.database.rate_track(alpha["id"], 8.5)
        self.database.rate_track(beta["id"], 9)
        self.assertEqual(self.database.playlist_track_page(smart["id"], limit=1)["total"], 2)
        page = self.database.playlist_track_page(manual["id"], query="alpha", limit=1)
        self.assertEqual(page["total"], 2)
        self.assertEqual(len(page["items"]), 1)
        creator_page = self.database.playlist_track_page(manual["id"], creator="beta", limit=5)
        self.assertEqual([item["id"] for item in creator_page["items"]], [beta["id"]])
        self.assertEqual(
            [track["id"] for track in self.database.pool_tracks({"creator": "alpha"})],
            [alpha["id"], gamma["id"]],
        )
        self.database.rate_track(beta["id"], None)
        self.assertIsNone(self.database.get_track(beta["id"])["rating"])
        self.database.set_queue([alpha, gamma], mode="true", include_repeats=False)
        self.database.move_queue(1, reason="verified_end")
        history = self.database.list_playback_history()
        self.assertEqual(history["items"][0]["track_id"], alpha["id"])
        self.assertEqual(history["items"][0]["reason"], "verified_end")
        self.database.delete_pool(self.database.save_pool("Temporary", {"creator": "alpha"})["id"])
        self.assertEqual(self.database.list_pools(), [])
        self.assertEqual(self.database.clear_queue()["total"], 0)


    def test_metadata_overrides_relations_and_smart_rules(self):
        first = add_track(self.database, remote_id="rel-1", title="Signal", creator="Uploader", provider="youtube")
        second = add_track(self.database, remote_id="rel-2", title="Signal (Remix)", creator="Uploader", provider="bilibili")
        self.database.refresh_provider_metadata(first["id"], {"artist": "Provider Artist", "album": "Release"})
        self.database.update_track_metadata(first["id"], {"artist": "Local Artist", "tags": ["focus", "night"]})
        self.database.refresh_provider_metadata(first["id"], {"artist": "Changed Provider"})
        self.assertEqual(self.database.get_track(first["id"])["artist"], "Local Artist")
        self.assertEqual(self.database.get_track(first["id"])["tags"], ["focus", "night"])
        suggestions = self.database.suggest_track_relations()
        self.assertTrue(any(item["relation_type"] == "remix_of" for item in suggestions))
        self.database.save_relation_suggestions(suggestions)
        self.assertTrue(self.database.list_relations(status="proposed"))
        playlist = self.database.create_playlist("Smart", kind="smart", query={"rules": {"all": [{"field": "artist", "op": "contains", "value": "Local"}]}})
        page = self.database.playlist_track_page(playlist["id"])
        self.assertEqual([item["id"] for item in page["items"]], [first["id"]])

    def test_unicode_fuzzy_relations_and_relation_neighborhood(self):
        original = add_track(self.database, remote_id="unicode-1", title="夜に駆ける", creator="YOASOBI", provider="youtube", duration=240)
        alternate = add_track(self.database, remote_id="unicode-2", title="夜に駆ける [Live]", creator="YOASOBI", provider="bilibili", duration=242)
        related = add_track(self.database, remote_id="unicode-3", title="夜に駆ける (Remix)", creator="YOASOBI", provider="youtube", duration=240)
        suggestions = self.database.suggest_track_relations(min_confidence=0.65)
        self.assertTrue(any(item["to_track_id"] == alternate["id"] for item in suggestions))
        self.assertTrue(any(item["relation_type"] == "remix_of" for item in suggestions))
        self.database.save_relation_suggestions(suggestions)
        with self.database.transaction() as conn:
            conn.execute("UPDATE track_relations SET status='accepted'")
        neighborhood = self.database.relation_neighborhood(original["id"], max_depth=1)
        self.assertEqual(neighborhood["root_track_id"], original["id"])
        self.assertGreaterEqual(len(neighborhood["nodes"]), 2)
        playlist = self.database.create_playlist(
            "Unicode relations", kind="smart",
            query={"relation": {"root_track_id": original["id"], "max_depth": 1}},
        )
        self.assertEqual({item["id"] for item in self.database.playlist_tracks(playlist["id"])}, set())
        self.assertEqual({item["id"] for item in self.database.smart_playlist_tracks({"relation": {"root_track_id": original["id"], "max_depth": 1}})}, {alternate["id"], related["id"]})

    def test_least_recently_played_sort_and_filters(self):
        never = add_track(self.database, remote_id="never", title="Never", creator="U", view_count=10)
        played = add_track(self.database, remote_id="played", title="Played", creator="U", view_count=100)
        self.database.update_track_metadata(never["id"], {"artist": "Alpha", "genre": "ambient"})
        self.database.update_track_metadata(played["id"], {"artist": "Beta", "genre": "rock"})
        self.database.set_queue([played], mode="true", include_repeats=False)
        self.database.move_queue(0, reason="test")
        # Add an explicit history row so the comparison is deterministic.
        with self.database.transaction() as conn:
            conn.execute(
                "INSERT INTO playback_history(track_id, played_at, reason) VALUES(?, ?, ?)",
                (played["id"], "2020-01-01T00:00:00+00:00", "test"),
            )
        tracks = self.database.list_tracks(sort="least_recently_played", order="asc")
        self.assertEqual(tracks[0]["id"], never["id"])
        self.assertEqual(self.database.track_count(artist="Alpha"), 1)
        self.assertEqual(self.database.track_count(min_views=50), 1)

    def test_atomic_restore_reopens_live_connection_and_keeps_one_rollback(self):
        original = add_track(self.database, remote_id="before-backup", title="Before backup")
        backup = self.database.create_backup(kind="manual")
        later = add_track(self.database, remote_id="after-backup", title="After backup")
        result = self.database.restore_backup(backup["id"])
        self.assertTrue(result["restored"])
        self.assertTrue(Path(result["rollback_path"]).is_file())
        self.assertEqual(self.database.get_track(original["id"])["title"], "Before backup")
        with self.assertRaises(ValueError):
            self.database.get_track(later["id"])
        self.assertEqual(
            self.database._conn.execute("PRAGMA integrity_check").fetchone()[0], "ok"
        )
        rolled_back = self.database.rollback_restore()
        self.assertTrue(rolled_back["rolled_back"])
        self.assertEqual(self.database.get_track(later["id"])["title"], "After backup")
        self.assertFalse(Path(result["rollback_path"]).exists())

    def test_tiered_backup_retention_and_safety_preservation(self):
        backup_dir = Path(self.temp.name) / "backups"
        backup_dir.mkdir(exist_ok=True)
        with self.database.transaction() as conn:
            for index, created_at in enumerate((
                "2026-08-31T12:00:00+00:00",
                "2026-08-30T12:00:00+00:00",
                "2026-08-20T12:00:00+00:00",
                "2026-07-10T12:00:00+00:00",
                "2026-01-10T12:00:00+00:00",
                "2025-01-10T12:00:00+00:00",
            )):
                backup_id = f"retention-{index}"
                path = backup_dir / f"{backup_id}.zip"
                path.write_bytes(b"placeholder")
                kind = "safety" if index == 5 else "scheduled"
                conn.execute(
                    "INSERT INTO backups(id, path, kind, manifest_json, created_at, checksum) "
                    "VALUES(?, ?, ?, ?, ?, '')",
                    (backup_id, str(path), kind, json.dumps({"timestamp": created_at}), created_at),
                )
        removed = self.database.prune_backups(daily=1, weekly=1, monthly=1, long_term=1)
        remaining = {item["id"] for item in self.database.list_backups()}
        self.assertGreater(removed, 0)
        self.assertIn("retention-0", remaining)
        self.assertIn("retention-5", remaining)

    def test_advanced_bulk_edit_preview_apply_and_undo(self):
        source = add_track(self.database, remote_id="bulk-source", title="Source")
        target = add_track(self.database, remote_id="bulk-target", title="Target")
        self.database.update_track_metadata(
            source["id"], {"album": "Copied Album", "tags": ["night"]}
        )
        self.database.update_track_metadata(
            target["id"], {"artist": "DJ OLD", "tags": ["focus"]}
        )
        operations = [
            {"type": "regex_replace", "field": "artist", "pattern": "OLD$", "replacement": "New", "ignore_case": True},
            {"type": "tags_add", "field": "tags", "values": ["night", "focus"]},
            {"type": "copy_from_track", "source_track_id": source["id"], "fields": ["album"]},
        ]
        preview = self.database.bulk_metadata_preview(
            [target["id"]], operations=operations
        )
        self.assertEqual(preview["items"][0]["after"]["artist"], "DJ New")
        self.assertEqual(preview["items"][0]["after"]["tags"], ["focus", "night"])
        applied = self.database.bulk_update_metadata(
            [target["id"]], operations=operations
        )
        updated = self.database.get_track(target["id"])
        self.assertEqual(updated["album"], "Copied Album")
        self.assertEqual(updated["artist"], "DJ New")
        self.assertGreater(self.database.undo_metadata(applied["operation_id"]), 0)
        restored = self.database.get_track(target["id"])
        self.assertEqual(restored["artist"], "DJ OLD")
        self.assertIsNone(restored.get("album"))

    def test_explicit_fingerprints_suggest_same_recording_without_media_fetch(self):
        first = add_track(self.database, remote_id="fp-one", title="First title")
        second = add_track(self.database, remote_id="fp-two", title="Different title")
        self.database.save_track_fingerprint(
            first["id"], algorithm="chromaprint", fingerprint="encoded-fingerprint"
        )
        self.database.save_track_fingerprint(
            second["id"], algorithm="chromaprint", fingerprint="encoded-fingerprint"
        )
        suggestions = self.database.suggest_track_relations()
        self.assertTrue(any(
            item["relation_type"] == "same_recording"
            and item["evidence"].get("local_opt_in")
            for item in suggestions
        ))
        local_file = Path(self.temp.name) / "explicit-audio.bin"
        local_file.write_bytes(b"not media, but path validation should pass")
        with patch("music_library.database.shutil.which", return_value=None):
            with self.assertRaisesRegex(ValueError, "fpcalc is not installed"):
                self.database.fingerprint_local_file(first["id"], local_file)

class TestImporter(unittest.TestCase):
    def test_importer_uses_existing_discovery_and_dedupes(self):
        class FakeExtractor:
            last_kwargs = None

            def __init__(self, **kwargs):
                type(self).last_kwargs = kwargs

            def discover(self, urls, **kwargs):
                type(self).discover_kwargs = kwargs
                return [
                    bili2yt.SourceItem(
                        "https://www.youtube.com/watch?v=abc",
                        "Imported",
                        42,
                        120,
                        "abc",
                        "youtube",
                        {"uploader": "Creator", "playlist": "Remote"},
                    ),
                    bili2yt.SourceItem(
                        "https://www.youtube.com/watch?v=abc",
                        "Imported",
                        42,
                        120,
                        "abc",
                        "youtube",
                        {"uploader": "Creator"},
                    ),
                ]

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            database = LibraryDatabase(Path(directory))
            playlist = database.create_playlist("Import target")
            importer = LibraryImporter(database, extractor_factory=FakeExtractor)
            result = importer.import_urls(
                ["https://www.youtube.com/@creator"],
                target_playlist_id=playlist["id"],
                options=ImportOptions(refresh_views=True, youtube_popular=True),
            )
            self.assertEqual(result.discovered_count, 2)
            self.assertEqual(result.added_memberships, 1)
            self.assertEqual(FakeExtractor.discover_kwargs["min_views"], 0)
            self.assertTrue(FakeExtractor.discover_kwargs["include_unknown_views"])
            self.assertEqual(database.list_tracks()[0]["creator"], "Creator")
            self.assertEqual(len(database.list_source_runs()), 1)
            database.close()

    def test_refresh_source_preserves_options_and_tracks_each_source(self):
        class FakeExtractor:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def discover(self, urls, **kwargs):
                source = urls[0]
                item_id = "one" if "one" in source else "two"
                return [bili2yt.SourceItem(
                    f"https://www.youtube.com/watch?v={item_id}", item_id, 1, 90, item_id,
                    "youtube", {"uploader": "Creator"},
                )]

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            database = LibraryDatabase(Path(directory))
            playlist = database.create_playlist("Refresh target")
            importer = LibraryImporter(database, extractor_factory=FakeExtractor)
            result = importer.import_urls(
                ["https://youtube.com/@one", "https://youtube.com/@two"],
                target_playlist_id=playlist["id"],
                options=ImportOptions(max_items=12, retries=5),
            )
            self.assertEqual(result.discovered_count, 2)
            sources = database.list_sources()
            self.assertEqual(len(sources), 2)
            self.assertTrue(all(source["imported_count"] == 1 for source in sources))
            refreshed = importer.refresh_source(sources[0]["id"])
            self.assertEqual(refreshed.existing_memberships, 1)
            self.assertEqual(database.get_source(sources[0]["id"])["options"]["retries"], 5)
            runs = database.list_source_runs()
            self.assertEqual(len(runs), 3)
            self.assertTrue(all(run["discovered_count"] == 1 for run in runs))
            database.close()

    def test_source_mirror_only_removes_owned_entries_and_preserves_local_removals(self):
        class ChangingExtractor:
            entries = ["remote", "manual"]

            def __init__(self, **kwargs):
                pass

            def discover(self, urls, **kwargs):
                return [
                    bili2yt.SourceItem(
                        f"https://www.youtube.com/watch?v={item_id}",
                        item_id,
                        1,
                        90,
                        item_id,
                        "youtube",
                        {"uploader": "Creator"},
                    )
                    for item_id in type(self).entries
                ]

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            database = LibraryDatabase(Path(directory))
            playlist = database.create_playlist("Mirror target")
            manual = add_track(database, remote_id="manual", title="Manual")
            database.add_track_to_playlist(playlist["id"], manual["id"])
            importer = LibraryImporter(database, extractor_factory=ChangingExtractor)
            importer.import_urls(
                ["https://youtube.com/@mirror"],
                target_playlist_id=playlist["id"],
                options=ImportOptions(sync_policy="mirror"),
            )
            ChangingExtractor.entries = []
            source = database.list_sources()[0]
            importer.refresh_source(source["id"])
            active = {item["remote_id"] for item in database.playlist_tracks(playlist["id"])}
            self.assertEqual(active, {"manual"})

            ChangingExtractor.entries = ["preserved"]
            importer.import_urls(
                ["https://youtube.com/@preserved"],
                target_playlist_id=playlist["id"],
                options=ImportOptions(sync_policy="mirror_preserve_local_removals"),
            )
            membership = next(
                item for item in database.playlist_tracks(playlist["id"])
                if item["remote_id"] == "preserved"
            )
            database.remove_membership(membership["membership_id"])
            importer.refresh_source(next(
                item["id"] for item in database.list_sources()
                if item["url"] == "https://youtube.com/@preserved"
            ))
            self.assertNotIn(
                "preserved",
                {item["remote_id"] for item in database.playlist_tracks(playlist["id"])},
            )
            database.close()


class TestApiAndExtensionContract(unittest.TestCase):
    def test_durable_job_resume_keeps_the_same_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(Path(directory))
            database = app.state.library_db
            playlist = database.create_playlist("Recovered destination")
            job_id = "durable-same-id"
            database.create_job(
                job_id, job_type="import", label="Recovered import",
                source_urls=["https://www.youtube.com/watch?v=recovered"],
                options={"target_playlist_id": playlist["id"]},
            )
            database.update_job(job_id, status="paused")
            fake_result = ImportResult(
                source_urls=["https://www.youtube.com/watch?v=recovered"],
                target_playlist_id=playlist["id"],
            )
            with patch("music_library.app.LibraryImporter.import_urls", return_value=fake_result):
                with TestClient(app) as client:
                    response = client.post(f"/api/jobs/{job_id}/resume")
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()["id"], job_id)
                    for _ in range(50):
                        current = client.get(f"/api/jobs/{job_id}").json()
                        if current["status"] == "completed":
                            break
                        time.sleep(0.01)
                    self.assertEqual(current["status"], "completed")
                    self.assertEqual(
                        len([job for job in client.get("/api/jobs").json() if job["id"] == job_id]),
                        1,
                    )

    def test_play_without_extension_opens_default_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(Path(directory))
            database = app.state.library_db
            playlist = database.create_playlist("Player")
            first = add_track(database, remote_id="first", title="First")
            database.add_track_to_playlist(playlist["id"], first["id"])
            database.set_queue([first], mode="true", include_repeats=False)
            with patch("music_library.app.webbrowser.open", return_value=True) as opened:
                with TestClient(app) as client:
                    response = client.post("/api/queue/play")
            self.assertTrue(response.json()["opened_in_browser"])
            opened.assert_called_once()

    def test_api_queue_rating_and_paired_extension_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(Path(directory))
            database = app.state.library_db
            playlist = database.create_playlist("Player")
            first = add_track(database, remote_id="first", title="First")
            second = add_track(database, remote_id="second", title="Second")
            database.add_track_to_playlist(playlist["id"], first["id"])
            database.add_track_to_playlist(playlist["id"], second["id"])
            database.set_queue([first, second], mode="true", include_repeats=False)

            with TestClient(app) as client:
                health = client.get("/api/health")
                self.assertEqual(health.status_code, 200)
                token = health.json()["pairing_token"]
                self.assertEqual(client.post(f"/api/tracks/{first['id']}/rating", json={"value": 9.25}).status_code, 200)
                self.assertEqual(client.get("/api/queue").json()["current"]["id"], first["id"])
                with client.websocket_connect(f"/ws/extension?token={token}") as websocket:
                    websocket.send_json({"type": "hello", "profile": "test", "tab_id": 47, "resume": True})
                    command = websocket.receive_json()
                    self.assertEqual(command["command"], "navigate")
                    self.assertIn("first", command["url"])
                    websocket.send_json(
                        {
                            "type": "ended",
                            "provider": "youtube",
                            "url": "https://www.youtube.com/watch?v=first",
                            "tab_id": 47,
                            "duration": 180,
                            "ad_state": "none",
                        }
                    )
                    next_command = websocket.receive_json()
                    self.assertEqual(next_command["command"], "navigate")
                    self.assertIn("second", next_command["url"])
                self.assertEqual(client.get("/api/queue").json()["current"]["id"], second["id"])

    def test_api_paging_smart_playlist_and_rejects_unsafe_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(Path(directory))
            database = app.state.library_db
            playlist = database.create_playlist("Manual")
            smart = database.create_playlist("High rated", kind="smart", query={"min_rating": 7})
            first = add_track(database, remote_id="first", title="First")
            second = add_track(database, remote_id="second", title="Second")
            for track in (first, second):
                database.add_track_to_playlist(playlist["id"], track["id"])
            database.rate_track(first["id"], 8)
            database.set_queue([first, second], mode="true", include_repeats=False)
            with TestClient(app) as client:
                self.assertEqual(client.get(f"/api/playlists/{playlist['id']}/tracks?limit=1").json()["total"], 2)
                self.assertEqual(
                    client.get(f"/api/playlists/{playlist['id']}/tracks?creator=Creator&limit=1").json()["total"],
                    2,
                )
                self.assertEqual(client.get(f"/api/playlists/{smart['id']}/tracks").json()["total"], 1)
                token = client.get("/api/health").json()["pairing_token"]
                with client.websocket_connect(f"/ws/extension?token={token}") as websocket:
                    websocket.send_json({"type": "hello", "profile": "safe", "tab_id": 4, "resume": False})
                    websocket.send_json({
                        "type": "ended", "provider": "youtube",
                        "url": "https://www.youtube.com/watch?v=not-the-first",
                        "duration": 180, "ad_state": "none", "tab_id": 4,
                    })
                    # The backend sends only a UI notice, never another player command.
                    self.assertEqual(client.get("/api/queue").json()["current"]["id"], first["id"])
                self.assertEqual(client.post(f"/api/tracks/{first['id']}/rating", json={"value": None}).status_code, 200)
                self.assertIsNone(database.get_track(first["id"])["rating"])
                self.assertEqual(client.post("/api/queue/clear").json()["queue"]["total"], 0)
                target = database.create_playlist("Bulk target")
                bulk_add = client.post(
                    f"/api/playlists/{target['id']}/tracks/bulk",
                    json={"track_ids": [first["id"], second["id"]]},
                )
                self.assertEqual(bulk_add.status_code, 200)
                self.assertEqual(bulk_add.json()["added"], 2)
                bulk_remove = client.post(
                    f"/api/playlists/{playlist['id']}/tracks/remove-selected",
                    json={"track_ids": [first["id"]]},
                )
                self.assertEqual(bulk_remove.status_code, 200)
                self.assertEqual(bulk_remove.json()["removed"], 1)

    def test_creator_status_and_subscription_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(Path(directory))
            database = app.state.library_db
            playlist = database.create_playlist("Saved releases")
            track = database.upsert_track(
                provider="youtube",
                remote_id="creator-video",
                url="https://www.youtube.com/watch?v=creator-video",
                title="Creator release",
                creator="Producer Channel",
                view_count=1234,
                duration=180,
                source={"channel_url": "https://www.youtube.com/@producer"},
            )
            database.add_track_to_playlist(playlist["id"], track["id"])
            subscription = database.create_subscription(
                name="Producer feed",
                url="https://www.youtube.com/@producer",
                provider="youtube",
                target_playlist_id=playlist["id"],
                options={},
            )
            with TestClient(app) as client:
                creators = client.get("/api/creators").json()
                self.assertEqual(creators[0]["name"], "Producer Channel")
                status = client.get("/api/track-status", params={"url": track["url"]}).json()
                self.assertTrue(status["saved"])
                self.assertEqual(status["creator"], "Producer Channel")
                listed = client.get("/api/subscriptions").json()
                self.assertEqual(listed[0]["id"], subscription["id"])
                self.assertEqual(client.post(f"/api/subscriptions/{subscription['id']}", json={"enabled": False}).json()["enabled"], False)
                self.assertEqual(client.delete(f"/api/subscriptions/{subscription['id']}").json()["deleted"], True)

    def test_extension_save_uses_provider_playlists_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(Path(directory))
            with TestClient(app) as client:
                youtube_url = "https://www.youtube.com/watch?v=browser-youtube"
                bili_url = "https://www.bilibili.com/video/BV1browser123"
                youtube = client.post(
                    "/api/extension/save",
                    json={
                        "url": youtube_url,
                        "title": "YouTube browser track",
                        "creator": "YT producer",
                        "view_count": 1200,
                        "duration": 180,
                    },
                )
                self.assertEqual(youtube.status_code, 200)
                self.assertEqual(youtube.json()["playlist_name"], "YouTube browser saves")
                repeated = client.post(
                    "/api/extension/save",
                    json={"url": youtube_url, "title": "Updated title"},
                )
                self.assertEqual(repeated.status_code, 200)
                bili = client.post(
                    "/api/extension/save",
                    json={
                        "url": bili_url,
                        "title": "Bilibili browser track",
                        "creator": "Bili producer",
                    },
                )
                self.assertEqual(bili.status_code, 200)
                self.assertEqual(bili.json()["playlist_name"], "Bilibili browser saves")

                playlists = {item["name"]: item for item in client.get("/api/playlists").json()}
                self.assertEqual(playlists["YouTube browser saves"]["active_count"], 1)
                self.assertEqual(playlists["Bilibili browser saves"]["active_count"], 1)
                status = client.get("/api/track-status", params={"url": youtube_url}).json()
                self.assertTrue(status["saved"])
                batch = client.post(
                    "/api/track-status-batch",
                    json={"urls": [youtube_url, bili_url, "https://example.com/not-supported"]},
                )
                self.assertEqual(batch.status_code, 200)
                self.assertTrue(batch.json()["results"][youtube_url]["saved"])
                self.assertTrue(batch.json()["results"][bili_url]["saved"])
                track_id = youtube.json()["id"]
                rated = client.post(f"/api/tracks/{track_id}/rating", json={"value": 8.5})
                self.assertEqual(rated.status_code, 200)
                self.assertEqual(rated.json()["rating"], 8.5)


if __name__ == "__main__":
    unittest.main()
