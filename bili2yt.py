"""Bulk Bilibili/YouTube -> YouTube Studio relay.

The program deliberately does not use the YouTube Data API. It discovers
source metadata with yt-dlp, then uses yt-dlp's native downloader to place the
selected streams into one temporary MP4 on a RAM drive. If a source has
separate video and audio streams, yt-dlp invokes ffmpeg for a stream-copy
merge/remux; this program does not re-encode media. It then drives the
signed-in YouTube Studio web uploader through agent-browser. The temporary
file is removed after each upload attempt.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

try:
    import yt_dlp
except ImportError:  # pragma: no cover - exercised by the CLI preflight
    yt_dlp = None  # type: ignore[assignment]


APP_NAME = "bili2yt"
DEFAULT_MAX_HEIGHT = 1080
DEFAULT_BROWSER_SESSION = "bili2yt"
INVALID_WINDOWS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Provider feeds are expected to terminate. A feed that emits an entry already
# seen in that same source is cycling, so discovery stops at the first repeat.
# This is deliberately content-based rather than an arbitrary scan-count cap:
# large feeds may contain far more than 10,000 legitimate entries.


class Bili2YTError(RuntimeError):
    """A user-facing, expected failure."""


class BrowserAutomationError(Bili2YTError):
    """The YouTube Studio browser adapter could not complete an action."""


@dataclasses.dataclass(frozen=True)
class SourceItem:
    """A source entry after discovery and optional view-count filtering."""

    url: str
    title: str
    view_count: int | None
    duration: float | None
    source_id: str | None
    source_kind: str
    raw: dict[str, Any]

    @property
    def key(self) -> str:
        return f"{self.source_kind}:{self.source_id}" if self.source_id else self.url


@dataclasses.dataclass(frozen=True)
class SelectedFormats:
    video: dict[str, Any]
    audio: dict[str, Any] | None


@dataclasses.dataclass
class RunOptions:
    playlist: str
    max_height: int
    min_views: int | None
    max_items: int | None
    include_unknown_views: bool
    temp_dir: Path | None
    manual_upload_dir: Path | None
    keep_failed_file: bool
    cookies_from_browser: tuple[str, str | None] | None
    ffmpeg: str
    browser_command: str
    browser_session: str
    headless: bool
    state_file: Path | None
    force: bool
    no_source_description: bool
    stop_on_error: bool


def parse_positive_int(value: str) -> int:
    try:
        parsed = int(value.replace(",", "").strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an integer: {value!r}") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def parse_nonnegative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a number: {value!r}") from exc
    if parsed < 0 or not float("-inf") < parsed < float("inf"):
        raise argparse.ArgumentTypeError("must be a finite number zero or greater")
    return parsed


def parse_view_cutoff(value: str) -> int:
    value = value.strip().lower().replace(",", "")
    suffixes = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}
    multiplier = 1
    if value and value[-1] in suffixes:
        multiplier = suffixes[value[-1]]
        value = value[:-1]
    try:
        parsed = float(value) * multiplier
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a view count: {value!r}") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("view cutoff must be zero or greater")
    return int(parsed)


def clip_text(value: str, limit: int) -> str:
    value = " ".join(value.split()).strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "\u2026"


def safe_filename(value: str, limit: int = 90) -> str:
    cleaned = INVALID_WINDOWS_CHARS.sub("_", value).strip().rstrip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned or "video")[:limit].rstrip().rstrip(".")


def parse_cookies_from_browser(value: str | None) -> tuple[str, str | None] | None:
    if not value:
        return None
    browser, separator, profile = value.partition(":")
    if not browser:
        raise argparse.ArgumentTypeError("browser name cannot be empty")
    if browser.lower() == "zen":
        browser = "firefox"
        if not profile:
            profile = _find_zen_profile()
        elif not Path(profile).is_dir():
            raise argparse.ArgumentTypeError(f"Zen profile directory does not exist: {profile}")
    return browser, profile or None


def _find_zen_profile() -> str:
    roots: list[Path] = []
    for variable in ("APPDATA", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if base:
            roots.append(Path(base) / "zen" / "Profiles")
    candidates = sorted(
        {cookie_path.parent for root in roots if root.is_dir() for cookie_path in root.rglob("cookies.sqlite")},
        key=lambda path: (path / "cookies.sqlite").stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise argparse.ArgumentTypeError(
            "Could not find a Zen Browser cookies profile. Pass firefox:FULL_PROFILE_PATH instead."
        )
    if len(candidates) > 1:
        choices = ", ".join(str(path) for path in candidates[:4])
        raise argparse.ArgumentTypeError(
            f"Found multiple Zen profiles; pass firefox:FULL_PROFILE_PATH explicitly. Found: {choices}"
        )
    return str(candidates[0])


def browser_command_path(value: str | None) -> str:
    if value:
        return value
    for candidate in ("agent-browser", "agent-browser.cmd"):
        path = shutil.which(candidate)
        if path:
            return path
    # PowerShell launched through a desktop shortcut can inherit a stale PATH
    # even though npm installed the per-user shim successfully.
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        npm_root = Path(appdata) / "npm" if appdata else Path.home() / "AppData" / "Roaming" / "npm"
        for candidate in ("agent-browser.cmd", "agent-browser.CMD", "agent-browser.exe"):
            path = npm_root / candidate
            if path.is_file():
                return str(path)
    raise Bili2YTError(
        "agent-browser was not found. Install it with `npm i -g agent-browser` "
        "and then run `agent-browser install`."
    )


def require_yt_dlp() -> Any:
    if yt_dlp is None:
        raise Bili2YTError(
            "The Python yt-dlp package is missing. Run `python -m pip install -r requirements.txt`."
        )
    return yt_dlp


class StateStore:
    """Small optional checkpoint file; no media is ever stored here."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.completed: dict[str, dict[str, Any]] = {}
        if path and path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                self.completed = dict(payload.get("completed", {}))
            except (OSError, ValueError, TypeError) as exc:
                print(f"Warning: could not read state file {path}: {exc}", file=sys.stderr)

    def contains(self, key: str) -> bool:
        return key in self.completed

    def mark_completed(self, item: SourceItem, youtube_url: str | None) -> None:
        if not self.path:
            return
        self.completed[item.key] = {
            "source_url": item.url,
            "title": item.title,
            "youtube_url": youtube_url,
            "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        self._save()

    def _save(self) -> None:
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "completed": self.completed}
        temp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_name = handle.name
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temp_name, self.path)
            temp_name = None
        finally:
            if temp_name:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass


