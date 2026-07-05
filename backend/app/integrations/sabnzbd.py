"""SABnzbd client — usenet download client (SABnzbd JSON API).

SABnzbd uses a single ``/api`` endpoint with a ``mode`` query param and ``apikey`` auth
(not a REST surface). On a bad key it answers HTTP 200 with ``{"status": false,
"error": "API Key Incorrect"}``, so we unwrap that into an IntegrationError.

Completed downloads land in the category's directory. When SABnzbd runs on another host
the path it reports (e.g. ``/media/NAS-Pool/...``) may differ from where Shelf reads it
(``/mnt/NAS-Pool/...``); the orchestrator applies a configurable path mapping at import.
"""
from __future__ import annotations

from dataclasses import dataclass

from .base import BaseClient, IntegrationError, RootFolder

API = "/api"


@dataclass
class QueueSlot:
    nzo_id: str
    filename: str
    status: str
    percentage: int
    category: str | None
    mb: float
    mb_left: float


@dataclass
class HistorySlot:
    nzo_id: str
    name: str
    status: str            # Completed | Failed | Extracting | ...
    category: str | None
    storage: str | None    # final path on the SABnzbd host
    fail_message: str | None
    bytes: int
    completed: int = 0     # unix epoch the download finished (0 if unknown)


class SABnzbdClient(BaseClient):
    provider = "sabnzbd"

    def _params(self, mode: str, **extra) -> dict:
        p = {"mode": mode, "output": "json", "apikey": self.api_key}
        for k, v in extra.items():
            if v is not None:
                p[k] = v
        return p

    async def _call(self, mode: str, **extra):
        data = await self._get(API, params=self._params(mode, **extra))
        if isinstance(data, dict):
            # SAB signals failures (incl. bad key) in-band with HTTP 200. Treat an explicit
            # status:false as failure; treat a top-level error only when status isn't an
            # explicit true (some modes carry a non-fatal error alongside status:true).
            if data.get("status") is False or (
                data.get("error") and data.get("status") is not True
            ):
                msg = data.get("error") or "request failed"
                raise IntegrationError(f"sabnzbd: {msg}")
        return data

    async def test_connection(self) -> dict:
        ver = await self._call("version")
        # `queue` requires a valid key, so this validates auth (version alone may not).
        await self._call("queue", limit="1")
        detail = None
        try:
            cats = await self.categories()
            names = [c.get("name") for c in cats if c.get("name") and c.get("name") != "*"]
            detail = "categories: " + (", ".join(names) if names else "(none)")
        except IntegrationError:
            pass
        return {
            "app": "SABnzbd",
            "version": (ver or {}).get("version"),
            "detail": detail,
        }

    async def get_config(self, section: str | None = None) -> dict:
        data = await self._call("get_config", section=section)
        return (data or {}).get("config", {}) if isinstance(data, dict) else {}

    async def categories(self) -> list[dict]:
        cfg = await self.get_config(section="categories")
        return cfg.get("categories", []) or []

    async def root_folders(self) -> list[RootFolder]:
        """Surface where downloads land: the global complete dir + each category dir."""
        out: list[RootFolder] = []
        try:
            cfg = await self.get_config()
        except IntegrationError:
            return out
        misc = cfg.get("misc", {}) or {}
        complete = misc.get("complete_dir")
        if complete:
            out.append(RootFolder(id=None, path=complete))
        for c in cfg.get("categories", []) or []:
            d = (c.get("dir") or "").strip()
            if d:
                out.append(RootFolder(id=None, path=d))
        return out

    async def add_url(
        self, url: str, *, category: str | None = None,
        nzbname: str | None = None, priority: int | None = None,
    ) -> dict:
        """Enqueue an NZB by URL. Returns {"status": true, "nzo_ids": [...]}."""
        data = await self._call(
            "addurl", name=url, cat=category, nzbname=nzbname,
            priority=(str(priority) if priority is not None else None),
        )
        ids = (data or {}).get("nzo_ids") or []
        if not ids:
            raise IntegrationError("sabnzbd: addurl returned no nzo_id (rejected?)")
        return {"nzo_ids": ids}

    async def queue(self, *, limit: int = 100, start: int = 0,
                    category: str | None = None) -> list[QueueSlot]:
        data = await self._call("queue", limit=str(limit), start=str(start), category=category)
        q = (data or {}).get("queue", {}) if isinstance(data, dict) else {}
        out: list[QueueSlot] = []
        for s in q.get("slots", []) or []:
            out.append(QueueSlot(
                nzo_id=s.get("nzo_id", ""),
                filename=s.get("filename", ""),
                status=s.get("status", ""),
                percentage=int(s.get("percentage") or 0),
                category=s.get("cat"),
                mb=float(s.get("mb") or 0),
                mb_left=float(s.get("mbleft") or 0),
            ))
        return out

    async def queue_all(self, *, page: int = 500, cap: int = 10000,
                        category: str | None = None) -> list[QueueSlot]:
        """Fetch the ENTIRE queue, paging through it. The poll/import path MUST see every queued
        nzo: a capped single fetch leaves slots beyond the cap invisible, and downloads still sitting
        in the queue would then be wrongly failed as 'SABnzbd no longer tracks this download'. ``cap``
        is a runaway backstop (a queue past it is already pathological). ``category`` scopes the page
        to OUR downloads on a shared SAB — without it we'd page through every other app's queue (e.g.
        Sonarr's hundreds of TV grabs) on every poll for no benefit."""
        out: list[QueueSlot] = []
        start = 0
        while start < cap:
            batch = await self.queue(limit=page, start=start, category=category)
            out.extend(batch)
            if len(batch) < page:
                break
            start += page
        return out

    async def is_paused(self) -> bool:
        """True when the WHOLE SABnzbd queue is paused (operator or scheduler pause). Important: during
        a global pause the individual slots still report status ``Queued`` (NOT ``paused``), so a caller
        that only inspects per-slot status would mistake a deliberate pause for hundreds of per-download
        stalls. Callers must consult this before acting on a stall."""
        data = await self._call("queue", limit="0")
        q = (data or {}).get("queue", {}) if isinstance(data, dict) else {}
        return bool(q.get("paused"))

    async def history(self, *, limit: int = 100, category: str | None = None) -> list[HistorySlot]:
        data = await self._call("history", limit=str(limit), category=category)
        h = (data or {}).get("history", {}) if isinstance(data, dict) else {}
        out: list[HistorySlot] = []
        for s in h.get("slots", []) or []:
            out.append(HistorySlot(
                nzo_id=s.get("nzo_id", ""),
                name=s.get("name", ""),
                status=s.get("status", ""),
                category=s.get("category"),
                storage=s.get("storage"),
                fail_message=s.get("fail_message") or None,
                bytes=int(s.get("bytes") or 0),
                completed=int(s.get("completed") or 0),
            ))
        return out

    async def delete_history(self, nzo_id: str, *, del_files: bool = False) -> dict:
        return await self._call(
            "history", name="delete", value=nzo_id,
            del_files=("1" if del_files else "0"),
        )

    async def queue_delete(self, nzo_id: str, *, del_files: bool = True) -> dict:
        """Remove an item from the ACTIVE queue (a still-downloading/queued nzo). Needed to cancel a
        candidate the cascade has advanced away from: delete_history only removes COMPLETED items, so a
        still-downloading abandoned candidate would otherwise keep going and land as an orphan."""
        return await self._call(
            "queue", name="delete", value=nzo_id,
            del_files=("1" if del_files else "0"),
        )
