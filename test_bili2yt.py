import tempfile
import unittest
import tempfile
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import bili2yt


class TestParsing(unittest.TestCase):
    def test_view_cutoffs(self):
        self.assertEqual(bili2yt.parse_view_cutoff("100k"), 100_000)
        self.assertEqual(bili2yt.parse_view_cutoff("1.5m"), 1_500_000)
        self.assertEqual(bili2yt.parse_view_cutoff("2,000"), 2_000)

    def test_positive_and_float_validation(self):
        self.assertEqual(bili2yt.parse_positive_int("20"), 20)
        self.assertEqual(bili2yt.parse_nonnegative_float("0.5"), 0.5)
        with self.assertRaises(Exception):
            bili2yt.parse_positive_int("0")
        with self.assertRaises(Exception):
            bili2yt.parse_nonnegative_float("-1")

    def test_clip_text_is_unicode_safe(self):
        self.assertEqual(bili2yt.clip_text("hello", 10), "hello")
        self.assertEqual(bili2yt.clip_text("abcdefgh", 5), "abcd\u2026")

    def test_manual_upload_dir_is_created(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manual-uploads"
            self.assertEqual(bili2yt.prepare_manual_upload_dir(path), path)
            self.assertTrue(path.is_dir())


class TestSourcesAndFormats(unittest.TestCase):
    def test_metadata_delay_is_bounded_by_default(self):
        extractor = bili2yt.SourceExtractor(request_delay=2)
        try:
            self.assertEqual(extractor.metadata_delay, 0.25)
        finally:
            extractor.close()

    def test_metadata_delay_can_be_explicitly_stricter(self):
        extractor = bili2yt.SourceExtractor(request_delay=0.1, metadata_delay=1.5)
        try:
            self.assertEqual(extractor.metadata_delay, 1.5)
        finally:
            extractor.close()

    def test_fast_bilibili_metadata_uses_view_endpoint(self):
        class FakeResponse:
            def read(self):
                return b'{"code": 0, "data": {"bvid": "BV1abc", "title": "Fast", "duration": 12, "stat": {"view": 770154}, "owner": {"name": "Bili Creator", "mid": 123}}}'

            def close(self):
                pass

        class FakeYDL:
            def urlopen(self, request):
                self.url = request.url
                return FakeResponse()

        extractor = bili2yt.SourceExtractor()
        fake_ydl = FakeYDL()
        with patch.object(extractor, "_get_ydl", return_value=fake_ydl):
            metadata = extractor._fast_bilibili_metadata("https://www.bilibili.com/video/BV1abc")
        extractor.close()
        self.assertEqual(metadata["view_count"], 770154)
        self.assertEqual(metadata["title"], "Fast")
        self.assertEqual(metadata["uploader"], "Bili Creator")
        self.assertEqual(metadata["uploader_id"], 123)
        self.assertIn("x/web-interface/view?bvid=BV1abc", fake_ydl.url)

    def test_flat_discovery_uses_lazy_playlist_processing(self):
        extractor = bili2yt.SourceExtractor()
        try:
            options = extractor._options("https://www.youtube.com/playlist?list=abc", flat=True, playlist_end=25)
            self.assertTrue(options["extract_flat"])
            self.assertTrue(options["lazy_playlist"])
            self.assertEqual(options["playlistend"], 25)
            self.assertFalse(extractor._options("https://www.youtube.com/watch?v=abc", flat=False)["lazy_playlist"])
        finally:
            extractor.close()

    def test_only_channel_feeds_inherit_root_creator(self):
        self.assertTrue(bili2yt.SourceExtractor._is_creator_feed("https://www.youtube.com/@creator/videos"))
        self.assertTrue(bili2yt.SourceExtractor._is_creator_feed("https://www.youtube.com/channel/UC123"))
        self.assertFalse(bili2yt.SourceExtractor._is_creator_feed("https://www.youtube.com/playlist?list=PL123"))
        self.assertTrue(bili2yt.SourceExtractor._is_creator_feed("https://space.bilibili.com/123/upload/video"))
        self.assertFalse(bili2yt.SourceExtractor._is_creator_feed("https://space.bilibili.com/123/lists/456?type=series"))

    def test_mixed_youtube_playlist_does_not_misatrribute_playlist_owner(self):
        source = "https://www.youtube.com/playlist?list=PL123"
        root = {
            "uploader": "Playlist Owner",
            "entries": [{
                "id": "video-id",
                "title": "Mixed playlist video",
                "view_count": 42,
                "url": "https://www.youtube.com/watch?v=video-id",
            }],
        }
        extractor = bili2yt.SourceExtractor()
        try:
            with patch.object(extractor, "_extract", return_value=root):
                item = extractor.discover([source], min_views=None, include_unknown_views=False, max_items=None)[0]
            self.assertNotIn("uploader", item.raw)
            with patch.object(extractor, "_extract", side_effect=[root, {"uploader": "Actual Video Creator"}]):
                enriched = extractor.discover(
                    [source],
                    min_views=None,
                    include_unknown_views=False,
                    max_items=None,
                    enrich_missing_metadata=True,
                )[0]
            self.assertEqual(enriched.raw["uploader"], "Actual Video Creator")
        finally:
            extractor.close()

    def test_discovery_stops_on_repeating_provider_feed(self):
        source = "https://space.bilibili.com/631070414/upload/video"
        entry = {
            "id": "BVloop123",
            "title": "Cached popular video",
            "view_count": 800_000,
            "url": "https://www.bilibili.com/video/BVloop123",
        }

        def repeating_entries():
            while True:
                yield dict(entry)

        extractor = bili2yt.SourceExtractor()
        try:
            with patch.object(extractor, "_extract", return_value={"entries": repeating_entries()}):
                items = extractor.discover(
                    [source],
                    min_views=700_000,
                    include_unknown_views=False,
                    max_items=None,
                )
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].source_id, "BVloop123")
        finally:
            extractor.close()

    def test_discovery_stops_at_first_repeated_url_without_scan_cap(self):
        source = "https://space.bilibili.com/631070414/upload/video"
        entries = [
            {
                "id": "BVfirst",
                "title": "First",
                "view_count": 800_000,
                "url": "https://www.bilibili.com/video/BVfirst",
            },
            {
                "id": "BVsecond",
                "title": "Second",
                "view_count": 900_000,
                "url": "https://www.bilibili.com/video/BVsecond",
            },
            {
                "id": "BVfirst",
                "title": "First again",
                "view_count": 800_000,
                "url": "https://www.bilibili.com/video/BVfirst",
            },
            {
                "id": "BVnever",
                "title": "Must not be reached",
                "view_count": 999_000,
                "url": "https://www.bilibili.com/video/BVnever",
            },
        ]
        extractor = bili2yt.SourceExtractor()
        try:
            with patch.object(extractor, "_extract", return_value={"entries": iter(entries)}):
                items = extractor.discover(
                    [source],
                    min_views=700_000,
                    include_unknown_views=False,
                    max_items=None,
                )
            self.assertEqual([item.source_id for item in items], ["BVfirst", "BVsecond"])
        finally:
            extractor.close()

    def test_native_format_selector_pairs_video_and_audio(self):
        selected = bili2yt.SelectedFormats(
            video={"format_id": "30280"},
            audio={"format_id": "30216"},
        )
        self.assertEqual(bili2yt.SourceExtractor._native_format_selector(selected), "30280+30216")

    def test_native_format_selector_can_use_progressive_video(self):
        selected = bili2yt.SelectedFormats(video={"format_id": "18"}, audio=None)
        self.assertEqual(bili2yt.SourceExtractor._native_format_selector(selected), "18")

    def test_native_format_selector_falls_back_to_ytdlp_selection(self):
        selected = bili2yt.SelectedFormats(video={"height": 1080}, audio=None)
        self.assertEqual(
            bili2yt.SourceExtractor._native_format_selector(selected, 1080),
            "(bv*[height<=1080]/bv*)+(ba/b)",
        )

    def test_native_downloader_does_not_configure_reencoding(self):
        class FakeYDL:
            options = None

            def __init__(self, options):
                type(self).options = options

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def download(self, urls):
                Path(self.options["outtmpl"]).write_bytes(b"fake-mp4")
                return 0

        class FakeModule:
            YoutubeDL = FakeYDL

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out.mp4"
            extractor = bili2yt.SourceExtractor()
            item = bili2yt.SourceItem(
                "https://www.bilibili.com/video/BV1test",
                "Test",
                None,
                None,
                "BV1test",
                "bilibili",
                {},
            )
            selected = bili2yt.SelectedFormats(
                video={"format_id": "30280"},
                audio={"format_id": "30216"},
            )
            try:
                with patch.object(bili2yt, "require_yt_dlp", return_value=FakeModule):
                    extractor.download_native(item, selected, output, 1080, "ffmpeg")
                self.assertTrue(output.exists())
                self.assertEqual(FakeYDL.options["format"], "30280+30216")
                self.assertEqual(FakeYDL.options["merge_output_format"], "mp4")
                self.assertFalse(FakeYDL.options["skip_download"])
                self.assertNotIn("recodevideo", FakeYDL.options)
                self.assertNotIn("postprocessors", FakeYDL.options)
            finally:
                extractor.close()

    def test_youtube_popular_url(self):
        self.assertEqual(
            bili2yt.youtube_popular_source("https://www.youtube.com/@creator"),
            "https://www.youtube.com/@creator/videos?view=0&sort=p&flow=grid",
        )
        playlist = "https://www.youtube.com/playlist?list=abc123"
        self.assertEqual(bili2yt.youtube_popular_source(playlist), playlist)

    def test_format_selection_prefers_video_only_and_separate_audio(self):
        selected = bili2yt.choose_formats(
            {
                "formats": [
                    {"url": "v360", "vcodec": "avc1", "acodec": "none", "height": 360, "tbr": 800},
                    {"url": "v480", "vcodec": "avc1", "acodec": "none", "height": 480, "tbr": 1200},
                    {"url": "v720", "vcodec": "avc1", "acodec": "none", "height": 720, "tbr": 2500},
                    {"url": "a", "vcodec": "none", "acodec": "opus", "abr": 160},
                ]
            },
            480,
        )
        self.assertEqual(selected.video["url"], "v480")
        self.assertEqual(selected.audio["url"], "a")

    def test_source_key_namespaces_platforms(self):
        bili = bili2yt.SourceItem("https://bilibili.com/video/BV1", "Bili", None, None, "same", "bilibili", {})
        youtube = bili2yt.SourceItem("https://youtube.com/watch?v=same", "YouTube", None, None, "same", "youtube", {})
        self.assertNotEqual(bili.key, youtube.key)