class ImDiskRamWorkspace:
    """Create a temporary ImDisk VM-backed drive when no temp path is given."""

    def __init__(self, drive: str, size: str) -> None:
        drive = drive.strip().rstrip("\\")
        if len(drive) == 1:
            drive += ":"
        if not re.fullmatch(r"[A-Za-z]:", drive):
            raise Bili2YTError(f"RAM drive must look like R: (got {drive!r})")
        self.drive = drive.upper()
        self.size = size
        self.imdisk = shutil.which("imdisk") or shutil.which("imdisk.exe")
        self.created = False

    @property
    def root(self) -> Path:
        return Path(f"{self.drive}\\")

    @property
    def workspace(self) -> Path:
        return self.root / APP_NAME

    def _is_imdisk_mount(self) -> bool:
        if not self.imdisk:
            return False
        completed = subprocess.run(
            [self.imdisk, "-l", "-m", self.drive],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return completed.returncode == 0

    def prepare(self) -> Path:
        if not self.imdisk:
            raise Bili2YTError(
                "RAM-only mode needs ImDisk (imdisk.exe), which was not found. "
                "Install ImDisk or pass --temp-dir pointing to an existing RAM disk."
            )
        if self._is_imdisk_mount():
            if not self.root.exists():
                if not sys.stdin.isatty():
                    raise Bili2YTError(
                        f"ImDisk reports {self.drive} is mounted, but this process cannot access it. "
                        "Detach the stale RAM disk from an Administrator terminal, or run interactively "
                        "to be asked whether it should be recreated."
                    )
                answer = input(
                    f"{self.drive} is mounted in ImDisk but inaccessible here. "
                    "Detach and recreate this temporary RAM disk? [Y/n]: "
                ).strip().lower()
                if answer not in ("", "y", "yes"):
                    raise Bili2YTError(
                        f"The existing {self.drive} RAM disk was left untouched. "
                        "Choose another --ram-drive or detach it manually."
                    )
                detached = self._detach_mount()
                if detached.returncode != 0:
                    raise Bili2YTError(
                        f"Could not detach the inaccessible RAM disk {self.drive}. "
                        f"Run `imdisk -d -m {self.drive}` from an Administrator terminal.\n"
                        f"{detached.stdout[-1000:]}"
                    )
            else:
                try:
                    self.workspace.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    raise Bili2YTError(
                        f"The existing RAM disk {self.drive} is not writable ({exc}). "
                        "Run Bili2YouTube.bat as Administrator or choose another --ram-drive."
                    ) from exc
                return self.workspace
        if self.root.exists():
            raise Bili2YTError(
                f"{self.drive} is already in use but is not an ImDisk RAM disk. "
                "Choose another --ram-drive or pass --temp-dir for your existing RAM disk."
            )
        print(f"Creating {self.size} RAM disk at {self.drive} with ImDisk...")
        completed = subprocess.run(
            [
                self.imdisk,
                "-a",
                "-t",
                "vm",
                "-s",
                self.size,
                "-m",
                self.drive,
                "-p",
                "/fs:ntfs /q /y",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if completed.returncode != 0 or not self.root.exists():
            raise Bili2YTError(
                "Could not create or access the RAM disk. This normally requires an "
                "Administrator terminal, "
                f"choose another drive with --ram-drive, or create one manually.\n{completed.stdout[-2000:]}"
            )
        self.created = True
        try:
            self.workspace.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise Bili2YTError(
                f"The RAM disk was attached at {self.drive}, but its workspace could not be opened ({exc}). "
                "Run Bili2YouTube.bat as Administrator."
            ) from exc
        return self.workspace

    def _detach_mount(self, *, force: bool = False) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.imdisk, "-D" if force else "-d", "-m", self.drive],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def cleanup(self) -> None:
        if not self.created or not self.imdisk:
            return
        completed = self._detach_mount()
        if completed.returncode != 0:
            # This disk was created by this run. If normal filesystem
            # shutdown failed after Ctrl+C or a browser crash, force-remove
            # the volatile device so it does not strand 8 GB of RAM.
            forced = self._detach_mount(force=True)
            if forced.returncode == 0:
                print(f"RAM disk {self.drive} force-detached after normal cleanup failed.", file=sys.stderr)
            else:
                print(
                    f"Warning: could not detach RAM disk {self.drive}. "
                    f"It may remain mounted until you run `imdisk -D -m {self.drive}`.",
                    file=sys.stderr,
                )

    @classmethod
    def is_imdisk_path(cls, path: Path) -> bool:
        if os.name != "nt":
            return False
        drive, _ = os.path.splitdrive(str(path))
        if not drive:
            return False
        imdisk = shutil.which("imdisk") or shutil.which("imdisk.exe")
        if not imdisk:
            return False
        completed = subprocess.run(
            [imdisk, "-l", "-m", drive],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return completed.returncode == 0


def _looks_bilibili_block(value: Any) -> bool:
    text = str(value).lower()
    return any(token in text for token in ("bilibili", "412", "401", "352")) and any(
        token in text for token in ("blocked", "rejected", "please wait", "request is", "http error")
    )


def _looks_transient_source_block(value: Any) -> bool:
    text = str(value).lower()
    return any(
        token in text
        for token in (
            "http error 412",
            "http error 401",
            "http error 429",
            "http error 500",
            "http error 502",
            "http error 503",
            "temporarily",
            "please wait",
            "timed out",
            "timeout",
            "connection reset",
            "transporterror",
        )
    )


class SourceExtractor:
    """yt-dlp-backed discovery and per-video metadata/format extraction."""

    def __init__(
        self,
        cookies: tuple[str, str | None] | None = None,
        cookies_file: Path | None = None,
        *,
        request_delay: float = 1.0,
        metadata_delay: float | None = None,
        retries: int = 3,
    ) -> None:
        require_yt_dlp()
        self.cookies = cookies
        self.cookies_file = cookies_file
        self.request_delay = max(0.0, request_delay)
        # View-count filtering should still be polite, but using the full
        # media/source delay for every metadata request makes long lists feel
        # stuck. Keep this bounded by default; callers can opt into a stricter
        # value with --metadata-delay when needed.
        self.metadata_delay = (
            min(self.request_delay, 0.25)
            if metadata_delay is None
            else max(0.0, metadata_delay)
        )
        self.retries = max(1, retries)
        self._ydl_cache: dict[tuple[str, bool, bool, float, int | None], Any] = {}

    def _options(
        self,
        url: str,
        *,
        flat: bool,
        single: bool = False,
        request_delay: float | None = None,
        playlist_end: int | None = None,
    ) -> dict[str, Any]:
        site_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
        }
        if "bilibili.com" in url.lower() or "b23.tv" in url.lower():
            site_headers["Referer"] = "https://www.bilibili.com/"
        elif "youtube.com" in url.lower() or "youtu.be" in url.lower():
            site_headers["Referer"] = "https://www.youtube.com/"
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": False,
            "skip_download": True,
            "cachedir": False,
            "extract_flat": flat,
            # YouTube channel/playlist pages can contain hundreds or
            # thousands of entries. Process entries as they arrive instead
            # of waiting for yt-dlp to materialize the whole playlist first.
            # This affects discovery memory/latency only; it does not bypass
            # provider limits or anti-bot checks.
            "lazy_playlist": flat,
            "noplaylist": single,
            "http_headers": site_headers,
            "retries": self.retries,
            "fragment_retries": self.retries,
            "extractor_retries": self.retries,
            "sleep_interval_requests": self.request_delay if request_delay is None else request_delay,
            "socket_timeout": 30,
            "concurrent_fragment_downloads": 1,
        }
        if flat and playlist_end is not None:
            options["playlistend"] = max(1, playlist_end)
        if self.cookies:
            browser, profile = self.cookies
            options["cookiesfrombrowser"] = (browser, profile, None, None)
        if self.cookies_file:
            options["cookiefile"] = str(self.cookies_file)
        return options

    def _get_ydl(
        self,
        url: str,
        *,
        flat: bool,
        single: bool,
        request_delay: float | None,
        playlist_end: int | None = None,
    ) -> Any:
        delay = self.request_delay if request_delay is None else max(0.0, request_delay)
        key = (self._source_kind(url), flat, single, delay, playlist_end if flat else None)
        ydl = self._ydl_cache.get(key)
        if ydl is None:
            module = require_yt_dlp()
            ydl = module.YoutubeDL(
                self._options(
                    url,
                    flat=flat,
                    single=single,
                    request_delay=delay,
                    playlist_end=playlist_end,
                )
            )
            self._ydl_cache[key] = ydl
        return ydl

    def close(self) -> None:
        """Release cached yt-dlp sessions, including any cookie resources."""
        cached = tuple(self._ydl_cache.values())
        self._ydl_cache.clear()
        for ydl in cached:
            try:
                ydl.close()
            except Exception:
                pass

    def _extract(
        self,
        url: str,
        *,
        flat: bool,
        single: bool = False,
        request_delay: float | None = None,
        playlist_end: int | None = None,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                ydl = self._get_ydl(
                    url,
                    flat=flat,
                    single=single,
                    request_delay=request_delay,
                    playlist_end=playlist_end,
                )
                info = ydl.extract_info(url, download=False)
                if not info:
                    raise Bili2YTError(f"No metadata was returned for {url}")
                return dict(info)
            except Bili2YTError as exc:
                last_error = exc
            except Exception as exc:  # yt-dlp uses many extractor-specific exception types
                last_error = exc
            if attempt < self.retries and _looks_transient_source_block(last_error):
                delay = min(30.0, 2.0 ** (attempt - 1))
                print(
                    f"  source request was temporarily rejected; retrying in {delay:g}s "
                    f"({attempt}/{self.retries - 1})...",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue
            break
        assert last_error is not None
        detail = str(last_error)
        if _looks_bilibili_block(detail):
            detail += (
                " Try again later or pass --cookies-from-browser zen/chrome/edge with the "
                "Bilibili browser profile you are authorized to use."
            )
        if "failed to decrypt with dpapi" in detail.lower():
            detail += (
                " Windows could not decrypt the browser profile; close the browser and "
                "try again, or export an authorized Netscape cookies.txt file and pass "
                "it with --cookies."
            )
        raise Bili2YTError(f"Could not inspect {url}: {detail}") from last_error

    @staticmethod
    def _entry_url(entry: dict[str, Any]) -> str | None:
        for key in ("webpage_url", "original_url", "url"):
            candidate = entry.get(key)
            if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
                return candidate
        entry_id = entry.get("id")
        ie_key = str(entry.get("ie_key") or entry.get("ie") or "").lower()
        if entry_id and "bili" in ie_key:
            return f"https://www.bilibili.com/video/{entry_id}"
        if entry_id and "youtube" in ie_key:
            return f"https://www.youtube.com/watch?v={entry_id}"
        return None

    @staticmethod
    def _view_count(info: dict[str, Any]) -> int | None:
        value = info.get("view_count", info.get("viewCount"))
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _source_kind(url: str) -> str:
        lowered = url.lower()
        if "bilibili.com" in lowered or "b23.tv" in lowered:
            return "bilibili"
        if "youtube.com" in lowered or "youtu.be" in lowered:
            return "youtube"
        return "other"

    @staticmethod
    def _is_creator_feed(url: str) -> bool:
        """Whether a source's owner can safely stand in for every entry.

        A YouTube playlist can contain videos from many channels, so its
        playlist owner must never become the entry creator. Channel Videos
        pages are different: their root creator is the entry creator when a
        flat entry omits it. The Bilibili equivalent is an uploader's
        `/upload/video` feed; series/favorites/list URLs are not assumed to
        contain only one creator.
        """
        parsed = urlsplit(url)
        host = parsed.netloc.lower().split(":")[0]
        path = parsed.path.rstrip("/").lower()
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if host == "youtu.be":
            return False
        if host.endswith("youtube.com"):
            return (
                not query.get("list")
                and (
                    path.startswith("/@")
                    or path.startswith("/channel/")
                    or path.startswith("/user/")
                    or path.startswith("/c/")
                )
            )
        if host.endswith("bilibili.com"):
            return path.endswith("/upload/video")
        return False

    def _fast_bilibili_metadata(self, url: str) -> dict[str, Any] | None:
        """Read basic Bilibili metadata without resolving video formats.

        The normal Bilibili video extractor also resolves play URLs and format
        lists. That is unnecessary during a view-count filter pass, so use the
        public view endpoint first and let the normal extractor remain the
        fallback for unusual/private/blocked entries.
        """
        match = re.search(r"/video/(BV[0-9A-Za-z]+)", url)
        if not match:
            return None
        bvid = match.group(1)
        module = require_yt_dlp()
        ydl = self._get_ydl(
            url,
            flat=True,
            single=True,
            request_delay=self.metadata_delay,
        )
        request = module.networking.Request(
            f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
            headers=self._options(url, flat=True, single=True)["http_headers"],
        )
        response = ydl.urlopen(request)
        try:
            payload = json.loads(response.read().decode("utf-8", "replace"))
        finally:
            response.close()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return None
        stat = data.get("stat")
        if not isinstance(stat, dict) or self._view_count({"view_count": stat.get("view")}) is None:
            return None
        return {
            "id": data.get("bvid") or bvid,
            "title": data.get("title"),
            "view_count": stat.get("view"),
            "duration": data.get("duration"),
            "uploader": ((data.get("owner") or {}).get("name") if isinstance(data.get("owner"), dict) else None),
            "uploader_id": ((data.get("owner") or {}).get("mid") if isinstance(data.get("owner"), dict) else None),
            "uploader_url": (
                f"https://space.bilibili.com/{data['owner'].get('mid')}"
                if isinstance(data.get("owner"), dict) and data["owner"].get("mid")
                else None
            ),
            "webpage_url": url,
        }

    def _fast_bilibili_series_metadata(self, source: str, root: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Fetch a Bilibili series in pages, without resolving video formats.

        The generic flat Bilibili series extractor often yields only BV IDs.
        This public series endpoint returns up to 30 records per request with
        the title, duration, and view count. It is an optional enrichment:
        callers retain the normal yt-dlp/single-video fallback if the endpoint
        changes, is unavailable, or an entry is absent from its result.
        """
        parsed = urlsplit(source)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if query.get("type") != "series":
            return {}
        season_id = query.get("series_id") or query.get("season_id")
        uploader_id = root.get("uploader_id") or root.get("channel_id")
        if not season_id and isinstance(root.get("id"), str) and "_" in root["id"]:
            _, season_id = root["id"].rsplit("_", 1)
        if not season_id or not uploader_id:
            return {}

        ydl = self._get_ydl(
            source,
            flat=True,
            single=True,
            request_delay=self.metadata_delay,
        )
        headers = self._options(source, flat=True, single=True)["http_headers"]
        metadata: dict[str, dict[str, Any]] = {}
        page_number = 1
        total: int | None = None
        while total is None or (page_number - 1) * 30 < total:
            endpoint = "https://api.bilibili.com/x/polymer/web-space/seasons_archives_list?"
            endpoint += urlencode(
                {
                    "mid": str(uploader_id),
                    "season_id": str(season_id),
                    "page_num": page_number,
                    "page_size": 30,
                }
            )
            response = ydl.urlopen(require_yt_dlp().networking.Request(endpoint, headers=headers))
            try:
                payload = json.loads(response.read().decode("utf-8", "replace"))
            finally:
                response.close()
            data = payload.get("data") if isinstance(payload, dict) else None
            archives = data.get("archives") if isinstance(data, dict) else None
            page = data.get("page") if isinstance(data, dict) else None
            if not isinstance(archives, list):
                break
            for archive in archives:
                if not isinstance(archive, dict) or not archive.get("bvid"):
                    continue
                stat = archive.get("stat") if isinstance(archive.get("stat"), dict) else {}
                metadata[str(archive["bvid"])] = {
                    "id": str(archive["bvid"]),
                    "title": archive.get("title"),
                    "view_count": stat.get("view", archive.get("play")),
                    "duration": archive.get("duration"),
                    "uploader": archive.get("author") or root.get("uploader") or root.get("channel"),
                    "uploader_id": archive.get("mid") or root.get("uploader_id") or root.get("channel_id"),
                }
            try:
                total = int(page.get("total")) if isinstance(page, dict) and page.get("total") is not None else len(archives)
            except (TypeError, ValueError):
                total = len(archives)
            if not archives:
                break
            page_number += 1
            if total is None or (page_number - 1) * 30 < total:
                time.sleep(self.metadata_delay)
        return metadata

    def discover(
        self,
        sources: Sequence[str],
        *,
        min_views: int | None,
        include_unknown_views: bool,
        max_items: int | None,
        enrich_missing_metadata: bool = False,
    ) -> list[SourceItem]:
        found: list[SourceItem] = []
        seen: set[str] = set()
        last_bilibili_metadata_at = 0.0
        for source in sources:
            print(f"Inspecting source: {source}")
            # A requested maximum can be passed to yt-dlp only when there is
            # no view cutoff. With a cutoff, later entries may be the ones
            # that qualify, so capping source enumeration would be incorrect.
            root = self._extract(
                source,
                flat=True,
                playlist_end=max_items if min_views is None else None,
            )
            source_is_creator_feed = self._is_creator_feed(source)
            entries = root.get("entries")
            if entries is None:
                entries = [root]
            series_metadata: dict[str, dict[str, Any]] = {}
            if self._source_kind(source) == "bilibili":
                try:
                    series_metadata = self._fast_bilibili_series_metadata(source, root)
                    if series_metadata:
                        print(
                            f"  enriched {len(series_metadata):,} Bilibili series entries from batched public metadata."
                        )
                except Exception:
                    # Treat the public endpoint as a speed optimization, not
                    # a discovery dependency. Some series/list types require
                    # cookies or use a different endpoint.
                    series_metadata = {}
            source_seen_urls: set[str] = set()
            source_seen_unusable: set[str] = set()
            for raw_entry in entries:
                if not isinstance(raw_entry, dict):
                    fingerprint = f"{type(raw_entry).__name__}:{raw_entry!r}"
                    if fingerprint in source_seen_unusable:
                        print(
                            f"  warning: stopped {source} after a repeated unusable entry; "
                            "the provider returned a looping feed.",
                            file=sys.stderr,
                        )
                        break
                    source_seen_unusable.add(fingerprint)
                    continue
                url = self._entry_url(raw_entry)
                if not url:
                    fingerprint = json.dumps(raw_entry, sort_keys=True, default=str)
                    if fingerprint in source_seen_unusable:
                        print(
                            f"  warning: stopped {source} after a repeated unusable entry; "
                            "the provider returned a looping feed.",
                            file=sys.stderr,
                        )
                        break
                    source_seen_unusable.add(fingerprint)
                    continue
                if url in source_seen_urls:
                    print(
                        f"  warning: stopped {source} after repeated entry {url}; "
                        "the provider returned a looping feed.",
                        file=sys.stderr,
                    )
                    break
                source_seen_urls.add(url)
                # The same video can legitimately occur in more than one
                # requested source. Deduplicate those cross-source entries
                # without treating them as a provider loop.
                if url in seen:
                    continue
                seen.add(url)
                metadata = {
                    **raw_entry,
                }
                # A channel/upload feed can safely inherit its owner only when
                # an entry omits its own uploader. Playlist owners are never
                # copied onto their entries because playlists mix creators.
                if source_is_creator_feed and not any(metadata.get(key) for key in ("uploader", "channel", "creator")):
                    entry_creator = next(
                        (
                            root.get(key)
                            for key in (
                                "uploader",
                                "channel",
                                "creator",
                            )
                            if root.get(key)
                        ),
                        None,
                    )
                    if entry_creator:
                        metadata["uploader"] = entry_creator
                    for key in ("uploader_url", "channel_url"):
                        if root.get(key) and not metadata.get(key):
                            metadata[key] = root[key]
                entry_id = str(metadata.get("id") or "")
                if entry_id in series_metadata:
                    metadata = {**metadata, **series_metadata[entry_id]}
                needs_view_count = min_views is not None and self._view_count(metadata) is None
                needs_bilibili_basics = (
                    self._source_kind(url) == "bilibili"
                    and (not metadata.get("title") or str(metadata.get("title")) == entry_id)
                )
                needs_creator = enrich_missing_metadata and not any(
                    metadata.get(key)
                    for key in ("uploader", "channel", "creator", "uploader_id", "channel_id")
                )
                if needs_view_count or needs_bilibili_basics or needs_creator:
                    fast_metadata = None
                    if self._source_kind(url) == "bilibili":
                        try:
                            # Unlike YouTube flat channel entries, Bilibili
                            # often gives only a BV ID. Pace these lightweight
                            # public metadata requests so importing a large
                            # Bilibili list remains respectful.
                            remaining_delay = self.metadata_delay - (time.monotonic() - last_bilibili_metadata_at)
                            if remaining_delay > 0:
                                time.sleep(remaining_delay)
                            fast_metadata = self._fast_bilibili_metadata(url)
                            last_bilibili_metadata_at = time.monotonic()
                        except Exception:
                            # The lightweight endpoint is an optimization,
                            # not a new failure mode; use yt-dlp below when it
                            # is unavailable or returns an unexpected shape.
                            fast_metadata = None
                    try:
                        if fast_metadata:
                            metadata = {**raw_entry, **fast_metadata}
                        else:
                            # Flat playlist entries are intentionally cheap but
                            # often omit view_count. Fetch metadata without
                            # media as a fallback so the cutoff is meaningful.
                            metadata = {
                                **raw_entry,
                                **self._extract(
                                    url,
                                    flat=False,
                                    single=True,
                                    request_delay=self.metadata_delay,
                                ),
                            }
                    except Bili2YTError as exc:
                        print(f"  warning: could not read metadata for {url}: {exc}", file=sys.stderr)
                view_count = self._view_count(metadata)
                if min_views is not None and view_count is None:
                    # A cutoff with unknown view count is excluded unless the user opts in.
                    if not include_unknown_views:
                        print(f"  skip (view count unavailable): {url}")
                        continue
                if min_views is not None and view_count is not None and view_count < min_views:
                    print(f"  skip ({view_count:,} views): {metadata.get('title') or url}")
                    continue
                title = str(metadata.get("title") or metadata.get("id") or url)
                item = SourceItem(
                    url=url,
                    title=title,
                    view_count=view_count,
                    duration=_as_float(metadata.get("duration")),
                    source_id=str(metadata.get("id")) if metadata.get("id") else None,
                    source_kind=self._source_kind(url),
                    raw=metadata,
                )
                found.append(item)
                print(
                    f"  keep {len(found):>4}: {clip_text(item.title, 80)}"
                    + (f" [{view_count:,} views]" if view_count is not None else "")
                )
                if max_items is not None and len(found) >= max_items:
                    return found
        return found

    def extract_video(self, item: SourceItem) -> dict[str, Any]:
        # Re-extract immediately before download so expiring media URLs are fresh.
        return self._extract(item.url, flat=False, single=True)

    @staticmethod
    def _native_format_selector(
        selected: SelectedFormats,
        max_height: int = DEFAULT_MAX_HEIGHT,
    ) -> str:
        video_id = selected.video.get("format_id")
        if not video_id:
            # This is only a fallback for unusual extractors that do not give
            # formats stable IDs. yt-dlp still performs the actual selection.
            return f"(bv*[height<={max_height}]/bv*)+(ba/b)"
        if selected.audio:
            audio_id = selected.audio.get("format_id")
            if not audio_id:
                return f"(bv*[height<={max_height}]/bv*)+(ba/b)"
            return f"{video_id}+{audio_id}"
        return str(video_id)

    def download_native(
        self,
        item: SourceItem,
        selected: SelectedFormats,
        output: Path,
        max_height: int,
        ffmpeg: str,
    ) -> None:
        """Download selected formats through yt-dlp into the RAM output path.

        This is especially important for Bilibili DASH responses. The native
        downloader honors fragment boundaries, retries, and HTTP range
        behavior; handing a raw expiring URL straight to ffmpeg can produce a
        truncated MP4 that looks like corrupt H.264.
        """
        format_selector = self._native_format_selector(selected, max_height)
        module = require_yt_dlp()
        options = self._options(
            item.url,
            flat=False,
            single=True,
            request_delay=self.request_delay,
        )
        options.update(
            {
                "skip_download": False,
                "format": format_selector,
                "outtmpl": str(output),
                "merge_output_format": "mp4",
                "overwrites": True,
                "continuedl": True,
                "noprogress": True,
                # yt-dlp uses ffmpeg only for stream merging/remuxing here;
                # no recodevideo/postprocessor is configured.
                "ffmpeg_location": ffmpeg,
            }
        )
        try:
            with module.YoutubeDL(options) as ydl:
                result = ydl.download([item.url])
        except Exception as exc:
            raise Bili2YTError(f"yt-dlp native download failed for {item.url}: {exc}") from exc
        if result not in (None, 0):
            raise Bili2YTError(f"yt-dlp native download failed for {item.url} (exit code {result})")
        if not output.exists() or output.stat().st_size == 0:
            raise Bili2YTError("yt-dlp native download finished without creating a non-empty MP4")


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _format_has_video(fmt: dict[str, Any]) -> bool:
    return fmt.get("vcodec") not in (None, "none") and bool(fmt.get("url"))


def _format_has_audio(fmt: dict[str, Any]) -> bool:
    return fmt.get("acodec") not in (None, "none") and bool(fmt.get("url"))


def _format_rank(fmt: dict[str, Any]) -> tuple[float, float, float, float]:
    return (
        float(fmt.get("height") or 0),
        float(fmt.get("fps") or 0),
        float(fmt.get("tbr") or fmt.get("vbr") or 0),
        float(fmt.get("filesize") or fmt.get("filesize_approx") or 0),
    )


def choose_formats(info: dict[str, Any], max_height: int) -> SelectedFormats:
    formats = [fmt for fmt in info.get("formats", []) if isinstance(fmt, dict)]
    if not formats:
        raise Bili2YTError("The extractor returned no downloadable formats")

    video_candidates = [fmt for fmt in formats if _format_has_video(fmt)]
    bounded_video = [
        fmt
        for fmt in video_candidates
        if (fmt.get("height") is None or float(fmt.get("height") or 0) <= max_height)
    ]
    # Prefer a video-only stream so we can pair it with the highest-quality audio.
    video_only = [fmt for fmt in bounded_video if not _format_has_audio(fmt)]
    if video_only:
        video = max(video_only, key=_format_rank)
    elif bounded_video:
        video = max(bounded_video, key=_format_rank)
    else:
        video = max(video_candidates, key=_format_rank)

    audio_candidates = [fmt for fmt in formats if _format_has_audio(fmt) and not _format_has_video(fmt)]
    audio = max(audio_candidates, key=_format_rank) if audio_candidates else None
    return SelectedFormats(video=video, audio=audio)


def make_temp_output(temp_dir: Path | None, item: SourceItem) -> Path:
    if temp_dir:
        temp_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix=f"{safe_filename(item.title, 45)}-",
        suffix=".mp4",
        dir=str(temp_dir) if temp_dir else None,
    )
    os.close(fd)
    path = Path(name)
    path.unlink(missing_ok=True)
    return path


def prepare_manual_upload_dir(path: Path) -> Path:
    """Create and validate the persistent directory used for manual uploads."""
    path = path.expanduser()
    if path.exists() and not path.is_dir():
        raise Bili2YTError(f"Manual upload path is not a directory: {path}")
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise Bili2YTError(f"Could not create manual upload directory {path}: {exc}") from exc
    return path


def _command_output(command: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    # agent-browser starts a persistent daemon from the short-lived CLI
    # process. On Windows the daemon can inherit stdout, so PIPE/communicate()
    # waits for the daemon instead of returning when the CLI command finishes.
    # Capture into a tiny temporary log file instead; media is still written
    # only to the RAM workspace.
    try:
        # NamedTemporaryFile uses delete-on-close semantics on Windows. The
        # daemon may retain the inherited handle, but the path is still
        # removed as soon as this short-lived command closes its handle.
        with tempfile.NamedTemporaryFile(
            mode="w+",
            encoding="utf-8",
            errors="replace",
            newline="",
            prefix="bili2yt-browser-",
            suffix=".log",
            delete=True,
        ) as log:
            completed = subprocess.run(
                list(command),
                check=False,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            log.flush()
            log.seek(0)
            output = log.read()
    except OSError as exc:
        raise BrowserAutomationError(f"Could not start {command[0]}: {exc}") from exc
    completed = subprocess.CompletedProcess(
        completed.args,
        completed.returncode,
        output,
        None,
    )
    if check and completed.returncode != 0:
        raise BrowserAutomationError(
            f"Browser command failed ({completed.returncode}): {' '.join(command)}\n{completed.stdout[-4000:]}"
        )
    return completed


class StudioBrowser:
    """A deliberately conservative adapter around the agent-browser CLI.

    YouTube Studio is a changing web app.  CSS fallbacks and a manual recovery
    prompt are intentional: if Studio changes a selector, the user can finish
    the visible step without losing the entire job.
    """

    def __init__(self, command: str, session: str, *, headed: bool) -> None:
        self.command = command
        self.session = session
        self.headed = headed
        self.restore_supported = self._detect_restore_support()

    def _detect_restore_support(self) -> bool:
        try:
            completed = subprocess.run(
                [self.command, "--help"],
                check=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except OSError:
            return False
        return "--restore" in (completed.stdout or "")

    def _prefix(self) -> list[str]:
        command = [self.command, "--session", self.session]
        if self.headed:
            command.append("--headed")
        if self.restore_supported:
            command.append("--restore")
        return command

    def _run_with_recovery(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        completed = _command_output(command, check=False)
        output = completed.stdout or ""
        if completed.returncode == 0 or "daemon failed to start" not in output.lower():
            return completed

        # A previous interrupted run can leave a dead session socket. Ask the
        # CLI to close that session, then retry the original command once.
        close_command = [self.command, "--session", self.session, "close"]
        _command_output(close_command, check=False)
        retried = _command_output(command, check=False)
        if retried.returncode == 0 or "daemon failed to start" not in (retried.stdout or "").lower():
            return retried

        # If the old CLI cannot clear a dead socket, isolate this run in a new
        # session rather than repeatedly failing on the same stale endpoint.
        recovery_session = f"{self.session}-recovery-{int(time.time())}"
        print(
            f"agent-browser session {self.session!r} has a dead daemon socket; "
            f"retrying as {recovery_session!r}.",
            file=sys.stderr,
        )
        self.session = recovery_session
        recovered_command = list(command)
        try:
            session_index = recovered_command.index("--session") + 1
            recovered_command[session_index] = recovery_session
        except (ValueError, IndexError):
            recovered_command = self._prefix() + recovered_command[1:]
        return _command_output(recovered_command, check=False)

    def _run(self, *args: str, check: bool = True) -> str:
        command = self._prefix() + list(args)
        completed = self._run_with_recovery(command)
        if check and completed.returncode != 0:
            raise BrowserAutomationError(
                f"Browser command failed ({completed.returncode}): {' '.join(command)}\n"
                f"{(completed.stdout or '')[-4000:]}"
            )
        return completed.stdout or ""

    def _click_text(self, labels: Iterable[str], *, required: bool = False) -> bool:
        labels = tuple(labels)
        for label in labels:
            command = self._prefix() + ["find", "text", label, "click", "--exact"]
            completed = _command_output(command, check=False)
            if completed.returncode == 0:
                return True
        if required:
            raise BrowserAutomationError(f"Could not click visible text: {labels!r}")
        return False

    def _click_css(self, selectors: Iterable[str], *, required: bool = False) -> bool:
        for selector in selectors:
            command = self._prefix() + ["click", selector]
            completed = _command_output(command, check=False)
            if completed.returncode == 0:
                return True
        if required:
            raise BrowserAutomationError(f"Could not click selector: {list(selectors)!r}")
        return False

    def _fill_css(self, selectors: Iterable[str], value: str, *, required: bool = False) -> bool:
        for selector in selectors:
            command = self._prefix() + ["fill", selector, value]
            completed = _command_output(command, check=False)
            if completed.returncode == 0:
                return True
        if required:
            raise BrowserAutomationError(f"Could not fill selector: {list(selectors)!r}")
        return False

    def ensure_signed_in(self) -> None:
        self._run("open", "https://studio.youtube.com")
        self._run("wait", "--load", "domcontentloaded", check=False)
        url = self._run("get", "url", check=False).strip().lower()
        if "accounts.google.com" in url or "signin" in url or "login" in url:
            print("\nA browser window is open for Google sign-in/2FA.")
            input("Finish signing in, return to YouTube Studio, then press Enter here: ")
            self._run("open", "https://studio.youtube.com")
            self._run("wait", "--load", "domcontentloaded", check=False)

    def _open_upload_dialog(self) -> None:
        # Direct navigation is less fragile when Studio exposes the upload route.
        self._run("open", "https://studio.youtube.com")
        self._run("wait", "--load", "domcontentloaded", check=False)
        if self._click_text(("Create", "CREATE")):
            self._run("wait", "--text", "Upload videos", check=False)
            if self._click_text(("Upload videos", "Upload video")):
                return
        # The menu route has changed over time; this is a safe fallback.
        self._run("open", "https://studio.youtube.com/video/upload")
        self._run("wait", "--load", "domcontentloaded", check=False)

    def _wait_for_file_input(self) -> None:
        self._run(
            "wait",
            "--fn",
            "document.querySelector('input[type=file]') !== null",
        )

    def _wait_for_details(self) -> None:
        self._run(
            "wait",
            "--fn",
            "Boolean(document.querySelector(\"#title-textarea, [aria-label='Add a title that describes your video'], input[aria-label*='title' i], [contenteditable='true']\"))",
        )

    def _set_playlist(self, playlist: str) -> None:
        opened = self._click_css(
            (
                "#playlist-picker",
                "ytcp-video-metadata-editor-basics #playlist-picker",
                "button[aria-label*='playlist' i]",
            )
        )
        if not opened:
            opened = self._click_text(("Playlists", "Select playlists"))
        if not opened:
            raise BrowserAutomationError("Could not open the YouTube Studio playlist picker")
        self._run("wait", "--text", playlist, check=False)
        if self._click_text((playlist,)):
            return
        created = self._click_text(("Create new playlist", "New playlist", "Create playlist"))
        if not created:
            raise BrowserAutomationError(
                "The playlist was not found and the Create playlist action was not visible"
            )
        if not self._fill_css(
            (
                "input[placeholder*='playlist' i]",
                "input[aria-label*='playlist' i]",
                "#title-textarea",
                "input[type=text]",
            ),
            playlist,
            required=True,
        ):
            raise BrowserAutomationError("Could not enter the new playlist name")
        self._click_text(("Create", "Done"), required=True)
        self._run("wait", "--text", playlist, check=False)
        self._click_text((playlist,), required=True)

    def _set_title(self, title: str) -> None:
        self._fill_css(
            (
                "#title-textarea",
                "ytcp-uploads-dialog #title-textarea",
                "[aria-label='Add a title that describes your video']",
                "input[aria-label*='title' i]",
                "[contenteditable='true']",
            ),
            clip_text(title, 100),
            required=True,
        )

    def _set_audience(self) -> None:
        # Studio requires a made-for-kids choice; this importer does not infer
        # it, so choose the neutral "No" option and leave other settings alone.
        if not self._click_text(("No, it's not made for kids", "No, it’s not made for kids")):
            self._click_css(
                (
                    "#made-for-kids-no",
                    "input[name='VIDEO_MADE_FOR_KIDS'][value='VIDEO_MADE_FOR_KIDS_NOT_MFK']",
                ),
                required=True,
            )

    def _next(self, expected_text: str) -> None:
        if not self._click_css(("#next-button", "button[aria-label='Next']")):
            self._click_text(("Next",), required=True)
        self._run("wait", "--text", expected_text)

    def upload(self, file_path: Path, *, title: str, playlist: str, description: str) -> str | None:
        self.ensure_signed_in()
        self._open_upload_dialog()
        self._wait_for_file_input()
        upload_command = self._prefix() + ["upload", "input[type=file]", str(file_path)]
        uploaded = _command_output(upload_command, check=False)
        if uploaded.returncode != 0:
            raise BrowserAutomationError(f"Could not attach {file_path}:\n{uploaded.stdout[-4000:]}")

        self._wait_for_details()
        try:
            self._set_title(title)
            if description:
                self._fill_css(("#description-textarea", "textarea[aria-label*='description' i]"), description)
            self._set_playlist(playlist)
            self._set_audience()
            for expected_text in ("Video elements", "Checks", "Visibility"):
                self._next(expected_text)
            if not self._click_text(("Unlisted",)):
                self._click_css(("input[value='UNLISTED']", "#privacy-radios input:nth-of-type(2)"), required=True)
            if not self._click_css(("#done-button", "#publish-button")):
                self._click_text(("Save", "Publish"), required=True)
        except BrowserAutomationError as exc:
            print("\nThe YouTube Studio page is still open for recovery.")
            print("If the UI changed, finish the visible title/playlist/unlisted steps manually.")
            print(f"Automation detail: {exc}")
            input("After the video is saved as unlisted, press Enter here: ")

        self._run("wait", "--text", "Video published", check=False)
        body = self._run("get", "text", "body", check=False)
        match = re.search(r"https?://(?:www\.)?youtube\.com/watch\?v=[A-Za-z0-9_-]+", body)
        if not match and not re.search(r"\b(?:video published|video saved|saved)\b", body, re.IGNORECASE):
            raise BrowserAutomationError("Could not confirm that YouTube Studio saved the upload")
        return match.group(0) if match else None


def source_description(item: SourceItem, *, disabled: bool) -> str:
    if disabled:
        return ""
    return f"Imported from: {item.url}"


def youtube_popular_source(url: str) -> str:
    """Turn a YouTube channel URL into its web Popular tab URL."""
    parts = urlsplit(url)
    if "youtube.com" not in parts.netloc.lower() or "/watch" in parts.path:
        return url
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if "list" in query:
        return url
    path = parts.path.rstrip("/")
    if not path.endswith("/videos"):
        if path.startswith("/@") or "/channel/" in path or "/user/" in path or "/c/" in path:
            path += "/videos"
        else:
            return url
    query.update({"view": "0", "sort": "p", "flow": "grid"})
    return urlunsplit((parts.scheme, parts.netloc, path, urlencode(query), parts.fragment))


def read_sources(
    positional: Sequence[str],
    source_file: Path | None,
    *,
    youtube_popular: bool = False,
) -> list[str]:
    sources = list(positional)
    if source_file:
        try:
            for line in source_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    sources.append(line)
        except OSError as exc:
            raise Bili2YTError(f"Could not read source file {source_file}: {exc}") from exc
    if youtube_popular:
        sources = [youtube_popular_source(source) for source in sources]
    return sources


def prompt_for_options(args: argparse.Namespace) -> tuple[str, int | None]:
    playlist = args.playlist
    if not playlist:
        if not sys.stdin.isatty():
            raise Bili2YTError("Pass --playlist when stdin is not interactive")
        playlist = input("YouTube playlist name (existing or new): ").strip()
    if not playlist:
        raise Bili2YTError("Playlist name cannot be empty")

    min_views = args.min_views
    if min_views is None and not args.all and sys.stdin.isatty():
        while True:
            answer = input("Import all videos? [Y/n]: ").strip().lower()
            if answer in ("", "y", "yes"):
                break
            if answer in ("n", "no"):
                while True:
                    cutoff = input("Minimum views cutoff (examples: 100k, 1.5m): ").strip()
                    try:
                        min_views = parse_view_cutoff(cutoff)
                        break
                    except argparse.ArgumentTypeError as exc:
                        print(f"Please enter a valid view count: {exc}", file=sys.stderr)
                break
            print("Please answer yes or no.")
    return playlist, min_views


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description="Filter Bilibili/YouTube lists, relay one video at a time, and upload unlisted to YouTube Studio.",
    )
    parser.add_argument("sources", nargs="*", help="Bilibili/YouTube list, channel, playlist, or video URL")
    parser.add_argument("--source-file", type=Path, help="UTF-8 file containing one source URL per line")
    parser.add_argument("--youtube-popular", action="store_true", help="Use the Popular tab for YouTube channel sources")
    parser.add_argument("--playlist", help="YouTube playlist to use or create")
    parser.add_argument("--all", action="store_true", help="Do not prompt for a view cutoff")
    parser.add_argument("--min-views", type=parse_view_cutoff, help="Skip videos below this count; accepts 10k/1.5m")
    parser.add_argument("--include-unknown-views", action="store_true", help="Keep entries whose view count cannot be read")
    parser.add_argument("--max-items", type=parse_positive_int, help="Maximum videos to process after filtering")
    parser.add_argument("--max-height", type=parse_positive_int, default=DEFAULT_MAX_HEIGHT, help=f"Maximum video height (default: {DEFAULT_MAX_HEIGHT})")
    parser.add_argument("--temp-dir", type=Path, help="Existing RAM-disk directory override; default creates an ImDisk VM disk")
    parser.add_argument("--allow-disk-temp", action="store_true", help="Allow --temp-dir on a non-RAM drive (debugging only)")
    parser.add_argument("--ram-drive", default="R:", help="Drive letter for the automatic ImDisk RAM disk (default: R:)")
    parser.add_argument("--ram-size", default="8G", help="Size of the automatic ImDisk RAM disk (default: 8G)")
    parser.add_argument("--keep-failed-file", action="store_true", help="Keep a temp MP4 if its upload fails")
    parser.add_argument(
        "--manual-upload-dir",
        type=Path,
        metavar="PATH",
        help="Save MP4s here for manual drag-and-drop/file-picker upload; skip browser automation",
    )
    parser.add_argument("--cookies-from-browser", metavar="BROWSER[:PROFILE]", help="Pass browser cookies to yt-dlp for source access")
    parser.add_argument("--cookies", type=Path, help="Netscape-format cookies.txt exported from an authorized browser session")
    parser.add_argument("--request-delay", type=parse_nonnegative_float, default=1.0, help="Seconds between source requests (default: 1)")
    parser.add_argument(
        "--metadata-delay",
        type=parse_nonnegative_float,
        help="Seconds between requests while checking view counts (default: min(--request-delay, 0.25))",
    )
    parser.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg executable or full path")
    parser.add_argument("--browser-command", help="agent-browser executable or full path")
    parser.add_argument("--browser-session", default=DEFAULT_BROWSER_SESSION, help="Persistent agent-browser session name")
    parser.add_argument("--headless", action="store_true", help="Use headless browser (only after a saved sign-in session exists)")
    parser.add_argument("--state-file", type=Path, default=Path("bili2yt-state.json"), help="Small resume ledger; use --no-state to disable")
    parser.add_argument("--no-state", action="store_true", help="Do not save completed source IDs")
    parser.add_argument("--force", action="store_true", help="Re-upload entries already in the state ledger")
    parser.add_argument("--no-source-description", action="store_true", help="Do not add the source URL to the YouTube description")
    parser.add_argument("--stop-on-error", action="store_true", help="Stop after the first failed video")
    parser.add_argument("--dry-run", action="store_true", help="Discover and filter only; do not download or upload")
    return parser


def preflight(args: argparse.Namespace) -> str | None:
    require_yt_dlp()
    if args.dry_run:
        return None
    ffmpeg = shutil.which(args.ffmpeg) or (args.ffmpeg if Path(args.ffmpeg).exists() else None)
    if not ffmpeg:
        raise Bili2YTError(
            f"ffmpeg was not found ({args.ffmpeg!r}). Install ffmpeg and put it on PATH, or pass --ffmpeg PATH."
        )
    return ffmpeg


def run(args: argparse.Namespace) -> int:
    sources = read_sources(args.sources, args.source_file, youtube_popular=args.youtube_popular)
    if not sources:
        raise Bili2YTError("Give at least one source URL or use --source-file")
    playlist, min_views = prompt_for_options(args)
    ffmpeg = preflight(args)
    cookies = parse_cookies_from_browser(args.cookies_from_browser)
    if args.cookies and not args.cookies.is_file():
        raise Bili2YTError(f"Cookie file does not exist: {args.cookies}")
    if args.cookies and cookies:
        raise Bili2YTError("Use either --cookies-from-browser or --cookies, not both")
    extractor = SourceExtractor(
        cookies,
        args.cookies,
        request_delay=args.request_delay,
        metadata_delay=args.metadata_delay,
    )
    try:
        items = extractor.discover(
            sources,
            min_views=min_views,
            include_unknown_views=args.include_unknown_views,
            max_items=args.max_items,
        )
    finally:
        # Discovery and media extraction may use different yt-dlp options.
        # Close the metadata session before opening the browser/upload phase;
        # media extraction will lazily create a fresh session when needed.
        extractor.close()
    print(f"\nSelected {len(items)} video(s).")
    if args.dry_run:
        return 0
    assert ffmpeg is not None
    ram_workspace: ImDiskRamWorkspace | None = None
    manual_upload_dir = None
    if args.manual_upload_dir is not None:
        if args.temp_dir is not None:
            raise Bili2YTError("Do not combine --manual-upload-dir with --temp-dir")
        if args.headless:
            raise Bili2YTError("Do not combine --manual-upload-dir with --headless")
        manual_upload_dir = prepare_manual_upload_dir(args.manual_upload_dir)
        temp_dir = manual_upload_dir
        print(f"Manual upload mode: downloaded MP4s will remain in {manual_upload_dir}")
    else:
        temp_dir = args.temp_dir
        if temp_dir is not None and not args.allow_disk_temp and not ImDiskRamWorkspace.is_imdisk_path(temp_dir):
            raise Bili2YTError(
                f"Temporary path {temp_dir} is not on an ImDisk RAM drive. "
                "Omit --temp-dir for automatic RAM mode, choose an ImDisk path, or use "
                "--allow-disk-temp only for debugging."
            )
        if temp_dir is None:
            ram_workspace = ImDiskRamWorkspace(args.ram_drive, args.ram_size)
            try:
                temp_dir = ram_workspace.prepare()
            except Exception:
                ram_workspace.cleanup()
                raise
    failures = 0
    try:
        browser = None
        if manual_upload_dir is None:
            browser = StudioBrowser(
                browser_command_path(args.browser_command),
                args.browser_session,
                headed=not args.headless,
            )
        state = StateStore(None if args.no_state else args.state_file)
        options = RunOptions(
            playlist=playlist,
            max_height=args.max_height,
            min_views=min_views,
            max_items=args.max_items,
            include_unknown_views=args.include_unknown_views,
            temp_dir=temp_dir,
            manual_upload_dir=manual_upload_dir,
            keep_failed_file=args.keep_failed_file,
            cookies_from_browser=cookies,
            ffmpeg=ffmpeg,
            browser_command=browser.command if browser is not None else "",
            browser_session=args.browser_session,
            headless=args.headless,
            state_file=None if args.no_state else args.state_file,
            force=args.force,
            no_source_description=args.no_source_description,
            stop_on_error=args.stop_on_error,
        )
        for index, item in enumerate(items, start=1):
            if not options.force and state.contains(item.key):
                print(f"[{index}/{len(items)}] already completed: {clip_text(item.title, 80)}")
                continue
            output: Path | None = None
            failed = False
            manual_file_ready = False
            try:
                print(f"\n[{index}/{len(items)}] extracting: {clip_text(item.title, 100)}")
                info = extractor.extract_video(item)
                selected = choose_formats(info, options.max_height)
                output = make_temp_output(options.temp_dir, item)
                if options.manual_upload_dir is not None:
                    print(f"  saving downloaded MP4 for manual upload: {output}")
                else:
                    print(f"  relaying to RAM-backed temporary file: {output}")
                print("  using yt-dlp native downloader (no video/audio re-encode)")
                extractor.download_native(
                    item,
                    selected,
                    output,
                    options.max_height,
                    options.ffmpeg,
                )
                manual_file_ready = options.manual_upload_dir is not None
                if options.manual_upload_dir is not None:
                    print(f"  ready for manual upload: {output}")
                    print("  drag this MP4 into YouTube Studio or choose it with the file picker")
                    continue
                actual_title = str(info.get("title") or item.title)
                assert browser is not None
                youtube_url = browser.upload(
                    output,
                    title=actual_title,
                    playlist=options.playlist,
                    description=source_description(item, disabled=options.no_source_description),
                )
                state.mark_completed(item, youtube_url)
                print(f"  uploaded unlisted to playlist {options.playlist!r}" + (f": {youtube_url}" if youtube_url else ""))
            except (Bili2YTError, OSError, ValueError) as exc:
                failures += 1
                failed = True
                print(f"  FAILED: {exc}", file=sys.stderr)
                if options.stop_on_error or isinstance(exc, BrowserAutomationError):
                    break
            finally:
                if output and output.exists():
                    if manual_file_ready:
                        print(f"  keeping manual-upload file: {output}")
                    elif failed and options.keep_failed_file:
                        print(f"  keeping failed RAM temporary file: {output}", file=sys.stderr)
                    else:
                        try:
                            output.unlink()
                            print("  temporary file deleted")
                        except OSError as exc:
                            print(f"  warning: could not delete {output}: {exc}", file=sys.stderr)
        print(f"\nFinished with {failures} failure(s).")
        return 1 if failures else 0
    finally:
        extractor.close()
        if ram_workspace:
            if args.keep_failed_file and failures:
                print(
                    f"RAM disk {ram_workspace.drive} left mounted because --keep-failed-file was used. "
                    f"Detach it later with `imdisk -d -m {ram_workspace.drive}`.",
                    file=sys.stderr,
                )
            else:
                ram_workspace.cleanup()


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Bili2YTError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
