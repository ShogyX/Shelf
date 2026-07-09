from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import current_user, require_admin
from ..db import get_db
from ..models import AppSetting, User, UserSettings
from ..schemas import (
    CloudflareAccessIn, ContentLanguagesIn, GlobalSmtpIn, GlobalSmtpOut, SettingsIn, SettingsOut,
)

router = APIRouter()

# Admin-set GLOBAL DEFAULT index layout (category/genre order + hidden), applied to any user who
# hasn't customized their own. It is purely a DISPLAY preference: the catalog endpoints already
# enforce per-user allowed categories + 18+ gating server-side, so this can only reorder/hide
# content a user is already authorized to see — it can never reveal anything they can't access.
_INDEX_LAYOUT_KEY = "index_layout_default"


def _clean_layout(payload: dict) -> dict:
    """Normalize a layout to four string-lists (order/hidden for categories + genre lanes)."""
    def _strs(v):
        return [str(x) for x in (v or []) if isinstance(x, (str, int))]
    return {
        "categoryOrder": _strs((payload or {}).get("categoryOrder")),
        "hiddenCategories": _strs((payload or {}).get("hiddenCategories")),
        "laneOrder": _strs((payload or {}).get("laneOrder")),
        "hiddenLanes": _strs((payload or {}).get("hiddenLanes")),
    }


@router.get("/settings/index-layout")
def get_index_layout(
    _: User = Depends(current_user), db: Session = Depends(get_db)
) -> dict:
    """The global default index layout. Readable by any logged-in user so the client can apply it
    as the base layout for users who haven't customized their own."""
    row = db.get(AppSetting, _INDEX_LAYOUT_KEY)
    return _clean_layout(row.value if (row and isinstance(row.value, dict)) else {})


@router.put("/settings/index-layout", dependencies=[Depends(require_admin)])
def set_index_layout(payload: dict, db: Session = Depends(get_db)) -> dict:
    """Set the global default index layout (admin only). Stored as-is; it's applied client-side ON
    TOP of the already permission-filtered catalog, so it cannot leak restricted content."""
    layout = _clean_layout(payload)
    row = db.get(AppSetting, _INDEX_LAYOUT_KEY)
    if row is None:
        db.add(AppSetting(key=_INDEX_LAYOUT_KEY, value=layout))
    else:
        row.value = layout
    db.commit()
    return layout


# Admin-set rules for the Discover "Featured this week" billboard: how the title is chosen (method),
# which genre categories + media types it may draw from, and how often it rotates. Applied client-side
# on top of the already permission-filtered catalog, so it can only narrow what a user already sees.
_FEATURED_KEY = "featured_config"
_FEATURED_METHODS = ("popular", "random", "newest")


def _clean_featured(payload: dict) -> dict:
    p = payload or {}

    def _strs(v):
        return [str(x) for x in (v or []) if isinstance(x, (str, int))]

    method = str(p.get("method") or "popular").lower()
    if method not in _FEATURED_METHODS:
        method = "popular"
    try:
        rotate = max(0, min(24 * 30, int(p.get("rotateHours") or 0)))  # 0..30 days; 0 = every visit
    except (TypeError, ValueError):
        rotate = 0
    return {
        "method": method,
        "categories": _strs(p.get("categories")),
        "media": _strs(p.get("media")),
        "rotateHours": rotate,
    }


@router.get("/settings/featured")
def get_featured_config(
    _: User = Depends(current_user), db: Session = Depends(get_db)
) -> dict:
    """The global Featured-this-week selection rules. Readable by any user so the client can apply
    them when picking the billboard title."""
    row = db.get(AppSetting, _FEATURED_KEY)
    return _clean_featured(row.value if (row and isinstance(row.value, dict)) else {})


@router.put("/settings/featured", dependencies=[Depends(require_admin)])
def set_featured_config(payload: dict, db: Session = Depends(get_db)) -> dict:
    """Set the Featured-this-week rules (admin only)."""
    cfg = _clean_featured(payload)
    row = db.get(AppSetting, _FEATURED_KEY)
    if row is None:
        db.add(AppSetting(key=_FEATURED_KEY, value=cfg))
    else:
        row.value = cfg
    db.commit()
    return cfg

