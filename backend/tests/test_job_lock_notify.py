"""A transient SQLite lock in a scheduler tick is logged but must NOT raise an admin notification."""
import sqlite3
from sqlalchemy.exc import OperationalError
from app.ingestion.scheduler import _is_transient_lock


def test_detects_locked_directly():
    assert _is_transient_lock(sqlite3.OperationalError("database is locked")) is True


def test_detects_locked_through_cause_chain():
    inner = sqlite3.OperationalError("database is locked")
    wrapped = OperationalError("stmt", {}, inner)  # SQLAlchemy wraps the dbapi error
    assert _is_transient_lock(wrapped) is True


def test_real_errors_still_notify():
    assert _is_transient_lock(ValueError("boom")) is False
    assert _is_transient_lock(KeyError("missing")) is False
    assert _is_transient_lock(sqlite3.OperationalError("no such table: x")) is False
