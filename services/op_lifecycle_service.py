from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from database import get_connection

from services.schedule_service import (
    clean_text,
    format_timestamp_short,
    get_user_timezone,
    now_ts,
)


ACTIVE_LIFECYCLE_STATUSES = ("Briefing", "Open")


@dataclass
class LifecycleOp:
    event_id: int
    op_template_id: int
    op_name: str
    op_type: str
    scheduled_at: int
    status: str
    reservation_slots: int
    server_event_id: str | None


@dataclass
class ActiveConflict:
    event_id: int
    op_name: str
    status: str
    scheduled_at: int


def row_to_lifecycle_op(row: Any) -> LifecycleOp:
    return LifecycleOp(
        event_id=int(row["event_id"]),
        op_template_id=int(row["op_template_id"]),
        op_name=str(row["op_name"]),
        op_type=str(row["op_type"]),
        scheduled_at=int(row["scheduled_at"]),
        status=str(row["status"]),
        reservation_slots=int(row["reservation_slots"] or 0),
        server_event_id=clean_text(row["server_event_id"]),
    )


def get_lifecycle_op(event_id: int) -> LifecycleOp | None:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                op_events.event_id,
                op_events.op_template_id,
                op_templates.name AS op_name,
                op_templates.type AS op_type,
                op_events.scheduled_at,
                op_events.status,
                op_events.reservation_slots,
                op_events.server_event_id
            FROM op_events
            JOIN op_templates
                ON op_templates.id = op_events.op_template_id
            WHERE op_events.event_id = ?
            LIMIT 1
            """,
            (int(event_id),),
        ).fetchone()

    if row is None:
        return None

    return row_to_lifecycle_op(row)


def search_lifecycle_ops(
    *,
    query: str | None = None,
    limit: int = 25,
    include_complete: bool = False,
    statuses: list[str] | tuple[str, ...] | None = None,
) -> list[LifecycleOp]:
    q = clean_text(query)

    if statuses is None:
        status_values = ["Scheduled", "Briefing", "Open", "Canceled"]

        if include_complete:
            status_values.append("Complete")
    else:
        status_values = [str(status) for status in statuses if clean_text(status)]

    if not status_values:
        return []

    statuses = status_values

    placeholders = ",".join("?" for _ in statuses)
    params: list[Any] = statuses[:]

    where_query = ""

    if q:
        where_query = """
          AND (
                CAST(op_events.event_id AS TEXT) LIKE ?
             OR op_templates.name LIKE ?
          )
        """
        like = f"%{q}%"
        params.extend([like, like])

    params.append(int(limit))

    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT
                op_events.event_id,
                op_events.op_template_id,
                op_templates.name AS op_name,
                op_templates.type AS op_type,
                op_events.scheduled_at,
                op_events.status,
                op_events.reservation_slots,
                op_events.server_event_id
            FROM op_events
            JOIN op_templates
                ON op_templates.id = op_events.op_template_id
            WHERE op_events.status IN ({placeholders})
            {where_query}
            ORDER BY op_events.scheduled_at ASC, op_events.event_id ASC
            LIMIT ?
            """,
            params,
        ).fetchall()

    return [row_to_lifecycle_op(row) for row in rows]



