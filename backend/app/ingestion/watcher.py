"""Real-time watching of mapped local folders (watchdog) + a periodic safety rescan.

Each enabled WatchedFolder gets a watchdog observer. Filesystem events are
debounced (bursts of writes during a file copy collapse into one sync) and then
drive ``sync_folder`` on a fresh DB session. A periodic full rescan (scheduled
from the APScheduler tick wiring) backstops any events the OS dropped and covers
network filesystems where inotify is unreliable.
"""
from __future__ import annotations

import logging
import threading

from sqlalchemy import select

from ..db import SessionLocal
from ..models import WatchedFolder
from .local_folder import sync_folder
from .media import is_supported

log = logging.getLogger("shelf.watcher")

_DEBOUNCE_S = 2.0


class _Manager:
    def __init__(self) -> None:
        self._observer = None
        self._watches: dict[int, object] = {}  # folder_id -> ObservedWatch
        self._timers: dict[int, threading.Timer] = {}
        self._lock = threading.Lock()

    # -- lifecycle -------------------------------------------------------------
    def start(self) -> None:
        try:
            from watchdog.observers import Observer
        except Exception as exc:  # pragma: no cover - optional dep
            log.warning("watchdog unavailable (%s); folders sync on the periodic rescan only", exc)
            return
        if self._observer is not None:
            return
        self._observer = Observer()
        self._observer.start()
        db = SessionLocal()
        try:
            for folder in db.scalars(
                select(WatchedFolder).where(WatchedFolder.enabled.is_(True))
            ).all():
                self._schedule(folder.id, folder.path, folder.recursive)
                self._debounced(folder.id)  # initial reconcile
        finally:
            db.close()

    def stop(self) -> None:
        with self._lock:
            for t in self._timers.values():
                t.cancel()
            self._timers.clear()
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=5)
            except Exception:
                pass
            self._observer = None
            self._watches.clear()

    # -- per-folder ------------------------------------------------------------
    def add(self, folder_id: int, path: str, recursive: bool) -> None:
        if self._observer is None:
            self.start()
        self._schedule(folder_id, path, recursive)
        self._debounced(folder_id)

    def remove(self, folder_id: int) -> None:
        with self._lock:
            t = self._timers.pop(folder_id, None)
            if t:
                t.cancel()
        watch = self._watches.pop(folder_id, None)
        if watch is not None and self._observer is not None:
            try:
                self._observer.unschedule(watch)
            except Exception:
                pass

    def _schedule(self, folder_id: int, path: str, recursive: bool) -> None:
        if self._observer is None:
            return
        import os

        if not os.path.isdir(path):
            return
        old = self._watches.pop(folder_id, None)
        if old is not None:
            try:
                self._observer.unschedule(old)
            except Exception:
                pass
        handler = _Handler(self, folder_id)
        self._watches[folder_id] = self._observer.schedule(handler, path, recursive=recursive)

    # -- debounced sync --------------------------------------------------------
    def _debounced(self, folder_id: int) -> None:
        with self._lock:
            t = self._timers.pop(folder_id, None)
            if t:
                t.cancel()
            timer = threading.Timer(_DEBOUNCE_S, self._run_sync, args=(folder_id,))
            timer.daemon = True
            self._timers[folder_id] = timer
            timer.start()

    def _run_sync(self, folder_id: int) -> None:
        with self._lock:
            self._timers.pop(folder_id, None)
        db = SessionLocal()
        try:
            folder = db.get(WatchedFolder, folder_id)
            if folder is None or not folder.enabled:
                return
            summary = sync_folder(db, folder)
            if any(summary.get(k) for k in ("added", "updated", "removed")):
                log.info("folder %s synced: %s", folder.path, summary)
        except Exception as exc:  # noqa: BLE001
            log.warning("folder sync failed (%s): %s", folder_id, exc)
        finally:
            db.close()


class _Handler:
    """watchdog FileSystemEventHandler (duck-typed to avoid an import at module load)."""

    def __init__(self, manager: _Manager, folder_id: int) -> None:
        self.manager = manager
        self.folder_id = folder_id

    def dispatch(self, event) -> None:
        if getattr(event, "is_directory", False):
            self.manager._debounced(self.folder_id)
            return
        paths = [getattr(event, "src_path", ""), getattr(event, "dest_path", "")]
        if any(p and is_supported(p) for p in paths):
            self.manager._debounced(self.folder_id)


manager = _Manager()


def rescan_all() -> None:
    """Full reconcile of every enabled folder (periodic safety net)."""
    db = SessionLocal()
    try:
        for folder in db.scalars(
            select(WatchedFolder).where(WatchedFolder.enabled.is_(True))
        ).all():
            try:
                sync_folder(db, folder)
            except Exception as exc:  # noqa: BLE001
                log.warning("rescan failed for %s: %s", folder.path, exc)
    finally:
        db.close()
