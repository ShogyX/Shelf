"""ARCH-H1: the schema-drift safety net catches a mapped column that never landed in the DB
(a new ``mapped_column`` added to a model but forgotten in ``_ADDITIVE_COLUMNS`` — create_all
won't ALTER an existing SQLite table, so the column silently goes missing on upgraded installs)."""
from __future__ import annotations

from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine

from app.db import init_db, schema_drift


def test_schema_in_sync_after_init():
    # init_db() builds the full schema (create_all + additive migrations) AND runs _check_schema_drift
    # internally on this disposable test DB — so simply reaching here without an AssertionError already
    # proves the real models are in sync. Assert it explicitly too.
    init_db()
    assert schema_drift() == {}


def test_schema_drift_detects_model_only_column():
    # Simulate an EXISTING DB that predates a newly-added mapped column: the DB table has only `id`,
    # while the ORM metadata declares an extra `newcol`. The net must flag the missing column.
    eng = create_engine("sqlite://")
    db_md = MetaData()
    Table("t", db_md, Column("id", Integer, primary_key=True))
    db_md.create_all(eng)  # DB side: only `id`

    model_md = MetaData()
    Table("t", model_md, Column("id", Integer, primary_key=True), Column("newcol", String))
    assert schema_drift(eng, model_md) == {"t": ["newcol"]}


def test_schema_drift_ignores_absent_table():
    # A table absent entirely is create_all's job, not the drift net's — it must NOT be reported
    # (otherwise a fresh/partial DB would false-alarm on every not-yet-created table).
    eng = create_engine("sqlite://")  # empty DB, no tables
    model_md = MetaData()
    Table("ghost", model_md, Column("id", Integer, primary_key=True))
    assert schema_drift(eng, model_md) == {}
