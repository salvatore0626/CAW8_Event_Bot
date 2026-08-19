from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any

from database import get_connection


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _json_text(value: Any) -> str | None:
    if value is None:
        return None
    if is_dataclass(value):
        value = asdict(value)
    return json.dumps(value, sort_keys=True, default=str)


def ensure_admin_log_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            user_discord_id TEXT,
            performed_by_id TEXT,
            before_json TEXT,
            after_json TEXT,
            reason TEXT,
            created_at INTEGER NOT NULL
        )
        """
    )

    # Older databases created admin_log with foreign keys back to users. That
    # prevents legitimate system actors (the bot itself) from being stored in
    # performed_by_id because bot accounts are intentionally not user records.
    # The current admin-log schema treats actor/target IDs as audit snapshots, so
    # migrate old copies to the FK-free form while preserving every log row.
    if conn.execute("PRAGMA foreign_key_list(admin_log)").fetchall():
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("ALTER TABLE admin_log RENAME TO admin_log_with_fks")
        conn.execute(
            """
            CREATE TABLE admin_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                user_discord_id TEXT,
                performed_by_id TEXT,
                before_json TEXT,
                after_json TEXT,
                reason TEXT,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO admin_log (
                id, action, user_discord_id, performed_by_id,
                before_json, after_json, reason, created_at
            )
            SELECT
                id, action, user_discord_id, performed_by_id,
                before_json, after_json, reason, created_at
            FROM admin_log_with_fks
            ORDER BY id ASC
            """
        )
        conn.execute("DROP TABLE admin_log_with_fks")
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")


def log_admin_action(
    *,
    action: str,
    user_discord_id: str | int | None = None,
    performed_by_id: str | int | None = None,
    before_json: Any = None,
    after_json: Any = None,
    reason: str | None = None,
    created_at: int | None = None,
) -> None:
    """Write one successful administrative data change to admin_log."""
    timestamp = int(created_at if created_at is not None else time.time())
    with get_connection() as conn:
        ensure_admin_log_table(conn)
        conn.execute(
            """
            INSERT INTO admin_log (
                action, user_discord_id, performed_by_id,
                before_json, after_json, reason, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _clean(action),
                _clean(user_discord_id),
                _clean(performed_by_id),
                _json_text(before_json),
                _json_text(after_json),
                _clean(reason),
                timestamp,
            ),
        )


@dataclass(frozen=True)
class AdminLogRecord:
    id: int
    action: str
    user_discord_id: str | None
    user_display_name: str | None
    performed_by_id: str | None
    performed_by_display_name: str | None
    before_json: str | None
    after_json: str | None
    reason: str | None
    created_at: int


def list_admin_log_records(*, limit: int | None = None) -> list[AdminLogRecord]:
    """Return admin-log rows newest first with best-effort user display names."""
    with get_connection() as conn:
        ensure_admin_log_table(conn)

        sql = """
            SELECT
                log.id,
                log.action,
                log.user_discord_id,
                COALESCE(target.display_name, target.discord_username) AS user_display_name,
                log.performed_by_id,
                COALESCE(actor.display_name, actor.discord_username) AS performed_by_display_name,
                log.before_json,
                log.after_json,
                log.reason,
                log.created_at
            FROM admin_log AS log
            LEFT JOIN users AS target
                ON target.discord_id = log.user_discord_id
            LEFT JOIN users AS actor
                ON actor.discord_id = log.performed_by_id
            ORDER BY log.created_at DESC, log.id DESC
        """
        params: tuple[Any, ...] = ()

        if limit is not None:
            safe_limit = max(1, int(limit))
            sql += "\nLIMIT ?"
            params = (safe_limit,)

        rows = conn.execute(sql, params).fetchall()

    return [
        AdminLogRecord(
            id=int(row["id"]),
            action=str(row["action"]),
            user_discord_id=(str(row["user_discord_id"]) if row["user_discord_id"] else None),
            user_display_name=(str(row["user_display_name"]) if row["user_display_name"] else None),
            performed_by_id=(str(row["performed_by_id"]) if row["performed_by_id"] else None),
            performed_by_display_name=(
                str(row["performed_by_display_name"])
                if row["performed_by_display_name"]
                else None
            ),
            before_json=row["before_json"],
            after_json=row["after_json"],
            reason=row["reason"],
            created_at=int(row["created_at"]),
        )
        for row in rows
    ]
