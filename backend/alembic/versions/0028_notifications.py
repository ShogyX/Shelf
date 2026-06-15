"""notifications: channels, in-app notifications, per-user event prefs

Adds the notification subsystem storage:
- notification_channels: per-user (or global, user_id NULL) delivery targets; structured config +
  the server-built Apprise URL.
- notifications: the per-user in-app feed (the header bell).
- user_settings.notify_prefs: {event_key: bool} explicit per-event overrides.

Idempotent (inspect-before-create) like 0027. Also folds any existing user_settings.apprise_url into
a kind='apprise' channel row (the apprise_url column is kept for back-compat, removed in a later
migration).

Revision ID: 0028_notifications
Revises: 0027_request_stats_outcome
Create Date: 2026-06-15
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0028_notifications"
down_revision: Union[str, None] = "0027_request_stats_outcome"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _migrate_apprise_urls(bind) -> None:
    """Fold each non-empty user_settings.apprise_url into a notification_channels row. Idempotent:
    skips a user who already has an imported channel."""
    rows = bind.execute(sa.text(
        "SELECT user_id, apprise_url FROM user_settings "
        "WHERE apprise_url IS NOT NULL AND TRIM(apprise_url) <> '' AND user_id IS NOT NULL"
    )).fetchall()
    for user_id, url in rows:
        exists = bind.execute(sa.text(
            "SELECT 1 FROM notification_channels WHERE user_id = :u AND apprise_url = :a LIMIT 1"
        ), {"u": user_id, "a": url}).first()
        if exists:
            continue
        bind.execute(sa.text(
            "INSERT INTO notification_channels (user_id, kind, label, config, apprise_url, enabled) "
            "VALUES (:u, 'apprise', 'Imported', '{}', :a, 1)"
        ), {"u": user_id, "a": url})


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    names = set(insp.get_table_names())

    if "notification_channels" not in names:
        op.create_table(
            "notification_channels",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("kind", sa.String(length=16), nullable=False),
            sa.Column("label", sa.String(length=64), nullable=True),
            sa.Column("config", sa.JSON(), nullable=True),
            sa.Column("apprise_url", sa.String(length=2048), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_notification_channels_user_id", "notification_channels", ["user_id"])

    if "notifications" not in names:
        op.create_table(
            "notifications",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("event_key", sa.String(length=48), nullable=False),
            sa.Column("title", sa.String(length=255), nullable=False),
            sa.Column("body", sa.Text(), nullable=False, server_default=""),
            sa.Column("level", sa.String(length=8), nullable=False, server_default="info"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_notif_user_unread", "notifications", ["user_id", "read_at"])
        op.create_index("ix_notif_user_created", "notifications", ["user_id", "created_at"])

    if "user_settings" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("user_settings")}
        if "notify_prefs" not in cols:
            op.add_column("user_settings", sa.Column("notify_prefs", sa.JSON(), nullable=True))

    _migrate_apprise_urls(bind)


def downgrade() -> None:
    for tbl in ("notifications", "notification_channels"):
        try:
            op.drop_table(tbl)
        except Exception:  # noqa: BLE001
            pass
    try:
        op.drop_column("user_settings", "notify_prefs")
    except Exception:  # noqa: BLE001
        pass