def recent_completed_lifecycle_ops(limit: int = 3) -> list[LifecycleOp]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                op_events.event_id,
                op_events.op_template_id,
                op_templates.name AS op_name,
                op_templates.type AS op_type,
                op_events.scheduled_at,
                op_events.status,
                op_events.reservation_slots,
                op_events.server_event_id
            FROM op_events
            JOIN op_templates
                ON op_templates.id = op_events.op_template_id
            WHERE op_events.status = 'Complete'
            ORDER BY op_events.scheduled_at DESC, op_events.event_id DESC
            LIMIT ?
            """,
            (max(0, int(limit)),),
        ).fetchall()

    return [row_to_lifecycle_op(row) for row in rows]


def option_label(op: LifecycleOp, timezone_name: str) -> str:
    return f"#{op.event_id} {format_timestamp_short(op.scheduled_at, timezone_name)} {op.op_name}"[:100]


def option_description(op: LifecycleOp) -> str:
    return f"{op.status} | {op.op_type}"[:100]


def safe_zoneinfo(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except Exception:
        return ZoneInfo("UTC")


def is_event_today(
    *,
    scheduled_at: int,
    timezone_name: str,
) -> bool:
    tz = safe_zoneinfo(timezone_name)
    event_date = datetime.fromtimestamp(int(scheduled_at), tz).date()
    today = datetime.now(tz).date()

    return event_date == today


def when_label(
    *,
    scheduled_at: int,
    timezone_name: str,
) -> str:
    prefix = "" if is_event_today(scheduled_at=scheduled_at, timezone_name=timezone_name) else "⚠️ "
    return f"{prefix}When"


def get_active_conflict(
    *,
    exclude_event_id: int | None = None,
) -> ActiveConflict | None:
    params: list[Any] = []

    exclude_sql = ""

    if exclude_event_id is not None:
        exclude_sql = "AND op_events.event_id != ?"
        params.append(int(exclude_event_id))

    with get_connection() as conn:
        row = conn.execute(
            f"""
            SELECT
                op_events.event_id,
                op_templates.name AS op_name,
                op_events.status,
                op_events.scheduled_at
            FROM op_events
            JOIN op_templates
                ON op_templates.id = op_events.op_template_id
            WHERE op_events.status IN ('Briefing', 'Open')
            {exclude_sql}
            ORDER BY
                CASE op_events.status
                    WHEN 'Open' THEN 0
                    WHEN 'Briefing' THEN 1
                    ELSE 2
                END,
                op_events.updated_at DESC,
                op_events.event_id DESC
            LIMIT 1
            """,
            params,
        ).fetchone()

    if row is None:
        return None

    return ActiveConflict(
        event_id=int(row["event_id"]),
        op_name=str(row["op_name"]),
        status=str(row["status"]),
        scheduled_at=int(row["scheduled_at"]),
    )


def get_open_conflict(
    *,
    exclude_event_id: int | None = None,
) -> ActiveConflict | None:
    params: list[Any] = []

    exclude_sql = ""

    if exclude_event_id is not None:
        exclude_sql = "AND op_events.event_id != ?"
        params.append(int(exclude_event_id))

    with get_connection() as conn:
        row = conn.execute(
            f"""
            SELECT
                op_events.event_id,
                op_templates.name AS op_name,
                op_events.status,
                op_events.scheduled_at
            FROM op_events
            JOIN op_templates
                ON op_templates.id = op_events.op_template_id
            WHERE op_events.status = 'Open'
            {exclude_sql}
            ORDER BY op_events.updated_at DESC, op_events.event_id DESC
            LIMIT 1
            """,
            params,
        ).fetchone()

    if row is None:
        return None

    return ActiveConflict(
        event_id=int(row["event_id"]),
        op_name=str(row["op_name"]),
        status=str(row["status"]),
        scheduled_at=int(row["scheduled_at"]),
    )


def get_briefing_conflict(
    *,
    exclude_event_id: int | None = None,
) -> ActiveConflict | None:
    params: list[Any] = []

    exclude_sql = ""

    if exclude_event_id is not None:
        exclude_sql = "AND op_events.event_id != ?"
        params.append(int(exclude_event_id))

    with get_connection() as conn:
        row = conn.execute(
            f"""
            SELECT
                op_events.event_id,
                op_templates.name AS op_name,
                op_events.status,
                op_events.scheduled_at
            FROM op_events
            JOIN op_templates
                ON op_templates.id = op_events.op_template_id
            WHERE op_events.status = 'Briefing'
            {exclude_sql}
            ORDER BY op_events.updated_at DESC, op_events.event_id DESC
            LIMIT 1
            """,
            params,
        ).fetchone()

    if row is None:
        return None

    return ActiveConflict(
        event_id=int(row["event_id"]),
        op_name=str(row["op_name"]),
        status=str(row["status"]),
        scheduled_at=int(row["scheduled_at"]),
    )


def start_op(event_id: int) -> None:
    ts = now_ts()

    with get_connection() as conn:
        event = conn.execute(
            """
            SELECT status
            FROM op_events
            WHERE event_id = ?
            LIMIT 1
            """,
            (int(event_id),),
        ).fetchone()

        if event is None:
            raise ValueError("That scheduled op does not exist.")

        if str(event["status"]) != "Scheduled":
            raise ValueError("Only scheduled ops can be started.")

        conn.execute(
            """
            UPDATE op_events
            SET status = 'Briefing',
                updated_at = ?
            WHERE event_id = ?
            """,
            (
                ts,
                int(event_id),
            ),
        )

        conn.execute(
            """
            UPDATE op_reservations
            SET status = 'locked'
            WHERE op_event_id = ?
              AND status IN ('open', 'reserved')
            """,
            (int(event_id),),
        )


def complete_op(event_id: int) -> None:
    ts = now_ts()

    with get_connection() as conn:
        event = conn.execute(
            """
            SELECT status
            FROM op_events
            WHERE event_id = ?
            LIMIT 1
            """,
            (int(event_id),),
        ).fetchone()

        if event is None:
            raise ValueError("That scheduled op does not exist.")

        if str(event["status"]) != "Open":
            raise ValueError("Only open ops can be completed.")

        conn.execute(
            """
            UPDATE op_events
            SET status = 'Complete',
                updated_at = ?
            WHERE event_id = ?
            """,
            (
                ts,
                int(event_id),
            ),
        )

        # User said open attendance rows should become complete.
        # This assumes attendance.status CHECK includes 'complete'.
        conn.execute(
            """
            UPDATE attendance
            SET status = 'complete',
                updated_at = ?
            WHERE scheduled_op_id = ?
              AND status = 'open'
            """,
            (
                ts,
                int(event_id),
            ),
        )


def set_op_open(event_id: int) -> None:
    ts = now_ts()

    with get_connection() as conn:
        event = conn.execute(
            """
            SELECT status
            FROM op_events
            WHERE event_id = ?
            LIMIT 1
            """,
            (int(event_id),),
        ).fetchone()

        if event is None:
            raise ValueError("That scheduled op does not exist.")

        if str(event["status"]) not in {"Scheduled", "Briefing", "Complete"}:
            raise ValueError("Only scheduled, briefing, or completed ops can be opened.")

        conn.execute(
            """
            UPDATE op_events
            SET status = 'Open',
                updated_at = ?
            WHERE event_id = ?
            """,
            (
                ts,
                int(event_id),
            ),
        )

        if str(event["status"]) == "Complete":
            conn.execute(
                """
                UPDATE attendance
                SET status = 'open',
                    updated_at = ?
                WHERE scheduled_op_id = ?
                  AND status = 'complete'
                """,
                (
                    ts,
                    int(event_id),
                ),
            )


def flight_templates_for_event(event_id: int) -> list[dict[str, Any]]:
    op = get_lifecycle_op(event_id)

    if op is None:
        return []

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                id,
                flight_index,
                flight_letter,
                flight_name,
                aircraft,
                aircraft_count,
                slot_count,
                description
            FROM flight_templates
            WHERE op_template_id = ?
            ORDER BY flight_index ASC, id ASC
            """,
            (int(op.op_template_id),),
        ).fetchall()

    return [dict(row) for row in rows]


