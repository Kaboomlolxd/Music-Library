"""Console entry point for the loopback-only local dashboard."""

from __future__ import annotations

import argparse
import threading
import webbrowser
from pathlib import Path

import uvicorn

from .app import create_app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Start the local YouTube/Bilibili music library.")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Directory for library.sqlite3 and generated library.md/.txt exports.",
    )
    parser.add_argument("--host", default="127.0.0.1", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=8765, help="Loopback port for the local dashboard (default: 8765).")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the dashboard automatically.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("This private local app only binds to 127.0.0.1 or localhost.")
    if not 1 <= args.port <= 65535:
        raise SystemExit("Port must be between 1 and 65535.")
    # Bind to the numeric loopback address even when the user supplied
    # localhost, avoiding an accidental LAN/IPv6 listener.
    host = "127.0.0.1"
    if not args.no_browser:
        threading.Timer(
            0.8,
            lambda: webbrowser.open(f"http://{host}:{args.port}", new=1),
        ).start()
    uvicorn.run(create_app(args.data_dir), host=host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
