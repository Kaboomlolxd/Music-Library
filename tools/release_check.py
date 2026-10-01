"""Validate the local extension release artifacts before store submission."""

from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "extension"
DIST = ROOT / "dist"


def fail(message: str) -> None:
    raise SystemExit(f"release check failed: {message}")


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - command-line guard
        fail(f"{path} is not valid JSON: {exc}")
    if not isinstance(value, dict):
        fail(f"{path} must contain an object")
    return value


def check_archive(path: Path, expected_version: str, *, firefox: bool) -> None:
    if not path.is_file():
        fail(f"missing package {path}")
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        required = {
            "manifest.json", "background.js", "content.js", "options.html",
            "options.js", "icons/icon-16.png", "icons/icon-32.png",
            "icons/icon-48.png", "icons/icon-128.png",
        }
        missing = sorted(required - names)
        if missing:
            fail(f"{path.name} is missing {', '.join(missing)}")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("version") != expected_version:
            fail(f"{path.name} has manifest version {manifest.get('version')!r}, expected {expected_version!r}")
        if manifest.get("manifest_version") != 3:
            fail(f"{path.name} is not Manifest V3")
        if firefox:
            background = manifest.get("background") or {}
            if not background.get("scripts"):
                fail(f"{path.name} has no Firefox background scripts fallback")
        else:
            if not (manifest.get("background") or {}).get("service_worker"):
                fail(f"{path.name} has no Chrome service worker")
        forbidden = re.compile(r"(?:\.env|cookies|token|library\.sqlite3|__pycache__)", re.I)
        leaked = sorted(name for name in names if forbidden.search(name))
        if leaked:
            fail(f"{path.name} contains sensitive or local files: {', '.join(leaked)}")


def main() -> int:
    manifest = load_json(EXTENSION / "manifest.json")
    version = str(manifest.get("version") or "")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        fail("extension version must use major.minor.patch")
    pyproject = ROOT / "pyproject.toml"
    project_text = pyproject.read_text(encoding="utf-8")
    project_match = re.search(r"^version\s*=\s*['\"]([^'\"]+)['\"]", project_text, re.MULTILINE)
    if not project_match or project_match.group(1) != version:
        fail(f"pyproject.toml version does not match extension version {version}")
    for required_doc in (ROOT / "LICENSE", ROOT / "CHANGELOG.md", ROOT / "SECURITY.md"):
        if not required_doc.is_file() or required_doc.stat().st_size < 100:
            fail(f"missing public-release document {required_doc.name}")
    for size in (16, 32, 48, 128):
        icon = EXTENSION / "icons" / f"icon-{size}.png"
        if not icon.is_file() or icon.stat().st_size < 50:
            fail(f"missing or empty icon {icon}")
    check_archive(DIST / f"local-music-library-player-{version}-chrome.zip", version, firefox=False)
    check_archive(DIST / f"local-music-library-player-{version}-firefox-zen.xpi", version, firefox=True)
    print(f"release check passed: extension {version}, Chrome ZIP and Firefox/Zen XPI validated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
