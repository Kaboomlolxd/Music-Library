"""Provider-facing URL and metadata normalization.

Only metadata and public playback links pass through this layer.  It is kept
independent from FastAPI and SQLite so a future Spotify adapter can follow the
same contract without changing the library rules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from urllib.parse import parse_qs, urlparse


class ProviderAdapter(Protocol):
    """Minimal contract used by the importer and player queue."""

    name: str

    def accepts(self, url: str) -> bool: ...

    def canonical_id_from_url(self, url: str) -> str | None: ...

    def normalize_creator(self, metadata: Mapping[str, Any]) -> str: ...

    def playback_url(self, url: str, remote_id: str) -> str: ...


def _nonempty(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split()).strip()
    return text or None


@dataclass(frozen=True)
class YouTubeAdapter:
    name: str = "youtube"

    def accepts(self, url: str) -> bool:
        host = urlparse(url).netloc.lower().split(":")[0]
        return host == "youtu.be" or host.endswith("youtube.com")

    def canonical_id_from_url(self, url: str) -> str | None:
        parsed = urlparse(url)
        host = parsed.netloc.lower().split(":")[0]
        if host == "youtu.be":
            return parsed.path.strip("/").split("/")[0] or None
        if not host.endswith("youtube.com"):
            return None
        if parsed.path == "/watch":
            return _nonempty(parse_qs(parsed.query).get("v", [None])[0])
        match = re.match(r"^/(?:shorts|live|embed)/([^/?#]+)", parsed.path)
        return match.group(1) if match else None

    def normalize_creator(self, metadata: Mapping[str, Any]) -> str:
        """Return the per-video uploader/channel, never a playlist owner."""
        for key in (
            "uploader",
            "channel",
            "creator",
        ):
            creator = _nonempty(metadata.get(key))
            if creator:
                return creator
        return "Unknown creator"

    def playback_url(self, url: str, remote_id: str) -> str:
        return f"https://www.youtube.com/watch?v={remote_id}"


@dataclass(frozen=True)
class BilibiliAdapter:
    name: str = "bilibili"

    def accepts(self, url: str) -> bool:
        host = urlparse(url).netloc.lower().split(":")[0]
        return host.endswith("bilibili.com") or host == "b23.tv"

    def canonical_id_from_url(self, url: str) -> str | None:
        match = re.search(r"/video/((?:BV[0-9A-Za-z]+)|(?:av\d+))", url, flags=re.IGNORECASE)
        return match.group(1) if match else None

    def normalize_creator(self, metadata: Mapping[str, Any]) -> str:
        """Return the per-video uploader/channel, never a playlist owner."""
        for key in (
            "uploader",
            "channel",
            "creator",
        ):
            creator = _nonempty(metadata.get(key))
            if creator:
                return creator
        return "Unknown creator"

    def playback_url(self, url: str, remote_id: str) -> str:
        if remote_id.upper().startswith("BV"):
            return f"https://www.bilibili.com/video/{remote_id}"
        return url


ADAPTERS: tuple[ProviderAdapter, ...] = (YouTubeAdapter(), BilibiliAdapter())


def adapter_for_url(url: str) -> ProviderAdapter | None:
    return next((adapter for adapter in ADAPTERS if adapter.accepts(url)), None)


def adapter_for_name(name: str) -> ProviderAdapter | None:
    return next((adapter for adapter in ADAPTERS if adapter.name == name), None)
