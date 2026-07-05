"""Cloudflare Access integration.

Adds/removes a Shelf user's email on a Cloudflare Zero Trust **Access application policy**, so an
admin who creates (or approves) a Shelf account doesn't have to add the email in the Cloudflare
dashboard by hand. Config is admin-only (AppSetting ``cloudflare_access``): an API token (needs
*Access: Apps and Policies: Edit*), the account id, the Access application id and the policy id.

Best-effort: a Cloudflare error NEVER blocks the Shelf user operation — it's logged and surfaced via
the settings "Test" button. Uses a synchronous httpx client (the user endpoints are sync).
"""
from __future__ import annotations

import logging

import httpx
from sqlalchemy.orm import Session

log = logging.getLogger("shelf.cloudflare")

_KEY = "cloudflare_access"                       # AppSetting key
_API = "https://api.cloudflare.com/client/v4"
_SECRET = "api_token"
_TIMEOUT = 15.0


# ---------------------------------------------------------------- config (AppSetting)
def get_config(db: Session) -> dict:
    from ..models import AppSetting
    row = db.get(AppSetting, _KEY)
    return dict(row.value) if (row and isinstance(row.value, dict)) else {}


def set_config(db: Session, patch: dict) -> dict:
    """Merge ``patch`` into the stored config. A blank ``api_token`` is IGNORED (write-only: the
    existing secret is preserved when the UI doesn't send a fresh one)."""
    from ..models import AppSetting
    cfg = get_config(db)
    for k, v in patch.items():
        if k == _SECRET and (v is None or v == ""):
            continue
        cfg[k] = v
    row = db.get(AppSetting, _KEY)
    if row is None:
        db.add(AppSetting(key=_KEY, value=cfg))
    else:
        row.value = cfg
    db.commit()
    return cfg


def is_configured(cfg: dict) -> bool:
    """True only when enabled AND every field needed to reach the policy is present."""
    return bool(cfg.get("enabled") and cfg.get(_SECRET) and cfg.get("account_id")
                and cfg.get("app_id") and cfg.get("policy_id"))


# ---------------------------------------------------------------- pure include-list helpers
def _is_email_rule(rule, email: str) -> bool:
    return (isinstance(rule, dict) and isinstance(rule.get("email"), dict)
            and (rule["email"].get("email") or "").lower() == email.lower())


def add_to_include(include: list, email: str) -> list:
    """Return ``include`` with an ``{email:{email}}`` rule for ``email`` (idempotent)."""
    if any(_is_email_rule(r, email) for r in include):
        return list(include)
    return list(include) + [{"email": {"email": email}}]


def remove_from_include(include: list, email: str) -> list:
    """Return ``include`` without any email rule matching ``email`` (case-insensitive)."""
    return [r for r in include if not _is_email_rule(r, email)]


# ---------------------------------------------------------------- HTTP
def _policy_url(cfg: dict) -> str:
    return f"{_API}/accounts/{cfg['account_id']}/access/apps/{cfg['app_id']}/policies/{cfg['policy_id']}"


def _headers(cfg: dict) -> dict:
    return {"Authorization": f"Bearer {cfg[_SECRET]}", "Content-Type": "application/json"}


def _get_policy(cfg: dict) -> dict:
    with httpx.Client(timeout=_TIMEOUT) as c:
        r = c.get(_policy_url(cfg), headers=_headers(cfg))
    r.raise_for_status()
    return r.json()["result"]


# Read-only fields Cloudflare returns on GET but rejects (or ignores) on PUT.
_READONLY_POLICY_FIELDS = {"id", "uid", "created_at", "updated_at"}


def _mutate_include(cfg: dict, mutate) -> None:
    """GET the policy, transform ONLY its ``include`` list via ``mutate``, and PUT the whole policy
    back. Cloudflare's PUT replaces the policy, so we re-send every field the admin configured
    (name, decision, exclude, require, session_duration, isolation…) untouched — only ``include`` is
    ours — otherwise those settings would reset to defaults."""
    policy = _get_policy(cfg)
    body = {k: v for k, v in policy.items() if k not in _READONLY_POLICY_FIELDS}
    body["include"] = mutate(policy.get("include") or [])
    body.setdefault("name", "Shelf users")
    body.setdefault("decision", "allow")
    with httpx.Client(timeout=_TIMEOUT) as c:
        r = c.put(_policy_url(cfg), headers=_headers(cfg), json=body)
    r.raise_for_status()


def test(cfg: dict) -> None:
    """Verify connectivity + credentials by fetching the policy. Raises on any failure."""
    _get_policy(cfg)


# ---------------------------------------------------------------- best-effort user hooks
def add_user_email(db: Session, email: str | None) -> None:
    """Add ``email`` to the configured Access policy (no-op if unconfigured/disabled). Never raises."""
    if not email:
        return
    cfg = get_config(db)
    if not is_configured(cfg):
        return
    try:
        _mutate_include(cfg, lambda inc: add_to_include(inc, email))
        log.info("cloudflare access: added %s to policy", email)
    except Exception:  # noqa: BLE001 — never block the Shelf user op
        log.exception("cloudflare access: failed to add %s", email)


def remove_user_email(db: Session, email: str | None) -> None:
    """Remove ``email`` from the configured Access policy (no-op if unconfigured). Never raises."""
    if not email:
        return
    cfg = get_config(db)
    if not is_configured(cfg):
        return
    try:
        _mutate_include(cfg, lambda inc: remove_from_include(inc, email))
        log.info("cloudflare access: removed %s from policy", email)
    except Exception:  # noqa: BLE001
        log.exception("cloudflare access: failed to remove %s", email)
