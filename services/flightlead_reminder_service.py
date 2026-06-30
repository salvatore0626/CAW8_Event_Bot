from __future__ import annotations

import time
from dataclasses import dataclass

from database import get_connection


def now_ts() -> int:
    return int(time.time())


@dataclass(frozen=True)
class FlightLeadReminderCandidate:
    event_id: int
    op_template_id: int
    op_name: str
    op_type: str
    scheduled_at: int
    reservation_id: int
    slot_index: int
    slot_label: str
    status: str
    reserved_by: str
    reserved_at: int | None
    flight_letter: str | None
    flight_name: str | None
    aircraft: str | None
    aircraft_count: int | None
    slot_count: int | None
    user_display_name: str | None
    discord_username: str | None
    timezone: str | None
    notify_start: str | None
    notify_end: str | None
    notify_flightlead: bool


def due_flightlead_reminders(
    *,
    current_ts: int | None = None,
    minutes_before: int = 60,
    window_seconds: int = 60,
    limit: int = 200,
) -> list[FlightLeadReminderCandidate]:
    """
    Return flight lead reservations whose event start time is inside the current
    reminder window.

    This does not use a DB log. The cog keeps an in-memory set so each
    reservation is only tried once per bot runtime.
    """
    current = now_ts() if current_ts is None else int(current_ts)
    reminder_seconds = max(1, int(minutes_before)) * 60
    window_seconds = max(1, int(window_seconds))

    target_start = current + reminder_seconds
    target_end = target_start + window_seconds

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                oe.event_id,
                oe.op_template_id,
                ot.name AS op_name,
                ot.type AS op_type,
                oe.scheduled_at,

                r.id AS reservation_id,
                r.slot_index,
                r.slot_label,
                r.status,
                r.reserved_by,
                r.reserved_at,

                ft.flight_letter,
                ft.flight_name,
                ft.aircraft,
                ft.aircraft_count,
                ft.slot_count,

                u.display_name AS user_display_name,
                u.discord_username,

                us.timezone,
                us.notify_start,
                us.notify_end,
                us.notify_flightlead
            FROM op_reservations r
            JOIN op_events oe
                ON oe.event_id = r.op_event_id
            JOIN op_templates ot
                ON ot.id = oe.op_template_id
            LEFT JOIN flight_templates ft
                ON ft.op_template_id = oe.op_template_id
               AND ft.flight_index = r.slot_index
            LEFT JOIN users u
                ON u.discord_id = r.reserved_by
            LEFT JOIN user_settings us
                ON us.discord_id = r.reserved_by
            WHERE oe.status IN ('Scheduled', 'Briefing')
              AND oe.scheduled_at >= ?
              AND oe.scheduled_at < ?
              AND r.reserved_by IS NOT NULL
              AND r.status IN ('reserved', 'locked')
            ORDER BY oe.scheduled_at ASC, oe.event_id ASC, r.slot_index ASC
            LIMIT ?
            """,
            (
                int(target_start),
                int(target_end),
                int(limit),
            ),
        ).fetchall()

    candidates: list[FlightLeadReminderCandidate] = []

    for row in rows:
        candidates.append(
            FlightLeadReminderCandidate(
                event_id=int(row["event_id"]),
                op_template_id=int(row["op_template_id"]),
                op_name=str(row["op_name"]),
                op_type=str(row["op_type"]),
                scheduled_at=int(row["scheduled_at"]),
                reservation_id=int(row["reservation_id"]),
                slot_index=int(row["slot_index"]),
                slot_label=str(row["slot_label"]),
                status=str(row["status"]),
                reserved_by=str(row["reserved_by"]),
                reserved_at=int(row["reserved_at"]) if row["reserved_at"] is not None else None,
                flight_letter=str(row["flight_letter"]) if row["flight_letter"] is not None else None,
                flight_name=str(row["flight_name"]) if row["flight_name"] is not None else None,
                aircraft=str(row["aircraft"]) if row["aircraft"] is not None else None,
                aircraft_count=int(row["aircraft_count"]) if row["aircraft_count"] is not None else None,
                slot_count=int(row["slot_count"]) if row["slot_count"] is not None else None,
                user_display_name=str(row["user_display_name"]) if row["user_display_name"] is not None else None,
                discord_username=str(row["discord_username"]) if row["discord_username"] is not None else None,
                timezone=str(row["timezone"]) if row["timezone"] is not None else None,
                notify_start=str(row["notify_start"]) if row["notify_start"] is not None else None,
                notify_end=str(row["notify_end"]) if row["notify_end"] is not None else None,
                notify_flightlead=bool(row["notify_flightlead"]),
            )
        )

    return candidates
