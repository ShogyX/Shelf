"""SSRF-1: notify target allowlist is default-DENY and pins host-validated schemes to public IPs."""
from __future__ import annotations

from app.notify import _target_allowed


def test_unknown_scheme_denied():
    assert _target_allowed("file:///etc/passwd") is False
    assert _target_allowed("json://attacker/internal") is False
    assert _target_allowed("totallymadeup://x") is False


def test_private_and_metadata_hosts_denied():
    assert _target_allowed("ntfy://127.0.0.1/topic") is False
    assert _target_allowed("ntfy://169.254.169.254/topic") is False
    assert _target_allowed("ntfy://10.0.0.5/topic") is False
    assert _target_allowed("ntfy://localhost/topic") is False


def test_public_ntfy_allowed():
    # ntfy.sh resolves to a public address; the fixed-vendor path needs no host.
    assert _target_allowed("ntfy://ntfy.sh/mytopic") is True
    assert _target_allowed("discord://webhook_id/webhook_token") is True