def attendance_exists(event_id: int) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM attendance
            WHERE scheduled_op_id = ?
            LIMIT 1
            """,
            (int(event_id),),
        ).fetchone()

    return row is not None


def create_attendance_slots_for_event(event_id: int) -> int:
    op = get_lifecycle_op(event_id)

    if op is None:
        raise ValueError("That scheduled op does not exist.")

    if attendance_exists(event_id):
        return 0

    flights = flight_templates_for_event(event_id)
    ts = now_ts()
    created = 0

    with get_connection() as conn:
        entry_slot_index = 0

        for flight in flights:
            slot_count = int(flight["slot_count"] or 0)

            for _ in range(1, slot_count + 1):
                conn.execute(
                    """
                    INSERT INTO attendance (
                        scheduled_op_id,
                        op_template_name,
                        entry_slot_index,
                        discord_id,
                        user_name,
                        slot,
                        aircraft,
                        combat_deaths,
                        landing_type,
                        wires,
                        bolters,
                        attend_type,
                        op_remarks,
                        fl_remarks,
                        note_remarks,
                        flight_lead_rating,
                        status,
                        type,
                        created_at,
                        logged_at,
                        updated_at
                    )
                    VALUES (
                        ?, ?, ?, NULL, NULL,
                        NULL, NULL,
                        NULL, NULL, NULL, NULL,
                        'normal',
                        NULL,
                        NULL,
                        NULL,
                        NULL,
                        'open',
                        'normal',
                        ?, NULL, ?
                    )
                    """,
                    (
                        int(event_id),
                        op.op_name,
                        int(entry_slot_index),
                        ts,
                        ts,
                    ),
                )

                entry_slot_index += 1
                created += 1

    return created


def open_op_and_create_attendance(event_id: int) -> int:
    set_op_open(event_id)
    return create_attendance_slots_for_event(event_id)


def complete_then_open(
    *,
    complete_event_id: int,
    open_event_id: int,
) -> int:
    complete_op(complete_event_id)
    return open_op_and_create_attendance(open_event_id)


def complete_then_start(
    *,
    complete_event_id: int,
    start_event_id: int,
) -> None:
    complete_op(complete_event_id)
    start_op(start_event_id)