DEFAULT_READER_PREFS = {
    "fontFamily": "serif",
    "fontSize": 19,
    "lineHeight": 1.7,
    "letterSpacing": 0,
    "paragraphSpacing": 1.0,
    "measure": 38,
    "justify": False,
    "mode": "scroll",
    "textColor": "",
    "bgColor": "",
    "textLightness": None,
    "bgLightness": None,
    "fabSide": "right",      # legacy (unused) — kept so existing stored prefs round-trip cleanly
    "fabPos": 0.5,           # legacy (unused)
    "textPosition": 50,      # 0=left … 50=center … 100=right
    "audioSpeed": 1,         # audiobook default playback rate
    "audioSkipBack": 15,     # audiobook back-skip seconds
    "audioSkipForward": 30,  # audiobook forward-skip seconds
    "audioAutoplayNext": True,
}

# Delivery keys returned to the client (password is never returned).
# The SMTP server is now global (admin-configured); a user's delivery config holds only their own
# recipient ('email_to' private inbox; 'kindle_email' is a separate column).
def _delivery_view(cfg: dict) -> dict:
    return {"email_to": (cfg or {}).get("email_to")}


def _get_or_create(db: Session, user_id: int) -> UserSettings:
    s = db.scalar(select(UserSettings).where(UserSettings.user_id == user_id))
    if s is None:
        s = UserSettings(user_id=user_id, theme="system", reader_prefs=dict(DEFAULT_READER_PREFS))
        db.add(s)
        db.commit()
        db.refresh(s)
    return s


def _out(s, db: Session) -> SettingsOut:
    from ..kindle import app_smtp, smtp_configured

    prefs = {**DEFAULT_READER_PREFS, **(s.reader_prefs or {})}
    cfg = app_smtp(db)  # the global (admin) SMTP server
    return SettingsOut(
        theme=s.theme,
        reader_prefs=prefs,
        kindle_email=s.kindle_email,
        smtp_configured=smtp_configured(cfg),
        # The shared sending address — the user can see who their mail comes from (read-only).
        smtp_from=cfg.sender or None,
        delivery=_delivery_view(s.delivery_config or {}),
        apprise_url=s.apprise_url,
    )


@router.get("/settings", response_model=SettingsOut)
def get_settings_ep(
    user: User = Depends(current_user), db: Session = Depends(get_db)
) -> SettingsOut:
    return _out(_get_or_create(db, user.id), db)


