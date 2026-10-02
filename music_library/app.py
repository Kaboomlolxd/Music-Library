"""Local FastAPI application for the metadata-only music library."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import secrets
import threading
import uuid
import webbrowser
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, Mapping

from fastapi import Body, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect, WebSocketException, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .database import LibraryDatabase
from .importer import ImportOptions, LibraryImporter
from .providers import adapter_for_name, adapter_for_url
from .queueing import true_shuffle, variety_shuffle
from .services import MetadataService, PlaylistService, RelationshipService
import bili2yt
from dataclasses import dataclass


STATIC_DIR = Path(__file__).with_name("static")


class JobInterrupted(Exception):
    def __init__(self, requested_status: str) -> None:
        self.requested_status = requested_status
        super().__init__(requested_status)


@dataclass
class AnalysisResult:
    suggestions: list[dict[str, Any]]
    created: int

    def as_dict(self) -> dict[str, Any]:
        return {"suggestions": self.suggestions, "created": self.created}


class EventHub:
    """Small in-process WebSocket hub; local Uvicorn uses one worker."""

    def __init__(self) -> None:
        self.ui_connections: set[WebSocket] = set()
        self.extension_connections: dict[str, WebSocket] = {}
        self.active_profile: str | None = None

    async def connect_ui(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.ui_connections.add(websocket)

    def disconnect_ui(self, websocket: WebSocket) -> None:
        self.ui_connections.discard(websocket)

    async def broadcast_ui(self, message: Mapping[str, Any]) -> None:
        disconnected: list[WebSocket] = []
        for websocket in tuple(self.ui_connections):
            try:
                await websocket.send_json(dict(message))
            except (RuntimeError, WebSocketDisconnect):
                disconnected.append(websocket)
        for websocket in disconnected:
            self.ui_connections.discard(websocket)

    async def connect_extension(self, profile: str, websocket: WebSocket) -> None:
        older = self.extension_connections.get(profile)
        self.extension_connections[profile] = websocket
        self.active_profile = profile
        if older and older is not websocket:
            with suppress(RuntimeError):
                await older.close(code=status.WS_1012_SERVICE_RESTART)

    def disconnect_extension(self, profile: str, websocket: WebSocket) -> None:
        if self.extension_connections.get(profile) is websocket:
            self.extension_connections.pop(profile, None)
        if self.active_profile == profile:
            self.active_profile = next(reversed(self.extension_connections), None) if self.extension_connections else None

    async def player_command(self, command: str, **data: Any) -> bool:
        """Target only one recently paired browser profile, never every tab."""
        if not self.active_profile:
            return False
        websocket = self.extension_connections.get(self.active_profile)
        if websocket is None:
            return False
        try:
            await websocket.send_json({"type": "command", "command": command, **data})
            return True
        except (RuntimeError, WebSocketDisconnect):
            self.disconnect_extension(self.active_profile, websocket)
            return False


def _error(value: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(value))


def _queue_command_payload(queue: Mapping[str, Any]) -> dict[str, Any]:
    current = queue.get("current")
    return {
        "track": current,
        "queue": queue,
        "can_navigate": bool(current),
    }

def _player_command_data(track: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "url": track["url"],
        "track_id": track["id"],
        "provider": track["provider"],
        "expected_duration": track.get("duration"),
    }


def _same_track(current: Mapping[str, Any] | None, *, provider: str | None, url: str | None) -> bool:
    if not current or not provider or not url:
        return False
    if current.get("provider") != provider:
        return False
    adapter = adapter_for_name(provider)
    if adapter is None:
        return False
    remote_id = adapter.canonical_id_from_url(url)
    return bool(remote_id and remote_id == current.get("remote_id"))


def create_app(data_dir: Path | None = None) -> FastAPI:
    database = LibraryDatabase(data_dir)
    importer = LibraryImporter(database)
    hub = EventHub()
    jobs: dict[str, dict[str, Any]] = {}
    job_tasks: dict[str, asyncio.Task[Any]] = {}
    job_cancel: dict[str, threading.Event] = {}
    job_requested_status: dict[str, str] = {}
    provider_job_lock = asyncio.Lock()
    active_subscription_syncs: set[int] = set()
    export_task: asyncio.Task[None] | None = None
    subscription_task: asyncio.Task[None] | None = None
    subscription_interval_seconds = 3 * 60 * 60

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        nonlocal subscription_task
        # Interrupted provider requests require an explicit resume, while work
        # that never started is safely rehydrated under its original identity.
        for persisted in database.list_jobs():
            if persisted.get("status") == "running":
                database.update_job(str(persisted["id"]), status="paused", error="Application restarted; resume when ready")
            elif persisted.get("status") == "queued":
                try:
                    resume_persisted_job(str(persisted["id"]))
                except Exception as exc:
                    database.update_job(
                        str(persisted["id"]), status="paused",
                        error=f"Could not recover queued job: {exc}",
                    )
        subscription_task = asyncio.create_task(subscription_loop())
        try:
            yield
        finally:
            if subscription_task and not subscription_task.done():
                subscription_task.cancel()
                with suppress(asyncio.CancelledError):
                    await subscription_task
            # Let completed jobs finish their final export before closing the
            # shared connection. Running provider workers receive a cooperative
            # pause signal; a bounded wait prevents shutdown from hanging.
            for job_id, task in tuple(job_tasks.items()):
                if not task.done():
                    job_requested_status[job_id] = "paused"
                    control = job_cancel.get(job_id)
                    if control:
                        control.set()
            pending_jobs = [task for task in tuple(job_tasks.values()) if not task.done()]
            if pending_jobs:
                _, still_running = await asyncio.wait(pending_jobs, timeout=10)
                for task in still_running:
                    task.cancel()
                if still_running:
                    await asyncio.gather(*still_running, return_exceptions=True)
            if export_task and not export_task.done():
                export_task.cancel()
                with suppress(asyncio.CancelledError):
                    await export_task
            database.close()

    app = FastAPI(title="Local Music Library", version="0.5.1", lifespan=lifespan)
    app.state.library_db = database
    app.state.event_hub = hub
    app.state.metadata_service = MetadataService(database)
    app.state.playlist_service = PlaylistService(database)
    app.state.relationship_service = RelationshipService(database)

    async def emit_state(reason: str) -> None:
        await hub.broadcast_ui(
            {
                "type": "library_changed",
                "reason": reason,
                "stats": database.stats(),
                "queue": database.queue_state(preview_limit=50),
                "extension_sessions": database.extension_sessions(),
            }
        )

    async def command_current(*, command: str = "navigate", open_fallback: bool = False) -> dict[str, Any]:
        queue = database.queue_state(preview_limit=50)
        current = queue["current"]
        if current is None:
            raise HTTPException(status_code=400, detail="The queue is empty")
        sent = await hub.player_command(command, **_player_command_data(current))
        opened = False
        if open_fallback and not sent:
            # Without the extension there is no in-app media player. Open the
            # public provider URL in the user's default browser instead.
            opened = bool(await asyncio.to_thread(webbrowser.open, str(current["url"]), new=2))
        if sent or opened:
            database.record_current_play(reason="extension_play" if sent else "browser_play")
        await emit_state("queue")
        return {
            **_queue_command_payload(queue),
            "extension_command_sent": sent,
            "opened_in_browser": opened,
        }

    async def schedule_export() -> None:
        nonlocal export_task
        if export_task and not export_task.done():
            export_task.cancel()

        async def delayed_export() -> None:
            try:
                await asyncio.sleep(0.75)
                await asyncio.to_thread(database.export_library)
                await hub.broadcast_ui({"type": "exports_updated"})
            except asyncio.CancelledError:
                raise

        export_task = asyncio.create_task(delayed_export())

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "stats": database.stats(),
            "pairing_token": database.extension_token(),
            "extension_sessions": database.extension_sessions(),
            "queue": database.queue_state(preview_limit=50),
        }

    @app.get("/api/creators")
    async def list_creators() -> list[dict[str, Any]]:
        return database.list_creators()

    @app.get("/api/track-status")
    async def track_status(url: str) -> dict[str, Any]:
        adapter = adapter_for_url(url)
        if adapter is None:
            return {"saved": False, "supported": False}
        remote_id = adapter.canonical_id_from_url(url)
        if not remote_id:
            return {"saved": False, "supported": True}
        return {"supported": True, **database.track_status(provider=adapter.name, remote_id=remote_id)}

    @app.post("/api/track-status-batch")
    async def track_status_batch(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        urls = payload.get("urls")
        if not isinstance(urls, list):
            raise HTTPException(status_code=400, detail="'urls' must be a list")
        results: dict[str, Any] = {}
        for raw_url in urls[:100]:
            url = str(raw_url or "").strip()
            adapter = adapter_for_url(url)
            if adapter is None:
                continue
            remote_id = adapter.canonical_id_from_url(url)
            if remote_id:
                results[url] = {"supported": True, **database.track_status(provider=adapter.name, remote_id=remote_id)}
        return {"results": results}

    @app.post("/api/extension/save")
    async def extension_save(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            url = " ".join(str(payload.get("url") or "").split()).strip()
            adapter = adapter_for_url(url)
            if adapter is None:
                raise ValueError("Only YouTube and Bilibili video URLs can be saved")
            remote_id = adapter.canonical_id_from_url(url)
            if not remote_id:
                raise ValueError("The page does not contain a recognizable video ID")
            title = " ".join(str(payload.get("title") or "").split()).strip() or remote_id
            creator = " ".join(str(payload.get("creator") or payload.get("uploader") or "").split()).strip() or None
            playlist_name = (
                "Bilibili browser saves"
                if adapter.name == "bilibili"
                else "YouTube browser saves"
            )
            playlists = database.list_playlists()
            playlist = next((item for item in playlists if item["name"].casefold() == playlist_name.casefold()), None)
            if playlist is None:
                playlist = database.create_playlist(playlist_name)
            track = database.upsert_track(
                provider=adapter.name,
                remote_id=remote_id,
                url=adapter.playback_url(url, remote_id),
                title=title,
                creator=creator,
                uploader=creator,
                view_count=int(payload["view_count"]) if payload.get("view_count") is not None else None,
                duration=float(payload["duration"]) if payload.get("duration") is not None else None,
                source={
                    "saved_from_extension": True,
                    "uploader_url": payload.get("creator_url"),
                },
            )
            database.add_track_to_playlist(int(playlist["id"]), int(track["id"]))
            await asyncio.to_thread(database.export_library)
            await emit_state("extension_track_saved")
            return {"saved": True, "playlist_name": playlist["name"], **database.track_status(
                provider=adapter.name,
                remote_id=remote_id,
            )}
        except (TypeError, ValueError) as exc:
            raise _error(exc) from exc

    @app.get("/api/playlists")
    async def list_playlists(include_archived: bool = False) -> list[dict[str, Any]]:
        return database.list_playlists(include_archived=include_archived)

    @app.post("/api/playlists")
    async def create_playlist(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            kind = str(payload.get("kind") or "manual")
            if kind not in {"manual", "smart"}:
                raise ValueError("Playlist kind must be 'manual' or 'smart'")
            playlist = database.create_playlist(
                str(payload.get("name") or ""),
                kind=kind,
                query=payload.get("query") if isinstance(payload.get("query"), dict) else {},
            )
            await asyncio.to_thread(database.export_library)
            await emit_state("playlist_created")
            return playlist
        except ValueError as exc:
            raise _error(exc) from exc

    @app.post("/api/playlists/{playlist_id}")
    async def update_playlist(playlist_id: int, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            playlist = database.update_playlist(
                playlist_id,
                name=str(payload["name"]) if "name" in payload else None,
                query=payload.get("query") if isinstance(payload.get("query"), dict) else None,
            )
            await asyncio.to_thread(database.export_library)
            await emit_state("playlist_updated")
            return playlist
        except ValueError as exc:
            raise _error(exc) from exc

    @app.post("/api/playlists/{playlist_id}/archive")
    async def archive_playlist(playlist_id: int, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        database.archive_playlist(playlist_id, archived=bool(payload.get("archived", True)))
        await asyncio.to_thread(database.export_library)
        await emit_state("playlist_archived")
        return database.get_playlist(playlist_id)

    @app.post("/api/playlists/{playlist_id}/snapshot")
    async def snapshot_playlist(playlist_id: int, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        try:
            return database.snapshot_playlist(playlist_id, name=str(payload.get("name") or "") or None)
        except ValueError as exc:
            raise _error(exc) from exc

    @app.get("/api/playlists/{playlist_id}/snapshots")
    async def playlist_snapshots(playlist_id: int) -> list[dict[str, Any]]:
        try:
            return database.list_playlist_snapshots(playlist_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/playlist-snapshots/{snapshot_id}/restore")
    async def restore_playlist_snapshot(snapshot_id: int) -> dict[str, Any]:
        try:
            result = database.restore_playlist_snapshot(snapshot_id)
            await asyncio.to_thread(database.export_library)
            await emit_state("playlist_snapshot_restored")
            return result
        except ValueError as exc:
            raise _error(exc) from exc

    @app.post("/api/playlists/set-operation")
    async def playlist_set_operation(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            result = database.playlist_set_operation(payload.get("playlist_ids") or [], operation=str(payload.get("operation") or "union"), name=str(payload.get("name") or "Combined playlist"))
            await asyncio.to_thread(database.export_library)
            await emit_state("playlist_set_operation")
            return result
        except (TypeError, ValueError) as exc:
            raise _error(exc) from exc

    @app.post("/api/playlists/{playlist_id}/sort")
    async def sort_playlist(playlist_id: int, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        try:
            result = database.sort_playlist(playlist_id, field=str(payload.get("field") or "title"), reverse=bool(payload.get("reverse", False)))
            await asyncio.to_thread(database.export_library)
            await emit_state("playlist_sorted")
            return result
        except ValueError as exc:
            raise _error(exc) from exc

    @app.get("/api/tracks")
    async def list_tracks(
        q: str = "",
        creator: str = "",
        uploader: str = "",
        playlist_id: int | None = None,
        include_hidden: bool = False,
        min_rating: float | None = None,
        provider: str | None = None,
        sort: str = "creator",
        order: str = "asc",
        artist: str = "",
        album: str = "",
        genre: str = "",
        availability: str | None = None,
        min_duration: float | None = Query(default=None, ge=0),
        max_duration: float | None = Query(default=None, ge=0),
        min_views: int | None = Query(default=None, ge=0),
        max_views: int | None = Query(default=None, ge=0),
        has_override: bool | None = None,
        limit: int = Query(default=250, ge=1, le=2000),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        return {
            "items": database.list_tracks(
                query=q,
                creator=uploader or creator,
                playlist_id=playlist_id,
                include_hidden=include_hidden,
                min_rating=min_rating,
                provider=provider,
                artist=artist,
                album=album,
                genre=genre,
                availability=availability,
                min_duration=min_duration,
                max_duration=max_duration,
                min_views=min_views,
                max_views=max_views,
                has_override=has_override,
                sort=sort,
                order=order,
                limit=limit,
                offset=offset,
            ),
            "total": database.track_count(
                query=q,
                creator=uploader or creator,
                playlist_id=playlist_id,
                include_hidden=include_hidden,
                min_rating=min_rating,
                provider=provider,
                artist=artist,
                album=album,
                genre=genre,
                availability=availability,
                min_duration=min_duration,
                max_duration=max_duration,
                min_views=min_views,
                max_views=max_views,
                has_override=has_override,
            ),
            "limit": limit,
            "offset": offset,
        }

    @app.get("/api/playlists/{playlist_id}/tracks")
    async def playlist_tracks(
        playlist_id: int,
        q: str = "",
        creator: str = "",
        uploader: str = "",
        provider: str | None = None,
        min_rating: float | None = None,
        sort: str = "position",
        order: str = "asc",
        artist: str = "",
        album: str = "",
        genre: str = "",
        availability: str | None = None,
        min_duration: float | None = Query(default=None, ge=0),
        max_duration: float | None = Query(default=None, ge=0),
        min_views: int | None = Query(default=None, ge=0),
        max_views: int | None = Query(default=None, ge=0),
        has_override: bool | None = None,
        limit: int = Query(default=250, ge=1, le=2000),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        try:
            page = database.playlist_track_page(
                playlist_id,
                query=q,
                creator=uploader or creator,
                provider=provider,
                artist=artist,
                album=album,
                genre=genre,
                availability=availability,
                min_duration=min_duration,
                max_duration=max_duration,
                min_views=min_views,
                max_views=max_views,
                has_override=has_override,
                min_rating=min_rating,
                sort=sort,
                order=order,
                limit=limit,
                offset=offset,
            )
            return {**page, "limit": limit, "offset": offset}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/playlists/{playlist_id}/tracks")
    async def add_track_to_playlist(playlist_id: int, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            database.get_playlist(playlist_id)
            database.get_track(int(payload["track_id"]))
            membership, added = database.add_track_to_playlist(
                playlist_id,
                int(payload["track_id"]),
                allow_duplicate=bool(payload.get("allow_duplicate", False)),
            )
            await asyncio.to_thread(database.export_library)
            await emit_state("membership_added")
            return {"membership": membership, "added": added}
        except (KeyError, TypeError, ValueError) as exc:
            raise _error(exc) from exc

    @app.post("/api/playlists/{playlist_id}/tracks/bulk")
    async def add_tracks_to_playlist(playlist_id: int, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            track_ids = payload.get("track_ids")
            if not isinstance(track_ids, list):
                raise ValueError("track_ids must be a list")
            result = database.add_tracks_to_playlist(
                playlist_id,
                track_ids,
                allow_duplicate=bool(payload.get("allow_duplicate", False)),
            )
            await asyncio.to_thread(database.export_library)
            await emit_state("memberships_added")
            return {"playlist_id": playlist_id, **result}
        except (TypeError, ValueError) as exc:
            raise _error(exc) from exc

    @app.post("/api/playlists/{playlist_id}/tracks/remove-selected")
    async def remove_selected_tracks(playlist_id: int, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            track_ids = payload.get("track_ids")
            if not isinstance(track_ids, list):
                raise ValueError("track_ids must be a list")
            removed = database.remove_memberships_for_tracks(playlist_id, track_ids)
            await asyncio.to_thread(database.export_library)
            await emit_state("memberships_removed")
            return {"playlist_id": playlist_id, "removed": removed}
        except (TypeError, ValueError) as exc:
            raise _error(exc) from exc

    def import_options(payload: Mapping[str, Any]) -> ImportOptions:
        try:
            raw_min_views = payload.get("min_views")
            min_views = (
                bili2yt.parse_view_cutoff(str(raw_min_views))
                if raw_min_views not in (None, "")
                else None
            )
            options = ImportOptions(
                min_views=min_views,
                include_unknown_views=bool(payload.get("include_unknown_views", False)),
                max_items=int(payload["max_items"]) if payload.get("max_items") not in (None, "") else None,
                cookies_from_browser=str(payload["cookies_from_browser"]).strip()
                if payload.get("cookies_from_browser")
                else None,
                cookies_file=str(payload["cookies_file"]).strip() if payload.get("cookies_file") else None,
                request_delay=float(payload.get("request_delay", 1.0)),
                metadata_delay=float(payload["metadata_delay"])
                if payload.get("metadata_delay") not in (None, "")
                else None,
                retries=int(payload.get("retries", 3)),
                youtube_popular=bool(payload.get("youtube_popular", False)),
                refresh_views=bool(payload.get("refresh_views", False)),
                sync_policy=str(payload.get("sync_policy") or "append_only").strip().lower(),
            )
            if options.min_views is not None and options.min_views < 0:
                raise ValueError("Minimum views must be zero or greater")
            if options.max_items is not None and options.max_items < 1:
                raise ValueError("Maximum items must be at least 1")
            if options.sync_policy not in {
                "append_only", "mirror", "mirror_preserve_local_removals"
            }:
                raise ValueError("Unsupported source synchronization policy")
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(str(exc)) from exc
        return options

    def start_import_job(
        *,
        label: str,
        worker: Any,
        source_urls: list[str] | None = None,
        options: Mapping[str, Any] | None = None,
        job_type: str = "import",
        existing_job_id: str | None = None,
    ) -> dict[str, Any]:
        job_id = existing_job_id or uuid.uuid4().hex
        if job_id in job_tasks and not job_tasks[job_id].done():
            raise ValueError("The job is still stopping; retry resume in a moment")
        if existing_job_id:
            database.get_job(job_id)
            database.update_job(
                job_id,
                status="queued",
                current_source=None,
                current_item=None,
                completed_count=0,
                total_count=0,
                next_retry_at=None,
                error_class=None,
                error=None,
                finished_at=None,
                result=None,
            )
            database.add_job_log(job_id, "Job resumed under its original identity")
        else:
            database.create_job(job_id, job_type=job_type, label=label, source_urls=source_urls or [], options=options or {})
        jobs[job_id] = database.get_job(job_id)
        job_cancel[job_id] = threading.Event()
        asyncio.create_task(hub.broadcast_ui({
            "type": "job_resumed" if existing_job_id else "job_created",
            "job_id": job_id,
            "job": jobs[job_id],
        }))

        loop = asyncio.get_running_loop()

        def worker_progress(message: dict[str, Any]) -> None:
            control = job_cancel.get(job_id)
            if control and control.is_set():
                raise JobInterrupted(job_requested_status.get(job_id, "cancelled"))
            payload = {"job_id": job_id, **message}
            try:
                database.update_job(
                    job_id,
                    current_source=message.get("source"),
                    current_item=message.get("title"),
                    completed_count=int(message.get("completed") or 0),
                    total_count=int(message.get("discovered_count") or 0),
                )
            except Exception:
                pass
            asyncio.run_coroutine_threadsafe(hub.broadcast_ui(payload), loop)
            asyncio.run_coroutine_threadsafe(
                hub.broadcast_ui({**payload, "type": "job_progress"}), loop
            )

        async def run_job() -> None:
            database.update_job(job_id, status="running", started_at=dt.datetime.now(dt.timezone.utc).isoformat())
            jobs[job_id] = database.get_job(job_id)
            await hub.broadcast_ui({"type": "job_started", "job_id": job_id})
            try:
                async with provider_job_lock:
                    result = await asyncio.to_thread(worker, worker_progress)
                database.update_job(job_id, status="completed", finished_at=dt.datetime.now(dt.timezone.utc).isoformat(), result=result.as_dict())
                jobs[job_id] = database.get_job(job_id)
                await asyncio.to_thread(database.export_library)
                await hub.broadcast_ui(
                    {"type": "job_finished", "job_id": job_id, "result": result.as_dict()}
                )
                await emit_state("import_finished")
            except JobInterrupted as exc:
                final_status = exc.requested_status if exc.requested_status in {"paused", "cancelled"} else "cancelled"
                database.update_job(
                    job_id, status=final_status,
                    finished_at=dt.datetime.now(dt.timezone.utc).isoformat() if final_status == "cancelled" else None,
                    error=None,
                )
                await hub.broadcast_ui({"type": f"job_{final_status}", "job_id": job_id})
            except Exception as exc:
                requested = job_requested_status.get(job_id)
                if requested in {"paused", "cancelled"}:
                    database.update_job(job_id, status=requested, error=None)
                    await hub.broadcast_ui({"type": f"job_{requested}", "job_id": job_id})
                    return
                database.update_job(job_id, status="failed", finished_at=dt.datetime.now(dt.timezone.utc).isoformat(), error=str(exc), error_class="provider")
                jobs[job_id] = database.get_job(job_id)
                await hub.broadcast_ui(
                    {"type": "job_failed", "job_id": job_id, "error": str(exc)}
                )
            finally:
                job_tasks.pop(job_id, None)
                job_cancel.pop(job_id, None)
                job_requested_status.pop(job_id, None)

        job_tasks[job_id] = asyncio.create_task(run_job())
        return database.get_job(job_id)

    def relation_scan_worker(progress: Any, *, limit: int = 500, min_confidence: float = 0.65) -> AnalysisResult:
        progress({"stage": "scanning_relations", "completed": 0, "discovered_count": 0, "title": "Indexing metadata"})
        suggestions = database.suggest_track_relations(limit=limit, min_confidence=min_confidence)
        created = database.save_relation_suggestions(suggestions)
        for suggestion in suggestions:
            try:
                relation = next((item for item in database.list_relations(limit=2000)
                                 if int(item["from_track_id"]) == int(suggestion["from_track_id"])
                                 and int(item["to_track_id"]) == int(suggestion["to_track_id"])
                                 and item["relation_type"] == suggestion["relation_type"]), None)
                if relation:
                    database.create_review_item(kind="track_relation", relation_id=int(relation["id"]), payload=suggestion)
            except Exception:
                continue
        progress({"stage": "relations_saved", "completed": len(suggestions), "discovered_count": len(suggestions), "title": "Relation scan complete"})
        return AnalysisResult(suggestions=suggestions, created=created)

    def start_relation_scan_job(*, limit: int = 500, min_confidence: float = 0.65, existing_job_id: str | None = None) -> dict[str, Any]:
        return start_import_job(
            label="relation_scan",
            source_urls=["local://metadata"],
            options={"limit": limit, "min_confidence": min_confidence},
            worker=lambda progress: relation_scan_worker(progress, limit=limit, min_confidence=min_confidence),
            job_type="relation_scan",
            existing_job_id=existing_job_id,
        )

    def resume_persisted_job(job_id: str) -> dict[str, Any]:
        """Rebuild a durable provider worker without allocating another job ID."""
        job = database.get_job(job_id)
        if job.get("job_type") == "relation_scan":
            options = dict(job.get("options") or {})
            return start_relation_scan_job(
                limit=int(options.get("limit", 500)),
                min_confidence=float(options.get("min_confidence", 0.65)),
                existing_job_id=job_id,
            )
        urls = list(job.get("source_urls") or [])
        options = dict(job.get("options") or {})
        if not urls:
            return database.update_job(job_id, status="completed", finished_at=dt.datetime.now(dt.timezone.utc).isoformat())
        playlist_id = options.get("target_playlist_id")
        if playlist_id is None:
            raise ValueError("The persisted job has no destination playlist")
        parsed = import_options(options)
        subscription_id = (
            int(options["subscription_id"])
            if options.get("subscription_id") is not None else None
        )

        def generic_worker(progress: Any) -> Any:
            try:
                result = importer.import_urls(
                    urls,
                    target_playlist_id=int(playlist_id),
                    options=parsed,
                    progress=progress,
                )
                if subscription_id is not None:
                    database.update_subscription(
                        subscription_id,
                        last_sync_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                        last_error=None,
                    )
                return result
            except Exception as exc:
                if subscription_id is not None:
                    database.update_subscription(
                        subscription_id,
                        last_sync_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                        last_error=str(exc),
                    )
                raise
            finally:
                if subscription_id is not None:
                    active_subscription_syncs.discard(subscription_id)

        if job.get("job_type") == "source_refresh" and options.get("source_id") is not None:
            worker = lambda progress: importer.refresh_source(int(options["source_id"]), progress=progress)
        else:
            worker = generic_worker
        if subscription_id is not None:
            active_subscription_syncs.add(subscription_id)
        try:
            return start_import_job(
                label=job.get("label") or str(job.get("job_type") or "import"),
                source_urls=urls,
                options=options,
                worker=worker,
                job_type=str(job.get("job_type") or "import"),
                existing_job_id=job_id,
            )
        except Exception:
            if subscription_id is not None:
                active_subscription_syncs.discard(subscription_id)
            raise

    def start_subscription_sync(subscription_id: int) -> dict[str, Any]:
        if subscription_id in active_subscription_syncs:
            return {"id": None, "status": "already_running", "subscription_id": subscription_id}
        subscription = database.get_subscription(subscription_id)
        if not subscription["enabled"]:
            raise ValueError("Subscription is paused")
        active_subscription_syncs.add(subscription_id)

        def worker(progress: Any) -> Any:
            try:
                options = ImportOptions(
                    **{
                        key: value
                        for key, value in dict(subscription.get("options") or {}).items()
                        if key in ImportOptions.__dataclass_fields__
                    }
                )
                result = importer.import_urls(
                    [str(subscription["url"])],
                    target_playlist_id=int(subscription["target_playlist_id"]),
                    options=options,
                    progress=progress,
                )
                database.update_subscription(subscription_id, last_sync_at=dt.datetime.now(dt.timezone.utc).isoformat())
                return result
            except Exception as exc:
                database.update_subscription(
                    subscription_id,
                    last_sync_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                    last_error=str(exc),
                )
                raise
            finally:
                active_subscription_syncs.discard(subscription_id)

        persisted_options = {
            **dict(subscription.get("options") or {}),
            "target_playlist_id": int(subscription["target_playlist_id"]),
            "subscription_id": int(subscription_id),
        }
        return start_import_job(
            label="subscription_sync", source_urls=[str(subscription["url"])],
            options=persisted_options, worker=worker, job_type="subscription_sync"
        )

    async def subscription_loop() -> None:
        while True:
            try:
                raw_schedule = next((row["value"] for row in database._rows("SELECT value FROM settings WHERE key='update_schedule'")), "manual")
                if raw_schedule in {"", "manual", "off", "disabled", None}:
                    await asyncio.sleep(60)
                    continue
                if raw_schedule == "daily":
                    configured_interval = 24 * 60 * 60
                elif raw_schedule == "3h":
                    configured_interval = 3 * 60 * 60
                else:
                    try: configured_interval = max(3 * 60 * 60, int(float(raw_schedule) * 3600))
                    except (TypeError, ValueError): configured_interval = subscription_interval_seconds
                now = dt.datetime.now(dt.timezone.utc)
                for subscription in database.list_subscriptions():
                    if not subscription["enabled"] or subscription["id"] in active_subscription_syncs:
                        continue
                    last_sync = subscription.get("last_sync_at")
                    if last_sync:
                        try:
                            elapsed = (now - dt.datetime.fromisoformat(last_sync)).total_seconds()
                        except ValueError:
                            elapsed = configured_interval
                        if elapsed < configured_interval:
                            continue
                    try:
                        start_subscription_sync(int(subscription["id"]))
                    except Exception:
                        # A failed scheduled start is kept visible through the
                        # subscription state on the next explicit request.
                        continue
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(60)

    @app.post("/api/subscriptions")
    async def create_subscription(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            url = " ".join(str(payload.get("url") or "").split()).strip()
            adapter = adapter_for_url(url)
            if adapter is None:
                raise ValueError("Subscription URL must be a YouTube or Bilibili channel, playlist, series, or video URL")
            target_playlist_id = int(payload["target_playlist_id"])
            options = import_options(payload)
            subscription = database.create_subscription(
                name=str(payload.get("name") or "").strip() or url,
                url=url,
                provider=adapter.name,
                target_playlist_id=target_playlist_id,
                options=options.source_options(),
            )
            job = start_subscription_sync(int(subscription["id"]))
            await emit_state("subscription_created")
            return {"subscription": subscription, "job": job}
        except (KeyError, TypeError, ValueError) as exc:
            raise _error(exc) from exc

    @app.get("/api/subscriptions")
    async def list_subscriptions() -> list[dict[str, Any]]:
        return database.list_subscriptions()

    @app.post("/api/subscriptions/{subscription_id}/sync")
    async def sync_subscription(subscription_id: int) -> dict[str, Any]:
        try:
            return start_subscription_sync(subscription_id)
        except ValueError as exc:
            raise _error(exc) from exc

    @app.post("/api/subscriptions/{subscription_id}")
    async def update_subscription(subscription_id: int, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return database.update_subscription(
                subscription_id,
                enabled=bool(payload["enabled"]) if "enabled" in payload else None,
            )
        except ValueError as exc:
            raise _error(exc) from exc

    @app.delete("/api/subscriptions/{subscription_id}")
    async def delete_subscription(subscription_id: int) -> dict[str, bool]:
        try:
            database.delete_subscription(subscription_id)
            await emit_state("subscription_deleted")
            return {"deleted": True}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/imports")
    async def start_import(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        urls = payload.get("urls")
        if not isinstance(urls, list):
            raise HTTPException(status_code=400, detail="'urls' must be a list")
        try:
            target_playlist_id = int(payload["target_playlist_id"])
            database.get_playlist(target_playlist_id)
            options = import_options(payload)
        except (KeyError, TypeError, ValueError) as exc:
            raise _error(exc) from exc
        return start_import_job(
            label="import",
            source_urls=[str(url) for url in urls],
            options={**options.source_options(), "target_playlist_id": target_playlist_id},
            worker=lambda progress: importer.import_urls(
                urls, target_playlist_id=target_playlist_id, options=options, progress=progress
            ),
        )

    @app.post("/api/imports/preview")
    async def preview_import(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        raw_urls = payload.get("urls") if isinstance(payload.get("urls"), list) else []
        cleaned = list(dict.fromkeys(" ".join(str(url).split()).strip() for url in raw_urls if str(url).strip()))
        preview = []
        for url in cleaned:
            adapter = adapter_for_url(url)
            preview.append({
                "url": url,
                "supported": bool(adapter),
                "provider": adapter.name if adapter else None,
                "remote_id": adapter.canonical_id_from_url(url) if adapter else None,
                "existing_source": any(source.get("url") == url for source in database.list_sources()),
            })
        return {"items": preview, "valid": sum(1 for item in preview if item["supported"]), "invalid": sum(1 for item in preview if not item["supported"])}

    @app.get("/api/imports/{job_id}")
    async def import_status(job_id: str) -> dict[str, Any]:
        try:
            return database.get_job(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/jobs")
    async def list_jobs(status_filter: str | None = None, limit: int = Query(default=100, ge=1, le=1000)) -> list[dict[str, Any]]:
        return database.list_jobs(limit=limit, status=status_filter)

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str) -> dict[str, Any]:
        try:
            return database.get_job(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/jobs/{job_id}/pause")
    async def pause_job(job_id: str) -> dict[str, Any]:
        try:
            control = job_cancel.get(job_id)
            if control:
                job_requested_status[job_id] = "paused"
                control.set()
            result = database.update_job(job_id, status="paused")
            await hub.broadcast_ui({"type": "job_paused", "job_id": job_id})
            return result
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/jobs/{job_id}/resume")
    async def resume_job(job_id: str) -> dict[str, Any]:
        try:
            job = database.get_job(job_id)
            if job["status"] not in {"paused", "failed", "waiting_retry"}:
                return job
            return resume_persisted_job(job_id)
        except (ValueError, TypeError) as exc:
            raise _error(exc) from exc

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str) -> dict[str, Any]:
        try:
            control = job_cancel.get(job_id)
            if control:
                job_requested_status[job_id] = "cancelled"
                control.set()
            result = database.update_job(job_id, status="cancelled", finished_at=dt.datetime.now(dt.timezone.utc).isoformat())
            await hub.broadcast_ui({"type": "job_cancelled", "job_id": job_id})
            return result
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/jobs/{job_id}/retry")
    async def retry_job(job_id: str) -> dict[str, Any]:
        job = database.get_job(job_id)
        database.update_job(job_id, retry_count=int(job.get("retry_count") or 0) + 1)
        result = await resume_job(job_id)
        await hub.broadcast_ui({"type": "job_retrying", "job_id": job_id})
        return result

    @app.delete("/api/jobs/{job_id}")
    async def delete_job(job_id: str) -> dict[str, bool]:
        try:
            database.delete_job(job_id); return {"deleted": True}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/tracks/{track_id}/metadata")
    async def get_track_metadata(track_id: int) -> dict[str, Any]:
        try:
            return database.get_track_metadata(track_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.patch("/api/tracks/{track_id}/metadata")
    async def patch_track_metadata(track_id: int, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            values = payload.get("values") if isinstance(payload.get("values"), dict) else payload
            return database.update_track_metadata(track_id, values, activate_override=bool(payload.get("override", True)))
        except ValueError as exc:
            raise _error(exc) from exc

    @app.post("/api/tracks/bulk-edit/preview")
    async def bulk_preview(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return database.bulk_metadata_preview(
                payload.get("track_ids") or [],
                payload.get("values") or {},
                operations=payload.get("operations") or [],
            )
        except (TypeError, ValueError) as exc:
            raise _error(exc) from exc

    @app.post("/api/tracks/bulk-edit")
    async def bulk_edit(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            result = database.bulk_update_metadata(
                payload.get("track_ids") or [],
                payload.get("values") or {},
                operations=payload.get("operations") or [],
            )
            await schedule_export(); await emit_state("bulk_metadata_changed"); return result
        except (TypeError, ValueError) as exc:
            raise _error(exc) from exc

    @app.post("/api/metadata/undo")
    async def undo_metadata(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return {"undone": database.undo_metadata(str(payload.get("operation_id") or ""))}

    @app.post("/api/metadata/accept-suggestions")
    async def accept_metadata_suggestions(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        ids = [int(v) for v in (payload.get("track_ids") or [])]
        fields = payload.get("fields")
        accepted = 0
        for track_id in ids:
            meta = database.get_track_metadata(track_id)
            values = {field: value for field, value in meta.get("effective", {}).items() if (not fields or field in fields) and not meta.get("overrides", {}).get(field)}
            if values:
                database.update_track_metadata(track_id, values, source="accepted_suggestion", activate_override=True)
                accepted += len(values)
        await schedule_export()
        return {"accepted": accepted, "tracks": len(ids)}

    @app.post("/api/relationships/suggest")
    async def suggest_relationships(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        suggestions = await asyncio.to_thread(
            database.suggest_track_relations,
            limit=int(payload.get("limit", 500)),
            min_confidence=float(payload.get("min_confidence", 0.65)),
        )
        created = await asyncio.to_thread(database.save_relation_suggestions, suggestions)
        # Keep suggestions in the review inbox without auto-merging tracks.
        for suggestion in suggestions:
            try:
                relation = next((item for item in database.list_relations(limit=2000)
                                 if int(item["from_track_id"]) == int(suggestion["from_track_id"])
                                 and int(item["to_track_id"]) == int(suggestion["to_track_id"])
                                 and item["relation_type"] == suggestion["relation_type"]), None)
                if relation:
                    database.create_review_item(kind="track_relation", relation_id=int(relation["id"]), payload=suggestion)
            except Exception:
                continue
        return {"created": created, "suggestions": suggestions}

    @app.post("/api/relationships/scan")
    async def start_relationship_scan(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        try:
            limit = max(1, min(int(payload.get("limit", 500)), 2000))
            minimum = max(0.0, min(float(payload.get("min_confidence", 0.65)), 1.0))
            return start_relation_scan_job(limit=limit, min_confidence=minimum)
        except (TypeError, ValueError) as exc:
            raise _error(exc) from exc

    @app.get("/api/tracks/{track_id}/relations")
    async def track_relations(
        track_id: int,
        depth: int = Query(default=2, ge=1, le=5),
        min_confidence: float = Query(default=0.65, ge=0, le=1),
        include_proposed: bool = False,
        relation_types: str = "",
    ) -> dict[str, Any]:
        try:
            types = [value.strip() for value in relation_types.split(",") if value.strip()]
            return database.relation_neighborhood(
                track_id,
                max_depth=depth,
                min_confidence=min_confidence,
                include_proposed=include_proposed,
                relation_types=types,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/tracks/{track_id}/relation-playlist")
    async def create_relation_playlist(track_id: int, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        try:
            track = database.get_track(track_id)
            depth = max(1, min(int(payload.get("max_depth", 2)), 5))
            minimum = max(0.0, min(float(payload.get("min_confidence", 0.65)), 1.0))
            relation = {
                "root_track_id": track_id,
                "max_depth": depth,
                "min_confidence": minimum,
                "include_proposed": bool(payload.get("include_proposed", False)),
                "relation_types": [str(value) for value in (payload.get("relation_types") or []) if str(value)],
            }
            name = str(payload.get("name") or f"Relations · {track['title']}").strip()
            playlist = database.create_playlist(name, kind="smart", query={"relation": relation})
            await emit_state("relation_playlist_created")
            return playlist
        except (TypeError, ValueError) as exc:
            raise _error(exc) from exc

    @app.get("/api/tracks/{track_id}/fingerprints")
    async def list_fingerprints(track_id: int) -> list[dict[str, Any]]:
        try:
            return database.list_track_fingerprints(track_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/tracks/{track_id}/fingerprints")
    async def add_fingerprint(track_id: int, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Accept user-supplied data; this endpoint never fetches provider media."""
        try:
            return database.save_track_fingerprint(
                track_id,
                algorithm=str(payload.get("algorithm") or "chromaprint"),
                fingerprint=str(payload.get("fingerprint") or ""),
                duration=float(payload["duration"]) if payload.get("duration") is not None else None,
                source="user_supplied",
            )
        except (TypeError, ValueError) as exc:
            raise _error(exc) from exc

    @app.post("/api/tracks/{track_id}/fingerprints/from-local-file")
    async def fingerprint_local_file(track_id: int, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Fingerprint only the explicit local path in this request."""
        try:
            raw_path = str(payload.get("path") or "").strip()
            if not raw_path:
                raise ValueError("Choose an explicit local audio file")
            return await asyncio.to_thread(
                database.fingerprint_local_file,
                track_id,
                Path(raw_path),
                fpcalc_path=str(payload["fpcalc_path"]) if payload.get("fpcalc_path") else None,
            )
        except (TypeError, ValueError) as exc:
            raise _error(exc) from exc

    @app.get("/api/relationships")
    async def list_relationships(status_filter: str | None = None, limit: int = Query(default=500, ge=1, le=2000)) -> list[dict[str, Any]]:
        return database.list_relations(status=status_filter, limit=limit)

    @app.post("/api/relationships/{relation_id}/review")
    async def review_relationship(relation_id: int, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return database.review_relation(relation_id, status=str(payload.get("status") or "accepted"))
        except ValueError as exc:
            raise _error(exc) from exc

    @app.get("/api/review")
    async def review_inbox(status_filter: str = "open", limit: int = Query(default=500, ge=1, le=2000)) -> list[dict[str, Any]]:
        return database.list_review_items(status=status_filter, limit=limit)

    @app.post("/api/review/{item_id}/resolve")
    async def resolve_review(item_id: int, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        try:
            return database.resolve_review_item(item_id, status=str(payload.get("status") or "resolved"))
        except ValueError as exc:
            raise _error(exc) from exc

    @app.post("/api/library/update")
    async def update_library(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        started: list[dict[str, Any]] = []
        for source in database.list_sources():
            try:
                started.append(start_import_job(label="library_update", source_urls=[str(source["url"])], options=source.get("options") or {},
                    worker=lambda progress, sid=int(source["id"]): importer.refresh_source(sid, progress=progress), job_type="library_update"))
            except Exception:
                continue
        return {"jobs": started}

    @app.get("/api/discover/sources")
    async def discover_sources() -> list[dict[str, Any]]:
        return database.list_sources() + database.list_subscriptions()

    @app.post("/api/discover")
    async def discover(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        sources = [str(v).strip() for v in (payload.get("sources") or []) if str(v).strip()]
        if not sources:
            sources = list(dict.fromkeys(
                [str(row["url"]) for row in database.list_sources()]
                + [str(row["url"]) for row in database.list_subscriptions() if row.get("enabled")]
            ))
        if not sources:
            raise _error(ValueError("Add or import at least one channel or playlist source before discovering."))
        size = max(1, min(int(payload.get("page_size", 100)), 500))
        session = database.create_discover_session(name=str(payload.get("name") or f"Discover · {dt.datetime.now().date()}"), sources=sources, page_size=size)
        options = import_options(payload)
        candidates = await asyncio.to_thread(
            importer.discover_candidates, sources, options=options, page_size=size,
        )
        return database.update_discover_session(session["id"], candidates=candidates, seen=[])

    @app.get("/api/discover/{session_id}")
    async def get_discover(session_id: str) -> dict[str, Any]:
        try: return database.get_discover_session(session_id)
        except ValueError as exc: raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/discover/{session_id}/refresh")
    async def refresh_discover(session_id: str, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        try:
            session = database.get_discover_session(session_id)
            options = import_options(payload)
            candidates = await asyncio.to_thread(
                importer.discover_candidates, session["sources"], options=options,
                page_size=int(payload.get("page_size") or session["page_size"]),
                seen=session.get("seen") or [],
            )
            seen = list(dict.fromkeys(
                [f"{item.get('provider')}:{item.get('remote_id')}" for item in (session.get("candidates") or [])]
                + [str(value) for value in (session.get("seen") or [])]
            ))
            return database.update_discover_session(session_id, candidates=candidates, seen=seen)
        except ValueError as exc: raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/discover/{session_id}/save")
    async def save_discover(session_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        session = database.get_discover_session(session_id)
        playlist_id = int(payload["playlist_id"])
        chosen = {str(x) for x in (payload.get("remote_ids") or [])}
        added = 0
        for candidate in session["candidates"]:
            if chosen and str(candidate.get("remote_id")) not in chosen: continue
            adapter = adapter_for_name(str(candidate.get("provider")))
            if not adapter or not candidate.get("remote_id"): continue
            track = database.upsert_track(provider=adapter.name, remote_id=str(candidate["remote_id"]), url=str(candidate.get("url") or ""), title=str(candidate.get("title") or candidate["remote_id"]), uploader=candidate.get("uploader"), creator=candidate.get("uploader"))
            _, was_added = database.add_track_to_playlist(playlist_id, int(track["id"]))
            added += int(was_added)
        await schedule_export(); await emit_state("discover_saved")
        return {"added": added, "playlist_id": playlist_id}

    @app.delete("/api/discover/{session_id}")
    async def delete_discover(session_id: str) -> dict[str, bool]:
        database.delete_discover_session(session_id); return {"deleted": True}

    @app.get("/api/backups")
    async def list_backups() -> list[dict[str, Any]]: return database.list_backups()

    @app.post("/api/backups")
    async def create_backup(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        return await asyncio.to_thread(database.create_backup, kind=str(payload.get("kind") or "manual"))

    @app.post("/api/backups/prune")
    async def prune_backups(payload: dict[str, Any] = Body(default={})) -> dict[str, int]:
        settings = await get_settings()
        def retention(name: str, default: int) -> int:
            return int(payload.get(name, settings.get(f"backup_retention_{name}", default)))
        return {
            "removed": await asyncio.to_thread(
                database.prune_backups,
                keep=int(payload["keep"]) if payload.get("keep") is not None else None,
                daily=retention("daily", 7),
                weekly=retention("weekly", 8),
                monthly=retention("monthly", 12),
                long_term=retention("long_term", 2),
            )
        }

    @app.post("/api/backups/{backup_id}/validate")
    async def validate_backup(backup_id: str) -> dict[str, Any]: return database.validate_backup(backup_id)

    @app.post("/api/backups/{backup_id}/restore")
    async def restore_backup(backup_id: str, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        if payload.get("dry_run"): return database.validate_backup(backup_id)
        target_dir = Path(str(payload["target_dir"])) if payload.get("target_dir") else None
        return await asyncio.to_thread(database.restore_backup, backup_id, target_dir=target_dir)

    @app.post("/api/backups/rollback-last-restore")
    async def rollback_last_restore() -> dict[str, Any]:
        return await asyncio.to_thread(database.rollback_restore)

    @app.delete("/api/backups/{backup_id}")
    async def delete_backup(backup_id: str) -> dict[str, bool]:
        backup = database.get_backup(backup_id); Path(backup["path"]).unlink(missing_ok=True)
        with database.transaction() as conn: conn.execute("DELETE FROM backups WHERE id = ?", (backup_id,))
        return {"deleted": True}

    @app.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        return {row["key"]: row["value"] for row in database._rows("SELECT key, value FROM settings ORDER BY key")}

    @app.patch("/api/settings")
    async def patch_settings(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        with database.transaction() as conn:
            for key, value in payload.items(): conn.execute("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(key), json.dumps(value) if not isinstance(value, str) else value))
        return await get_settings()

    @app.post("/api/settings/rotate-pairing-token")
    async def rotate_pairing_token() -> dict[str, str]:
        token = database.rotate_extension_token()
        await emit_state("pairing_token_rotated")
        return {"pairing_token": token}

    @app.get("/api/maintenance")
    async def maintenance_report() -> dict[str, Any]:
        tracks = database.list_tracks(limit=2000, include_hidden=True)
        return {
            "missing_uploader": [t["id"] for t in tracks if not (t.get("uploader") or t.get("creator"))],
            "missing_artist": [t["id"] for t in tracks if not t.get("artist")],
            "invalid_url": [t["id"] for t in tracks if not str(t.get("url") or "").startswith(("http://", "https://"))],
            "possible_duplicates": database.possible_duplicates(limit=500),
            "integrity": database._conn.execute("PRAGMA integrity_check").fetchone()[0],
        }

    @app.post("/api/maintenance/rebuild-entities")
    async def rebuild_entities() -> dict[str, int]:
        return await asyncio.to_thread(database.rebuild_music_entities)

    @app.get("/api/artists")
    async def list_artists(limit: int = Query(default=500, ge=1, le=2000)) -> list[dict[str, Any]]:
        return database._rows("SELECT * FROM artists ORDER BY name COLLATE NOCASE LIMIT ?", (limit,))

    @app.get("/api/releases")
    async def list_releases(limit: int = Query(default=500, ge=1, le=2000)) -> list[dict[str, Any]]:
        return database._rows("SELECT * FROM releases ORDER BY title COLLATE NOCASE LIMIT ?", (limit,))

    @app.get("/api/searches")
    async def list_searches() -> list[dict[str, Any]]:
        return database.list_searches()

    @app.post("/api/searches")
    async def save_search(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return database.save_search(str(payload.get("name") or ""), payload.get("query") if isinstance(payload.get("query"), dict) else {})
        except ValueError as exc:
            raise _error(exc) from exc

    @app.delete("/api/searches/{search_id}")
    async def delete_search(search_id: int) -> dict[str, bool]:
        try:
            database.delete_search(search_id); return {"deleted": True}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/sources")
    async def list_sources() -> dict[str, Any]:
        return {"sources": database.list_sources(), "runs": database.list_source_runs()}

    @app.post("/api/sources/{source_id}/refresh")
    async def refresh_source(source_id: int) -> dict[str, Any]:
        try:
            database.get_source(source_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        source = database.get_source(source_id)
        return start_import_job(
            label="source_refresh",
            source_urls=[str(source["url"])],
            options={
                **(source.get("options") or {}),
                "target_playlist_id": int(source["target_playlist_id"]),
                "source_id": int(source_id),
            },
            worker=lambda progress: importer.refresh_source(source_id, progress=progress),
            job_type="source_refresh",
        )

    @app.post("/api/tracks/{track_id}/rating")
    async def rate_track(track_id: int, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            value = payload.get("value")
            track = database.rate_track(track_id, None if value in (None, "") else float(value))
            await schedule_export()
            await emit_state("rating_changed")
            return track
        except (KeyError, TypeError, ValueError) as exc:
            raise _error(exc) from exc

    @app.post("/api/tracks/{track_id}/hidden")
    async def hide_track(track_id: int, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            database.get_track(track_id)
            database.hide_track(track_id, hidden=bool(payload.get("hidden", True)))
            await asyncio.to_thread(database.export_library)
            await emit_state("track_hidden")
            return database.get_track(track_id)
        except ValueError as exc:
            raise _error(exc) from exc

    @app.post("/api/tracks/{track_id}/open")
    async def open_track(track_id: int) -> dict[str, Any]:
        try:
            track = database.get_track(track_id)
            opened = await asyncio.to_thread(webbrowser.open, str(track["url"]))
            return {"opened": bool(opened), "url": track["url"]}
        except ValueError as exc:
            raise _error(exc) from exc

    @app.post("/api/memberships/{membership_id}/remove")
    async def remove_membership(membership_id: int) -> dict[str, bool]:
        database.remove_membership(membership_id)
        await asyncio.to_thread(database.export_library)
        await emit_state("membership_removed")
        return {"removed": True}

    @app.post("/api/memberships/{membership_id}/restore")
    async def restore_membership(membership_id: int) -> dict[str, bool]:
        try:
            database.restore_membership(membership_id)
            await asyncio.to_thread(database.export_library)
            await emit_state("membership_restored")
            return {"restored": True}
        except ValueError as exc:
            raise _error(exc) from exc

    @app.get("/api/trash")
    async def trash() -> dict[str, list[dict[str, Any]]]:
        return database.list_trash()

    @app.get("/api/possible-duplicates")
    async def possible_duplicates(limit: int = Query(default=50, ge=1, le=500)) -> list[dict[str, Any]]:
        return database.possible_duplicates(limit=limit)

    @app.get("/api/pools")
    async def list_pools() -> list[dict[str, Any]]:
        return database.list_pools()

    @app.post("/api/pools")
    async def save_pool(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            selection = payload.get("selection")
            if not isinstance(selection, dict):
                raise ValueError("Pool selection must be an object")
            pool = database.save_pool(str(payload.get("name") or ""), selection)
            await emit_state("pool_saved")
            return pool
        except ValueError as exc:
            raise _error(exc) from exc

    @app.delete("/api/pools/{pool_id}")
    async def delete_pool(pool_id: int) -> dict[str, bool]:
        try:
            database.delete_pool(pool_id)
            await emit_state("pool_deleted")
            return {"deleted": True}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/merge")
    async def merge_playlists(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            playlist_ids = [int(value) for value in payload.get("playlist_ids", [])]
            merged = database.merge_playlists(playlist_ids, str(payload.get("name") or ""))
            if bool(payload.get("archive_sources", False)):
                for playlist_id in playlist_ids:
                    database.archive_playlist(playlist_id, archived=True)
            await asyncio.to_thread(database.export_library)
            await emit_state("playlists_merged")
            return merged
        except (TypeError, ValueError) as exc:
            raise _error(exc) from exc

    @app.post("/api/queue/build")
    async def build_queue(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            selection = payload.get("selection")
            if not isinstance(selection, dict):
                raise ValueError("Queue selection must be an object")
            mode = str(payload.get("mode") or "true")
            if mode not in {"true", "variety"}:
                raise ValueError("Shuffle mode must be 'true' or 'variety'")
            tracks = database.pool_tracks(selection)
            seed = int(payload["seed"]) if payload.get("seed") not in (None, "") else None
            if mode == "true":
                tracks = true_shuffle(tracks, seed=seed)
            else:
                tracks = variety_shuffle(
                    tracks,
                    cooldown=max(0, int(payload.get("cooldown", 4))),
                    seed=seed,
                )
            queue = database.set_queue(
                tracks,
                mode=mode,
                include_repeats=bool(selection.get("include_repeats", False)),
            )
            result = await command_current(open_fallback=True)
            return {"track_count": len(tracks), **result}
        except (TypeError, ValueError) as exc:
            raise _error(exc) from exc

    @app.get("/api/queue")
    async def queue_state(
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        return database.queue_state(preview_limit=limit, preview_offset=offset)

    @app.get("/api/history")
    async def playback_history(
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        return database.list_playback_history(limit=limit, offset=offset)

    @app.post("/api/queue/play")
    async def play_queue() -> dict[str, Any]:
        return await command_current(open_fallback=True)

    @app.post("/api/queue/pause")
    async def pause_queue() -> dict[str, Any]:
        queue = database.queue_state(preview_limit=50)
        sent = await hub.player_command("pause")
        return {**_queue_command_payload(queue), "extension_command_sent": sent}

    @app.post("/api/queue/stop")
    async def stop_queue() -> dict[str, Any]:
        queue = database.queue_state(preview_limit=50)
        sent = await hub.player_command("stop")
        return {**_queue_command_payload(queue), "extension_command_sent": sent}

    @app.post("/api/queue/clear")
    async def clear_queue() -> dict[str, Any]:
        queue = database.clear_queue()
        await emit_state("queue_cleared")
        return _queue_command_payload(queue)

    @app.post("/api/queue/reorder")
    async def reorder_queue(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        positions = payload.get("positions")
        if not isinstance(positions, list):
            raise HTTPException(status_code=400, detail="'positions' must be a list")
        queue = database.reorder_queue([int(value) for value in positions])
        await emit_state("queue_reordered")
        return _queue_command_payload(queue)

    @app.delete("/api/queue/{position}")
    async def remove_queue_item(position: int) -> dict[str, Any]:
        queue = database.remove_queue_item(position)
        await emit_state("queue_item_removed")
        return _queue_command_payload(queue)

    @app.post("/api/queue/save")
    async def save_queue(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        queue = database.queue_state(preview_limit=500)
        name = str(payload.get("name") or "").strip()
        if not name: raise HTTPException(status_code=400, detail="Queue mix name is required")
        pool = database.save_pool(name, {"track_ids": [item["id"] for item in queue["items"]], "include_repeats": queue["include_repeats"]})
        return pool

    @app.post("/api/queue/load")
    async def load_queue(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        pool_id = int(payload["pool_id"])
        pools = {int(pool["id"]): pool for pool in database.list_pools()}
        if pool_id not in pools: raise HTTPException(status_code=404, detail="Queue mix was not found")
        tracks = database.pool_tracks(pools[pool_id]["selection"])
        queue = database.set_queue(tracks, mode=str(payload.get("mode") or "true"), include_repeats=bool(pools[pool_id]["selection"].get("include_repeats")))
        await emit_state("queue_loaded")
        return _queue_command_payload(queue)

    async def move_queue(delta: int, reason: str) -> dict[str, Any]:
        before = database.queue_state(preview_limit=1)
        if not before["total"]:
            raise HTTPException(status_code=400, detail="The queue is empty")
        current_position = int(before["current_position"])
        target = current_position + delta
        if target < 0 or target >= int(before["total"]):
            await hub.broadcast_ui({"type": "queue_finished" if delta > 0 else "queue_at_start"})
            return {"changed": False, **_queue_command_payload(before)}
        queue = database.move_queue(delta, reason=reason)
        current = queue["current"]
        assert current is not None
        sent = await hub.player_command("navigate", **_player_command_data(current))
        await emit_state("queue_moved")
        return {"changed": True, **_queue_command_payload(queue), "extension_command_sent": sent}

    @app.post("/api/queue/next")
    async def next_queue() -> dict[str, Any]:
        return await move_queue(1, "manual_next")

    @app.post("/api/queue/previous")
    async def previous_queue() -> dict[str, Any]:
        return await move_queue(-1, "manual_previous")

    @app.post("/api/export")
    async def export_library() -> dict[str, str]:
        exports = await asyncio.to_thread(database.export_library)
        await hub.broadcast_ui({"type": "exports_updated", "exports": exports})
        return exports

    @app.websocket("/ws/ui")
    async def ui_websocket(websocket: WebSocket) -> None:
        await hub.connect_ui(websocket)
        try:
            await websocket.send_json(
                {
                    "type": "connected",
                    "stats": database.stats(),
                    "queue": database.queue_state(preview_limit=50),
                    "extension_sessions": database.extension_sessions(),
                }
            )
            async for _ in websocket.iter_json():
                # The dashboard is server-authoritative; requests go through
                # HTTP while this channel only keeps state live.
                pass
        finally:
            hub.disconnect_ui(websocket)

    @app.websocket("/ws/extension")
    async def extension_websocket(websocket: WebSocket, token: str) -> None:
        expected = database.extension_token()
        if not secrets.compare_digest(token, expected):
            raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid pairing token")
        profile = "unknown"
        try:
            await websocket.accept()
            first = await websocket.receive_json()
            if not isinstance(first, dict) or first.get("type") != "hello":
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                return
            profile = str(first.get("profile") or "default")[:120]
            await hub.connect_extension(profile, websocket)
            tab_id = first.get("tab_id")
            database.update_extension_session(
                profile=profile,
                tab_id=int(tab_id) if tab_id is not None else None,
                status="connected",
                url=str(first.get("url") or "") or None,
            )
            await emit_state("extension_connected")
            queue = database.queue_state(preview_limit=50)
            # Reconnecting the extension must not create a player tab. It may
            # resume navigation only when that browser still has its designated
            # tab, reported explicitly by the extension handshake.
            if queue["current"] is not None and bool(first.get("resume")) and tab_id is not None:
                await websocket.send_json(
                    {
                        "type": "command",
                        "command": "navigate",
                        **_player_command_data(queue["current"]),
                    }
                )
            async for message in websocket.iter_json():
                if not isinstance(message, dict):
                    continue
                event_type = str(message.get("type") or "")
                tab_id = message.get("tab_id")
                url = str(message.get("url") or "") or None
                status_text = str(message.get("status") or event_type or "connected")[:120]
                database.update_extension_session(
                    profile=profile,
                    tab_id=int(tab_id) if tab_id is not None else None,
                    status=status_text,
                    url=url,
                )
                if event_type == "ended":
                    queue = database.queue_state(preview_limit=1)
                    provider = str(message.get("provider") or "")
                    expected_duration = queue["current"].get("duration") if queue["current"] else None
                    observed_duration = message.get("duration")
                    duration_matches = True
                    if expected_duration is not None:
                        try:
                            observed = float(observed_duration)
                            expected = float(expected_duration)
                            duration_matches = abs(observed - expected) <= max(10.0, expected * 0.15)
                        except (TypeError, ValueError):
                            duration_matches = False
                    if (
                        bool(message.get("is_ad"))
                        or str(message.get("ad_state") or "").lower() not in {"", "none", "false"}
                        or not _same_track(queue["current"], provider=provider, url=url)
                        or not duration_matches
                    ):
                        await hub.broadcast_ui(
                            {
                                "type": "completion_ignored",
                                "reason": "Completion did not safely match the queued track",
                            }
                        )
                    else:
                        await move_queue(1, "verified_end")
                elif event_type in {"blocked", "unavailable", "live", "unknown_duration", "error"}:
                    await hub.broadcast_ui(
                        {
                            "type": "playback_status",
                            "status": event_type,
                            "detail": str(message.get("detail") or ""),
                            "url": url,
                        }
                    )
                else:
                    await hub.broadcast_ui(
                        {
                            "type": "player_state",
                            "status": status_text,
                            "url": url,
                            "provider": message.get("provider"),
                        }
                    )
        except WebSocketDisconnect:
            pass
        finally:
            connected = hub.extension_connections.get(profile)
            hub.disconnect_extension(profile, websocket)
            if connected is websocket:
                database.update_extension_session(
                    profile=profile,
                    tab_id=None,
                    status="disconnected",
                    url=None,
                )
                await emit_state("extension_disconnected")

    @app.get("/library.md")
    async def markdown_export() -> FileResponse:
        path = Path(database.export_library()["markdown"])
        return FileResponse(path, media_type="text/markdown", filename="library.md")

    @app.get("/library.txt")
    async def text_export() -> FileResponse:
        path = Path(database.export_library()["text"])
        return FileResponse(path, media_type="text/plain", filename="library.txt")

    @app.get("/library.json")
    async def json_export() -> FileResponse:
        path = Path(database.export_library()["json"])
        return FileResponse(path, media_type="application/json", filename="library.json")

    @app.get("/library.csv")
    async def csv_export() -> FileResponse:
        path = Path(database.export_library()["csv"])
        return FileResponse(path, media_type="text/csv", filename="library.csv")

    @app.get("/library.m3u")
    async def m3u_export() -> FileResponse:
        path = Path(database.export_library()["m3u"])
        return FileResponse(path, media_type="audio/x-mpegurl", filename="library.m3u")

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
