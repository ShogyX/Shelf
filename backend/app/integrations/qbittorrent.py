"""qBittorrent client — torrent download client (Web API v2).

qBittorrent authenticates with a cookie: POST /api/v2/auth/login (form username+password) returns
``Ok.`` + a ``SID`` cookie that every later call must carry. The BaseClient opens a fresh HTTP client
per request, so we cache the SID on the instance and attach it ourselves (re-logging in once on a 403).

The username lives in ``config``; the password is stored in the Integration ``api_key`` column (the
existing never-returned secret slot). Completed torrents land under the save path / category dir; the
orchestrator applies the same path mapping + verify→import flow as the SABnzbd path.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .. import telemetry
from . import ratelimit
from .base import BaseClient, IntegrationError, RootFolder

API = "/api/v2"

# A magnet's btih is either a 40-char hex (v1) or a 32-char base32 infohash.
_MAGNET_BTIH = re.compile(r"xt=urn:btih:([0-9a-fA-F]{40}|[A-Za-z2-7]{32})")


def magnet_hash(magnet: str) -> str | None:
    """Extract the lowercase infohash from a magnet URI (None if it isn't a magnet / has no btih)."""
    m = _MAGNET_BTIH.search(magnet or "")
    return m.group(1).lower() if m else None


@dataclass
class TorrentInfo:
    hash: str
    name: str
    state: str             # downloading | stalledDL | uploading | pausedUP | checkingUP | error | ...
    progress: float        # 0.0–1.0
    category: str | None
    save_path: str | None
    content_path: str | None   # the torrent's root file/dir on the qBit host
    size: int


# qBittorrent "complete" states (download finished; may be seeding or stopped after completion).
# qBittorrent 5.0 renamed pausedUP→stoppedUP; include both so it works across versions.
_DONE_STATES = {"uploading", "stalledUP", "pausedUP", "stoppedUP",
                "queuedUP", "forcedUP", "checkingUP"}


def is_complete(state: str) -> bool:
    return state in _DONE_STATES


def _session_cookie(cookies) -> tuple[str, str | None]:
    """qBittorrent names its WebUI session cookie ``SID`` (classic) or ``QBT_SID_<port>`` (newer
    builds, multiple instances, or behind a proxy). Return (name, value) of whichever it set so we can
    echo it back under the right name. Falls back to the sole cookie when exactly one was set."""
    pairs = [(c.name, c.value) for c in getattr(cookies, "jar", [])]
    for name, value in pairs:
        if name == "SID" or name.startswith("QBT_SID"):
            return name, value
    if len(pairs) == 1:
        return pairs[0]
    return "SID", None


class QBittorrentClient(BaseClient):
    provider = "qbittorrent"

    def __init__(self, base_url: str, api_key: str, *,
                 kind: str | None = None, config: dict | None = None) -> None:
        super().__init__(base_url, api_key, kind=kind, config=config)
        self._sid: str | None = None
        self._sid_name = "SID"                 # actual cookie name learned at login (SID / QBT_SID_<port>)
        self._username = ((config or {}).get("username") or "").strip()
        self._password = api_key or ""        # stored in the api_key column (never returned)

    async def _login(self) -> None:
        await ratelimit.throttle(self._rate_key, self._rpm)
        url = f"{self.base_url}{API}/auth/login"
        try:
            async with telemetry.instrument("integration", timeout=self._timeout,
                                            follow_redirects=True) as client:
                resp = await client.post(
                    url, data={"username": self._username, "password": self._password},
                    headers={"Referer": self.base_url})
        except Exception as exc:  # noqa: BLE001 — surface a clean message
            raise IntegrationError(f"qbittorrent: cannot reach {self.base_url} ({exc})") from exc
        if resp.status_code == 403:
            raise IntegrationError("qbittorrent: login temporarily banned (too many failures)")
        # A wrong password is HTTP 200 + body "Fails." and NO cookie. Success is "Ok." on 200 (classic)
        # but an EMPTY body on 204 (newer builds). So the reliable cross-version success signal is the
        # session cookie's presence, not the status/body — and we must echo it back under its real name.
        if resp.status_code >= 400 or "Fails" in (resp.text or ""):
            raise IntegrationError("qbittorrent: login failed — check username/password")
        name, sid = _session_cookie(resp.cookies)
        if not sid:
            raise IntegrationError("qbittorrent: login returned no session cookie")
        self._sid, self._sid_name = sid, name

    async def _api(self, method: str, path: str, *, data: dict | None = None,
                   params: dict | None = None, want_json: bool = True, _retry: bool = True):
        if not self._sid:
            await self._login()
        await ratelimit.throttle(self._rate_key, self._rpm)
        url = f"{self.base_url}{API}{path}"
        headers = {"Cookie": f"{self._sid_name}={self._sid}", "Referer": self.base_url}
        # Strip None from BOTH data and params: qBittorrent treats an empty `category=` as "filter to
        # uncategorized", so a None category leaked into the query wrongly hides categorized torrents.
        clean = {k: v for k, v in (data or {}).items() if v is not None} or None
        clean_params = {k: v for k, v in (params or {}).items() if v is not None} or None
        try:
            async with telemetry.instrument("integration", timeout=self._timeout,
                                            follow_redirects=True) as client:
                resp = await client.request(method, url, data=clean, params=clean_params,
                                            headers=headers)
        except Exception as exc:  # noqa: BLE001
            raise IntegrationError(f"qbittorrent: cannot reach {self.base_url} ({exc})") from exc
        if resp.status_code == 403 and _retry:   # SID expired → re-login once and retry
            self._sid = None
            return await self._api(method, path, data=data, params=params,
                                   want_json=want_json, _retry=False)
        if resp.status_code >= 400:
            raise IntegrationError(
                f"qbittorrent: HTTP {resp.status_code} from {path}: {resp.text[:200]}")
        if not want_json:
            return resp.text
        if not resp.content:
            return {}
        try:
            return resp.json()
        except Exception:  # noqa: BLE001 — some endpoints return plain "Ok."
            return resp.text

    async def test_connection(self) -> dict:
        await self._login()                       # validates auth
        ver = await self._api("GET", "/app/version", want_json=False)
        return {"app": "qBittorrent", "version": (ver or "").strip() or None}

    async def root_folders(self) -> list[RootFolder]:
        """Surface qBittorrent's default save path so the operator can set a path mapping."""
        try:
            prefs = await self._api("GET", "/app/preferences")
        except IntegrationError:
            return []
        save = (prefs or {}).get("save_path") if isinstance(prefs, dict) else None
        return [RootFolder(id=None, path=save)] if save else []

    async def add_torrent(self, url: str, *, category: str | None = None,
                          savepath: str | None = None, paused: bool = True) -> None:
        """Add a magnet or .torrent URL. qBittorrent answers ``Ok.``/``Fails.`` and never returns the
        hash, so the caller reads it back from the magnet (``magnet_hash``) or from ``torrents_info``."""
        # qBittorrent 5.0 renamed the "paused" add param to "stopped" (and pause/resume → stop/start).
        # Send BOTH so it works on v4 and v5; qBittorrent ignores the unknown one.
        flag = "true" if paused else "false"
        res = await self._api("POST", "/torrents/add", want_json=False, data={
            "urls": url, "category": category, "savepath": savepath,
            "paused": flag, "stopped": flag,
        })
        if isinstance(res, str) and "Fails" in res:
            raise IntegrationError("qbittorrent: torrents/add rejected the URL (Fails.)")

    async def torrents_info(self, *, category: str | None = None,
                            hashes: str | None = None) -> list[TorrentInfo]:
        data = await self._api("GET", "/torrents/info",
                                params={"category": category, "hashes": hashes})
        out: list[TorrentInfo] = []
        for t in data or []:
            out.append(TorrentInfo(
                hash=(t.get("hash") or "").lower(), name=t.get("name", ""),
                state=t.get("state", ""), progress=float(t.get("progress") or 0.0),
                category=t.get("category"), save_path=t.get("save_path"),
                content_path=t.get("content_path"), size=int(t.get("size") or 0),
            ))
        return out

    async def torrent_files(self, torrent_hash: str) -> list[dict]:
        data = await self._api("GET", "/torrents/files", params={"hash": torrent_hash})
        return list(data or []) if isinstance(data, list) else []

    async def set_file_priority(self, torrent_hash: str, file_ids: list[int], priority: int) -> None:
        """Set download priority for the given file indices (0 = do-not-download)."""
        if not file_ids:
            return
        await self._api("POST", "/torrents/filePrio", want_json=False, data={
            "hash": torrent_hash, "id": "|".join(str(i) for i in file_ids),
            "priority": str(priority),
        })

    async def resume(self, torrent_hash: str) -> None:
        # qBittorrent 5.0 renamed resume→start; try the new endpoint first, fall back to the legacy one.
        try:
            await self._api("POST", "/torrents/start", want_json=False, data={"hashes": torrent_hash})
        except IntegrationError as exc:
            if "HTTP 404" not in str(exc):
                raise
            await self._api("POST", "/torrents/resume", want_json=False, data={"hashes": torrent_hash})

    async def pause(self, torrent_hash: str) -> None:
        # qBittorrent 5.0 renamed pause→stop; try the new endpoint first, fall back to the legacy one.
        try:
            await self._api("POST", "/torrents/stop", want_json=False, data={"hashes": torrent_hash})
        except IntegrationError as exc:
            if "HTTP 404" not in str(exc):
                raise
            await self._api("POST", "/torrents/pause", want_json=False, data={"hashes": torrent_hash})

    async def delete(self, torrent_hash: str, *, delete_files: bool = False) -> None:
        await self._api("POST", "/torrents/delete", want_json=False, data={
            "hashes": torrent_hash, "deleteFiles": "true" if delete_files else "false",
        })


def _demo() -> None:
    """Self-check: magnet-hash parsing + completion-state classification (no network)."""
    assert magnet_hash("magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=x") \
        == "0123456789abcdef0123456789abcdef01234567"
    assert magnet_hash("magnet:?xt=urn:btih:ABCDEFGHIJKLMNOPQRSTUVWXYZ234567&dn=y") \
        == "abcdefghijklmnopqrstuvwxyz234567"        # 32-char base32 infohash
    assert magnet_hash("https://example.org/file.torrent") is None
    assert magnet_hash("") is None
    assert is_complete("uploading") and is_complete("pausedUP")
    assert not is_complete("downloading") and not is_complete("stalledDL")
    # session-cookie parsing across qBittorrent versions (no network)
    import httpx
    c_new = httpx.Cookies(); c_new.set("QBT_SID_8090", "abc", domain="h")
    assert _session_cookie(c_new) == ("QBT_SID_8090", "abc")    # newer port-suffixed name
    c_old = httpx.Cookies(); c_old.set("SID", "xyz", domain="h")
    assert _session_cookie(c_old) == ("SID", "xyz")             # classic name
    assert _session_cookie(httpx.Cookies())[1] is None          # no cookie → no session
    print("qbittorrent self-check ok")


if __name__ == "__main__":
    _demo()