@router.put("/settings", response_model=SettingsOut)
def update_settings_ep(
    payload: SettingsIn, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> SettingsOut:
    s = _get_or_create(db, user.id)
    if payload.theme is not None:
        s.theme = payload.theme
    if payload.reader_prefs is not None:
        s.reader_prefs = {**(s.reader_prefs or {}), **payload.reader_prefs}
    if payload.kindle_email is not None:
        s.kindle_email = payload.kindle_email.strip() or None
        # Adding a Kindle address auto-provisions a "Kindle" shelf that auto-sends new content there.
        if s.kindle_email:
            from ..library import ensure_named_shelf
            ensure_named_shelf(db, user.id, "Kindle", auto_kindle=True)
    if payload.apprise_url is not None:
        s.apprise_url = payload.apprise_url.strip() or None
    if payload.delivery is not None:
        # Only the user's own recipient is per-user now; the SMTP server is global/admin.
        cfg = dict(s.delivery_config or {})
        if "email_to" in payload.delivery:
            cfg["email_to"] = (payload.delivery["email_to"] or "").strip()
        s.delivery_config = cfg
    db.commit()
    db.refresh(s)
    return _out(s, db)


def _global_smtp_out(db: Session) -> GlobalSmtpOut:
    from ..kindle import app_smtp, get_global_smtp, smtp_configured
    g = get_global_smtp(db)
    cfg = app_smtp(db)
    return GlobalSmtpOut(
        smtp_host=g.get("smtp_host") or cfg.host or None,
        smtp_port=int(g.get("smtp_port") or cfg.port or 587),
        smtp_username=g.get("smtp_username") or cfg.username or None,
        smtp_from=g.get("smtp_from") or cfg.sender or None,
        smtp_security=g.get("smtp_security") or ("ssl" if cfg.ssl else "starttls"),
        smtp_password_set=bool(g.get("smtp_password") or cfg.password),
        configured=smtp_configured(cfg),
    )


@router.get("/settings/smtp", response_model=GlobalSmtpOut,
            dependencies=[Depends(require_admin)])
def get_global_smtp_ep(db: Session = Depends(get_db)) -> GlobalSmtpOut:
    """The shared, admin-configured SMTP server every user sends through (password never returned)."""
    return _global_smtp_out(db)


@router.put("/settings/smtp", response_model=GlobalSmtpOut,
            dependencies=[Depends(require_admin)])
def set_global_smtp_ep(payload: GlobalSmtpIn, db: Session = Depends(get_db)) -> GlobalSmtpOut:
    """Configure the shared SMTP server (admin only). Password is only updated when re-entered."""
    from ..kindle import set_global_smtp
    set_global_smtp(db, payload.model_dump(exclude_none=True))
    return _global_smtp_out(db)


# --------------------------------------------------------------- storage paths (admin)
def _storage_state(db: Session) -> dict:
    """Effective + overridable storage paths in one place: the app dirs (image cache / covers /
    backups), the stock central pool, and the SAB + libgen download paths. ``effective`` is the path
    in use right now; ``override`` is the admin-set value (blank → using the default)."""
    from .. import storage
    from ..backups_store import backups_dir
    from ..covers import covers_dir
    from ..ingestion.downloads import get_sabnzbd
    from ..ingestion.stock import get_stock_dir
    from ..media import media_dir
    from ..models import Integration, WatchedFolder

    def slot(key, effective):
        return {"override": storage.get(key), "effective": str(effective)}

    sab = get_sabnzbd(db) or db.scalar(select(Integration).where(Integration.kind == "sabnzbd"))
    sab_cfg = (sab.config or {}) if sab else {}
    lg = db.scalar(select(Integration).where(Integration.kind == "libgen"))
    folders = db.scalars(select(WatchedFolder).order_by(WatchedFolder.id)).all()
    return {
        "image_cache_dir": slot("media_dir", media_dir()),
        "covers_dir": slot("covers_dir", covers_dir()),
        "backups_dir": slot("backup_dir", backups_dir()),
        "stock_dir": get_stock_dir(db) or "",
        # The on-disk media pool is the source of truth; a user library is just pointers (LibraryItem)
        # into it. Uploads/web-hook content is ingested into this pool, not a separate folder.
        "sab_library_path": sab_cfg.get("library_path") or "",
        "sab_category": sab_cfg.get("category") or "shelf",
        "sab_path_mappings": sab_cfg.get("path_mappings") or [],
        "sab_configured": sab is not None,
        "libgen_download_dir": ((lg.config or {}).get("download_dir") if lg else "") or "",
        "libgen_configured": lg is not None,
        # Audiobooks are stored on their OWN path (separate from ebooks). Blank → a default derived at
        # import time (a sibling 'Audiobooks' dir next to the SAB library, or under the media dir).
        "audiobook_library_path": storage.audiobook_path(db),
        "watched_folders": [{"id": f.id, "path": f.path, "enabled": bool(f.enabled),
                             "name": f.display_name} for f in folders],
    }


@router.get("/settings/library-health", dependencies=[Depends(require_admin)])
def library_health(db: Session = Depends(get_db)) -> dict:
    """Admin summary of the background media-integrity scan: per-state counts over the LOCAL-file
    library plus the flagged (missing/corrupt) titles, so a broken file is an operator to-do item
    instead of a user-facing surprise. scanned/total show rotation coverage."""
    from sqlalchemy import func as _f
    from ..models import Work as _W
    local = (_W.local_path.is_not(None), _W.local_path != "", _W.hooked.is_(False))
    counts = dict(db.execute(
        select(_W.health, _f.count()).where(*local).group_by(_W.health)).all())
    scanned = db.scalar(select(_f.count()).select_from(_W).where(
        *local, _W.health_checked_at.is_not(None))) or 0
    total = db.scalar(select(_f.count()).select_from(_W).where(*local)) or 0
    flagged = db.scalars(select(_W).where(*local, _W.health.in_(("missing", "corrupt")))
                         .order_by(_W.health, _W.title).limit(200)).all()
    return {
        "total": total, "scanned": scanned,
        "ok": counts.get("ok", 0), "missing": counts.get("missing", 0),
        "corrupt": counts.get("corrupt", 0),
        "flagged": [{"id": w.id, "title": w.title, "author": w.author,
                     "media_kind": w.media_kind, "health": w.health,
                     "detail": w.health_detail,
                     "checked_at": w.health_checked_at.isoformat() if w.health_checked_at else None}
                    for w in flagged],
    }


@router.get("/settings/system", dependencies=[Depends(require_admin)])
def get_system_ep() -> dict:
    """Runtime-editable behavioral config (Settings → System): effective values + which are overridden."""
    from .. import config_store
    return {"values": config_store.all_effective(), "overridden": sorted(config_store.overridden())}


@router.put("/settings/system", dependencies=[Depends(require_admin)])
def set_system_ep(payload: dict, db: Session = Depends(get_db)) -> dict:
    """Apply runtime config overrides (admin). Only known keys are accepted; honored without a restart."""
    from .. import config_store
    return {"values": config_store.update(db, payload), "overridden": sorted(config_store.overridden())}


# Content languages Shelf can grab + stock — the crawler + metadata providers are queried per this
# set, and it's what the language badges/filters reflect. Small + explicit (English + Norwegian today).
SUPPORTED_CONTENT_LANGUAGES = [("en", "English"), ("no", "Norwegian")]


@router.get("/settings/content-languages")
def get_content_languages(_: User = Depends(current_user)) -> dict:
    """Which languages the instance grabs + stocks. Readable by any signed-in user (visibility — it
    drives the language badges they see); only admins change it. ``enabled`` = canonical codes."""
    from .. import config_store
    return {
        "supported": [{"code": c, "name": n} for c, n in SUPPORTED_CONTENT_LANGUAGES],
        "enabled": config_store.content_languages(),
    }


@router.put("/settings/content-languages", dependencies=[Depends(require_admin)])
def set_content_languages(payload: ContentLanguagesIn, db: Session = Depends(get_db)) -> dict:
    """Set which languages Shelf grabs + stocks (admin). Only supported codes are honoured; clearing
    the selection falls back to English (never 'all', so an accidental empty can't unleash every
    language). Persists the ``content_languages`` runtime setting — no restart needed."""
    from .. import config_store
    supported = {c for c, _ in SUPPORTED_CONTENT_LANGUAGES}
    codes = [c for c in dict.fromkeys(payload.languages) if c in supported] or ["en"]
    config_store.update(db, {"content_languages": ",".join(codes)})
    return {"enabled": config_store.content_languages()}


def _cf_access_out(cfg: dict) -> dict:
    """Public shape of the Cloudflare Access config — the API token is never returned, only whether
    one is stored (``api_token_set``)."""
    return {
        "account_id": cfg.get("account_id", ""),
        "app_id": cfg.get("app_id", ""),
        "policy_id": cfg.get("policy_id", ""),
        "enabled": bool(cfg.get("enabled")),
        "api_token_set": bool(cfg.get("api_token")),
    }


@router.get("/settings/cloudflare-access", dependencies=[Depends(require_admin)])
def get_cloudflare_access(db: Session = Depends(get_db)) -> dict:
    """The Cloudflare Access integration config (admin). Used so creating a Shelf user can add their
    email to an Access application policy automatically. The stored API token is never returned."""
    from ..integrations import cloudflare
    return _cf_access_out(cloudflare.get_config(db))


@router.put("/settings/cloudflare-access", dependencies=[Depends(require_admin)])
def set_cloudflare_access(payload: CloudflareAccessIn, db: Session = Depends(get_db)) -> dict:
    """Update the Cloudflare Access config (admin). A blank ``api_token`` preserves the stored one."""
    from ..integrations import cloudflare
    cfg = cloudflare.set_config(db, payload.model_dump(exclude_none=True))
    return _cf_access_out(cfg)


@router.post("/settings/cloudflare-access/test", dependencies=[Depends(require_admin)])
def test_cloudflare_access(db: Session = Depends(get_db)) -> dict:
    """Verify the Cloudflare Access credentials by fetching the configured policy (admin)."""
    from ..integrations import cloudflare
    cfg = cloudflare.get_config(db)
    if not cloudflare.is_configured(cfg):
        raise HTTPException(400, "Set the API token, account, application and policy IDs, and enable it first.")
    try:
        cloudflare.test(cfg)
    except Exception as e:  # noqa: BLE001 — surface a clean message to the admin
        raise HTTPException(502, f"Cloudflare test failed: {e}")
    return {"ok": True}


@router.get("/settings/storage", dependencies=[Depends(require_admin)])
def get_storage_ep(db: Session = Depends(get_db)) -> dict:
    return _storage_state(db)


def _migrate_dir(old: str, new: str) -> int:
    """Move the contents of ``old`` into ``new`` (best-effort, skip-existing). Fast (rename) on the
    same filesystem; a recursive copy across mounts. Returns the number of top-level entries moved."""
    import os
    import shutil
    if not old or not new or os.path.abspath(old) == os.path.abspath(new) or not os.path.isdir(old):
        return 0
    os.makedirs(new, exist_ok=True)
    moved = 0
    for name in os.listdir(old):
        src, dst = os.path.join(old, name), os.path.join(new, name)
        if os.path.exists(dst):
            continue
        try:
            shutil.move(src, dst)
            moved += 1
        except OSError:
            continue
    return moved


@router.put("/settings/storage", dependencies=[Depends(require_admin)])
def set_storage_ep(payload: dict, db: Session = Depends(get_db)) -> dict:
    """Update storage paths (admin). Only keys present are changed; blank reverts an app dir to its
    default. Re-points where NEW files are written/read. With ``migrate: true`` the existing contents
    of each changed directory are MOVED to the new location too."""
    from .. import storage
    from ..backups_store import backups_dir
    from ..covers import covers_dir
    from ..ingestion.stock import get_stock_dir, set_stock_dir
    from ..media import media_dir
    from ..models import Integration

    migrate = bool(payload.get("migrate"))
    # Snapshot the OLD effective dirs before re-pointing, so a migration knows where to move from.
    old = ({"media_dir": str(media_dir()), "covers_dir": str(covers_dir()),
            "backup_dir": str(backups_dir()), "stock_dir": get_stock_dir(db) or ""}
           if migrate else {})

    app_patch = {k: payload[k] for k in ("media_dir", "covers_dir", "backup_dir") if k in payload}
    if app_patch:
        storage.update(db, app_patch)
    if "stock_dir" in payload:
        set_stock_dir(db, (payload.get("stock_dir") or "").strip() or None)

    migrated: dict[str, int] = {}
    if migrate:
        new = {"media_dir": str(media_dir()), "covers_dir": str(covers_dir()),
               "backup_dir": str(backups_dir()), "stock_dir": get_stock_dir(db) or ""}
        for key, old_path in old.items():
            n = _migrate_dir(old_path, new[key])
            if n:
                migrated[key] = n
    # SAB download paths (only when those keys are sent + a SAB integration exists).
    sab_keys = {"sab_library_path": "library_path", "sab_category": "category",
                "sab_path_mappings": "path_mappings"}
    if any(k in payload for k in sab_keys):
        sab = db.scalar(select(Integration).where(Integration.kind == "sabnzbd"))
        if sab is not None:
            cfg = dict(sab.config or {})
            for pk, ck in sab_keys.items():
                if pk in payload:
                    val = payload[pk]
                    if ck == "path_mappings":
                        val = [{"remote": str(m.get("remote", "")).strip(),
                                "local": str(m.get("local", "")).strip()}
                               for m in (val or []) if (m.get("remote") or m.get("local"))]
                    else:
                        val = (val or "").strip()
                    cfg[ck] = val
            sab.config = cfg
            db.commit()
    if "libgen_download_dir" in payload:
        lg = db.scalar(select(Integration).where(Integration.kind == "libgen"))
        if lg is not None:
            cfg = dict(lg.config or {})
            cfg["download_dir"] = (payload.get("libgen_download_dir") or "").strip()
            lg.config = cfg
            db.commit()
    if "audiobook_library_path" in payload:
        storage.set_audiobook_path(db, payload.get("audiobook_library_path"))
    return {**_storage_state(db), "migrated": migrated}
