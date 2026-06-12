"""F4.3: Alembic is the schema-version authority on boot — an already-built (create_all) schema is
stamped at head rather than replaying the incremental, create_all-dependent revisions."""
from __future__ import annotations

from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from app.db import _alembic_config, _sync_schema_version, engine, init_db


def _current():
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def test_sync_stamps_existing_schema_at_head_idempotently():
    init_db()  # build the full schema via create_all (+ additive columns)
    head = ScriptDirectory.from_config(_alembic_config()).get_current_head()
    assert head  # the chain has a head

    # Simulate an unstamped DB whose schema is already built (the real production state).
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")

    _sync_schema_version()
    assert _current() == head            # stamped at head (NOT replayed from base)

    _sync_schema_version()               # current == head → no-op
    assert _current() == head