class TestStateAndPrompt(unittest.TestCase):
    def test_state_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            item = bili2yt.SourceItem("https://example/video", "Title", 123, None, "id", "other", {})
            store = bili2yt.StateStore(path)
            store.mark_completed(item, "https://youtube.com/watch?v=abc")
            self.assertTrue(bili2yt.StateStore(path).contains(item.key))

    def test_interactive_cutoff_prompt(self):
        tty = type("TTY", (), {"isatty": lambda self: True})
        args = Namespace(playlist="Imports", min_views=None, all=False)
        with patch.object(bili2yt.sys, "stdin", tty()), patch("builtins.input", side_effect=["n", "100k"]):
            self.assertEqual(bili2yt.prompt_for_options(args), ("Imports", 100_000))


class TestManualUpload(unittest.TestCase):
    def test_manual_mode_keeps_download_and_skips_browser(self):
        item = bili2yt.SourceItem(
            "https://www.bilibili.com/video/BV1manual",
            "Manual test",
            None,
            None,
            "BV1manual",
            "bilibili",
            {},
        )

        class FakeExtractor:
            def __init__(self, *args, **kwargs):
                pass

            def discover(self, *args, **kwargs):
                return [item]

            def extract_video(self, source_item):
                return {
                    "title": source_item.title,
                    "formats": [
                        {"url": "video", "vcodec": "avc1", "acodec": "aac", "height": 360},
                    ],
                }

            def download_native(self, source_item, selected, output, max_height, ffmpeg):
                output.write_bytes(b"manual-mp4")

            def close(self):
                pass

        args = Namespace(
            sources=[item.url],
            source_file=None,
            youtube_popular=False,
            playlist="Imports",
            min_views=None,
            all=True,
            include_unknown_views=False,
            max_items=None,
            max_height=1080,
            temp_dir=None,
            allow_disk_temp=False,
            ram_drive="R:",
            ram_size="8G",
            keep_failed_file=False,
            cookies_from_browser=None,
            cookies=None,
            request_delay=1.0,
            metadata_delay=None,
            ffmpeg="ffmpeg",
            browser_command=None,
            browser_session="bili2yt-test",
            headless=False,
            state_file=None,
            no_state=True,
            force=False,
            no_source_description=False,
            stop_on_error=False,
            dry_run=False,
            manual_upload_dir=None,
        )

        with tempfile.TemporaryDirectory() as directory:
            args.manual_upload_dir = Path(directory) / "manual-uploads"
            with patch.object(bili2yt, "read_sources", return_value=[item.url]), patch.object(
                bili2yt, "prompt_for_options", return_value=("Imports", None)
            ), patch.object(bili2yt, "preflight", return_value="ffmpeg"), patch.object(
                bili2yt, "SourceExtractor", FakeExtractor
            ), patch.object(
                bili2yt, "StudioBrowser", side_effect=AssertionError("browser should be skipped")
            ):
                self.assertEqual(bili2yt.run(args), 0)

            files = list(args.manual_upload_dir.glob("*.mp4"))
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].read_bytes(), b"manual-mp4")


class TestBrowserAdapter(unittest.TestCase):
    def test_browser_command_capture_does_not_use_inherited_pipe(self):
        def fake_run(command, *, check, stdout, stderr):
            self.assertTrue(hasattr(stdout, "write"))
            stdout.write("browser output")
            return bili2yt.subprocess.CompletedProcess(command, 0)

        with patch.object(bili2yt.subprocess, "run", side_effect=fake_run):
            completed = bili2yt._command_output(["agent-browser", "open", "about:blank"], check=False)

        self.assertEqual(completed.stdout, "browser output")
        self.assertEqual(completed.returncode, 0)


class TestRamWorkspace(unittest.TestCase):
    def test_invalid_drive_is_rejected(self):
        with self.assertRaises(bili2yt.Bili2YTError):
            bili2yt.ImDiskRamWorkspace("not-a-drive", "8G")


if __name__ == "__main__":
    unittest.main()
