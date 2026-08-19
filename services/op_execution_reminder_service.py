from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime

from database import get_connection
from services.user_settings_service import safe_zoneinfo


OPENER_REMINDER_SECONDS = 30 * 60
GLOBAL_COMPLETE_REMINDER_SECONDS = 4 * 60 * 60
GLOBAL_CANCEL_REMINDER_SECONDS = 4 * 60 * 60
# Allows a due reminder to wait for a user's notification window without
# surfacing very old/stale events after a long bot outage.
GLOBAL_REMINDER_GRACE_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class OpenedOpContext:
    event_id: int
    op_name: str
    opener_id: int
    opened_at: int


@dataclass(frozen=True)
class OperationReminderEvent:
    event_id: int
    op_name: str
    status: str
    scheduled_at: int
    updated_at: int


@dataclass(frozen=True)
class OperationReminderSubscriber:
    discord_id: str
    timezone: str | None
    notify_start: str | None
    notify_end: str | None


_OPENED_OPS: dict[int, OpenedOpContext] = {}


def now_ts() -> int:
    return int(time.time())


def remember_op_opened(
    *,
    event_id: int,
    op_name: str,
    opener_id: int,
    opened_at: int | None = None,
) -> None:
    """Remember who opened an op for the 30-minute personal reminder.

    This is intentionally in-memory only. The opener association does not need
    to survive a bot restart.
    """
    _OPENED_OPS[int(event_id)] = OpenedOpContext(
        event_id=int(event_id),
        op_name=str(op_name),
        opener_id=int(opener_id),
        opened_at=now_ts() if opened_at is None else int(opened_at),
    )


def forget_op_opened(event_id: int) -> None:
    _OPENED_OPS.pop(int(event_id), None)


def opened_op_contexts() -> list[OpenedOpContext]:
    return list(_OPENED_OPS.values())


def get_event(event_id: int) -> OperationReminderEvent | None:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                oe.event_id,
                ot.name AS op_name,
                oe.status,
                oe.scheduled_at,
                oe.updated_at
            FROM op_events oe
            JOIN op_templates ot
                ON ot.id = oe.op_template_id
            WHERE oe.event_id = ?
            LIMIT 1
            """,
            (int(event_id),),
        ).fetchone()

    if row is None:
        return None

    return OperationReminderEvent(
        event_id=int(row["event_id"]),
        op_name=str(row["op_name"]),
        status=str(row["status"]),
        scheduled_at=int(row["scheduled_at"]),
        updated_at=int(row["updated_at"]),
    )


def due_open_events_for_global_reminder(
    *,
    current_ts: int | None = None,
) -> list[OperationReminderEvent]:
    current = now_ts() if current_ts is None else int(current_ts)
    due_at_or_before = current - GLOBAL_COMPLETE_REMINDER_SECONDS
    oldest_open_time = due_at_or_before - GLOBAL_REMINDER_GRACE_SECONDS

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                oe.event_id,
                ot.name AS op_name,
                oe.status,
                oe.scheduled_at,
                oe.updated_at
            FROM op_events oe
            JOIN op_templates ot
                ON ot.id = oe.op_template_id
            WHERE oe.status = 'Open'
              AND oe.updated_at <= ?
              AND oe.updated_at > ?
            ORDER BY oe.updated_at ASC, oe.event_id ASC
            """,
            (int(due_at_or_before), int(oldest_open_time)),
        ).fetchall()

    return [
        OperationReminderEvent(
            event_id=int(row["event_id"]),
            op_name=str(row["op_name"]),
            status=str(row["status"]),
            scheduled_at=int(row["scheduled_at"]),
            updated_at=int(row["updated_at"]),
        )
        for row in rows
    ]


def due_scheduled_events_for_cancel_reminder(
    *,
    current_ts: int | None = None,
) -> list[OperationReminderEvent]:
    current = now_ts() if current_ts is None else int(current_ts)
    due_at_or_before = current - GLOBAL_CANCEL_REMINDER_SECONDS
    oldest_scheduled_time = due_at_or_before - GLOBAL_REMINDER_GRACE_SECONDS

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                oe.event_id,
                ot.name AS op_name,
                oe.status,
                oe.scheduled_at,
                oe.updated_at
            FROM op_events oe
            JOIN op_templates ot
                ON ot.id = oe.op_template_id
            WHERE oe.status = 'Scheduled'
              AND oe.scheduled_at <= ?
              AND oe.scheduled_at > ?
            ORDER BY oe.scheduled_at ASC, oe.event_id ASC
            """,
            (int(due_at_or_before), int(oldest_scheduled_time)),
        ).fetchall()

    return [
        OperationReminderEvent(
            event_id=int(row["event_id"]),
            op_name=str(row["op_name"]),
            status=str(row["status"]),
            scheduled_at=int(row["scheduled_at"]),
            updated_at=int(row["updated_at"]),
        )
        for row in rows
    ]


def operation_reminder_subscribers() -> list[OperationReminderSubscriber]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                us.discord_id,
                us.timezone,
                us.notify_start,
                us.notify_end
            FROM user_settings us
            JOIN users u
                ON u.discord_id = us.discord_id
            WHERE COALESCE(us.notify_operations, 0) = 1
              AND u.status = 'Active'
              AND us.discord_id IS NOT NULL
            ORDER BY us.discord_id ASC
            """
        ).fetchall()

    return [
        OperationReminderSubscriber(
            discord_id=str(row["discord_id"]),
            timezone=str(row["timezone"]) if row["timezone"] is not None else None,
            notify_start=str(row["notify_start"]) if row["notify_start"] is not None else None,
            notify_end=str(row["notify_end"]) if row["notify_end"] is not None else None,
        )
        for row in rows
    ]


def operation_reminder_setting(discord_id: str) -> OperationReminderSubscriber | None:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                us.discord_id,
                us.timezone,
                us.notify_start,
                us.notify_end
            FROM user_settings us
            JOIN users u
                ON u.discord_id = us.discord_id
            WHERE us.discord_id = ?
              AND COALESCE(us.notify_operations, 0) = 1
              AND u.status = 'Active'
            LIMIT 1
            """,
            (str(discord_id),),
        ).fetchone()

    if row is None:
        return None

    return OperationReminderSubscriber(
        discord_id=str(row["discord_id"]),
        timezone=str(row["timezone"]) if row["timezone"] is not None else None,
        notify_start=str(row["notify_start"]) if row["notify_start"] is not None else None,
        notify_end=str(row["notify_end"]) if row["notify_end"] is not None else None,
    )


def parse_notification_time(value: str | None, default: str) -> int:
    text = str(value or default).strip()

    try:
        hour_text, minute_text = text.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except (TypeError, ValueError):
        hour_text, minute_text = default.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)

    hour = max(0, min(23, hour))
    minute = max(0, min(59, minute))
    return hour * 60 + minute


def is_within_notification_window(subscriber: OperationReminderSubscriber) -> bool:
    local_now = datetime.now(safe_zoneinfo(subscriber.timezone))
    now_minutes = local_now.hour * 60 + local_now.minute
    start_minutes = parse_notification_time(subscriber.notify_start, "09:00")
    end_minutes = parse_notification_time(subscriber.notify_end, "21:00")

    if start_minutes == end_minutes:
        return True

    if start_minutes < end_minutes:
        return start_minutes <= now_minutes < end_minutes

    # Overnight notification window, for example 21:00 -> 09:00.
    return now_minutes >= start_minutes or now_minutes < end_minutes
