"""The production-DB destructive-op guard (INCIDENT-1, 2026-06-18)."""
from __future__ import annotations

import pytest

from app import safety


def test_disposable_db_detection():
    assert safety.db_is_disposable("sqlite:///tmp/shelf-test-abc/test.db")
    assert safety.db_is_disposable("sqlite:///:memory:")
    assert not safety.db_is_disposable("sqlite:///./shelf.db")
    assert not safety.db_is_disposable("postgresql://host/shelf_prod")


def test_guard_allows_under_test_db():
    # conftest points the suite at a throwaway tmp DB (and sets SHELF_ALLOW_DESTRUCTIVE=1), so the
    # guard the test fixtures call must NOT raise.
    safety.require_destructive_ok("unit test")  # no exception


def test_guard_refuses_a_prod_looking_db(monkeypatch):
    """With a prod-looking URL and the opt-in cleared, the guard must refuse."""
    monkeypatch.delenv("SHELF_ALLOW_DESTRUCTIVE", raising=False)
    monkeypatch.setattr(safety, "db_is_disposable", lambda url=None: False)
    with pytest.raises(RuntimeError, match="PRODUCTION database"):
        safety.require_destructive_ok("bulk delete")
    # …but the explicit operator opt-in re-enables it.
    monkeypatch.setenv("SHELF_ALLOW_DESTRUCTIVE", "1")
    safety.require_destructive_ok("bulk delete")  # no exception
