from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from database import get_connection

from services.schedule_service import (
    FlightTemplateSummary,
    OpEventRecord,
    clean_text,
    format_timestamp_short,
    get_flight_templates,
    get_op_event,
    get_user_timezone,
    now_ts,
)


ACTIVE_EVENT_STATUSES = ("Scheduled",)


@dataclass
class FlightLeadEventSummary:
    event_id: int
    op_template_id: int
    op_name: str
    op_type: str
    scheduled_at: int
    status: str
    total_slots: int
    taken_slots: int
    user_has_slot: bool


@dataclass
class FlightLeadSlot:
    reservation_id: int
    op_event_id: int
    slot_index: int
    slot_label: str
    reserved_by: str | None
    reserved_at: int | None
    status: str

    flight_id: int | None
    flight_letter: str
    flight_name: str
    aircraft: str | None
    aircraft_count: int | None
    slot_count: int
    description: str | None

    @property
    def is_taken(self) -> bool:
        return bool(self.reserved_by) or self.status in {"reserved", "locked"}

    @property
    def is_open(self) -> bool:
        return self.status == "open" and not self.reserved_by

    def is_reserved_by(self, discord_id: str) -> bool:
        return str(self.reserved_by or "") == str(discord_id)


def ensure_user_record(
    *,
    discord_id: str,
    discord_username: str | None,
    display_name: str | None,
) -> None:
    ts = now_ts()

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO users (
                discord_id,
                discord_username,
                display_name,
                rank,
                status,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, 'Recruit', 'Active', ?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET
                discord_username = excluded.discord_username,
                display_name = excluded.display_name,
                updated_at = excluded.updated_at
            """,
            (
                str(discord_id),
                clean_text(discord_username),
                clean_text(display_name),
                ts,
                ts,
            ),
        )


def get_upcoming_flightlead_events(
    *,
    discord_id: str,
    days: int = 7,
    limit: int = 25,
) -> list[FlightLeadEventSummary]:
    start_ts = now_ts()
    end_ts = start_ts + (int(days) * 86400)

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

                COUNT(op_reservations.id) AS total_slots,

                COALESCE(
                    SUM(
                        CASE
                            WHEN op_reservations.reserved_by IS NOT NULL
                              OR op_reservations.status IN ('reserved', 'locked')
                            THEN 1
                            ELSE 0
                        END
                    ),
                    0
                ) AS taken_slots,

                COALESCE(
                    SUM(
                        CASE
                            WHEN op_reservations.reserved_by = ?
                            THEN 1
                            ELSE 0
                        END
                    ),
                    0
                ) AS user_slots
            FROM op_events
            JOIN op_templates
                ON op_templates.id = op_events.op_template_id
            LEFT JOIN op_reservations
                ON op_reservations.op_event_id = op_events.event_id
            WHERE op_events.status = 'Scheduled'
              AND op_events.scheduled_at >= ?
              AND op_events.scheduled_at <= ?
            GROUP BY
                op_events.event_id,
                op_events.op_template_id,
                op_templates.name,
                op_templates.type,
                op_events.scheduled_at,
                op_events.status
            ORDER BY op_events.scheduled_at ASC, op_events.event_id ASC
            LIMIT ?
            """,
            (
                str(discord_id),
                int(start_ts),
                int(end_ts),
                int(limit),
            ),
        ).fetchall()

    return [
        FlightLeadEventSummary(
            event_id=int(row["event_id"]),
            op_template_id=int(row["op_template_id"]),
            op_name=str(row["op_name"]),
            op_type=str(row["op_type"]),
            scheduled_at=int(row["scheduled_at"]),
            status=str(row["status"]),
            total_slots=int(row["total_slots"] or 0),
            taken_slots=int(row["taken_slots"] or 0),
            user_has_slot=int(row["user_slots"] or 0) > 0,
        )
        for row in rows
    ]


def get_flightlead_event(event_id: int, discord_id: str) -> FlightLeadEventSummary | None:
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

                COUNT(op_reservations.id) AS total_slots,

                COALESCE(
                    SUM(
                        CASE
                            WHEN op_reservations.reserved_by IS NOT NULL
                              OR op_reservations.status IN ('reserved', 'locked')
                            THEN 1
                            ELSE 0
                        END
                    ),
                    0
                ) AS taken_slots,

                COALESCE(
                    SUM(
                        CASE
                            WHEN op_reservations.reserved_by = ?
                            THEN 1
                            ELSE 0
                        END
                    ),
                    0
                ) AS user_slots
            FROM op_events
            JOIN op_templates
                ON op_templates.id = op_events.op_template_id
            LEFT JOIN op_reservations
                ON op_reservations.op_event_id = op_events.event_id
            WHERE op_events.event_id = ?
              AND op_events.status = 'Scheduled'
            GROUP BY
                op_events.event_id,
                op_events.op_template_id,
                op_templates.name,
                op_templates.type,
                op_events.scheduled_at,
                op_events.status
            LIMIT 1
            """,
            (
                str(discord_id),
                int(event_id),
            ),
        ).fetchone()

    if row is None:
        return None

    return FlightLeadEventSummary(
        event_id=int(row["event_id"]),
        op_template_id=int(row["op_template_id"]),
        op_name=str(row["op_name"]),
        op_type=str(row["op_type"]),
        scheduled_at=int(row["scheduled_at"]),
        status=str(row["status"]),
        total_slots=int(row["total_slots"] or 0),
        taken_slots=int(row["taken_slots"] or 0),
        user_has_slot=int(row["user_slots"] or 0) > 0,
    )


def flight_template_details_by_event(event_id: int) -> list[dict[str, Any]]:
    event = get_op_event(event_id)

    if event is None:
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
            (int(event.op_template_id),),
        ).fetchall()

    return [dict(row) for row in rows]



def reservation_rows_for_event(event_id: int) -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                id,
                op_event_id,
                slot_index,
                slot_label,
                reserved_by,
                reserved_at,
                status
            FROM op_reservations
            WHERE op_event_id = ?
            ORDER BY slot_index ASC, id ASC
            """,
            (int(event_id),),
        ).fetchall()

    return [dict(row) for row in rows]


def flightlead_slots_for_event(event_id: int) -> list[FlightLeadSlot]:
    event = get_op_event(event_id)

    if event is None:
        return []

    flights = flight_template_details_by_event(event_id)
    reservations = reservation_rows_for_event(event_id)

    slots: list[FlightLeadSlot] = []

    for index, row in enumerate(reservations):
        flight: dict[str, Any] | None = None

        if 0 <= index < len(flights):
            flight = flights[index]

        fallback_label = str(row["slot_label"])
        fallback_letter = fallback_label.split("|", 1)[0].strip() if "|" in fallback_label else str(row["slot_index"])
        fallback_name = fallback_label.split("|", 1)[1].strip() if "|" in fallback_label else fallback_label

        slots.append(
            FlightLeadSlot(
                reservation_id=int(row["id"]),
                op_event_id=int(row["op_event_id"]),
                slot_index=int(row["slot_index"]),
                slot_label=fallback_label,
                reserved_by=clean_text(row["reserved_by"]),
                reserved_at=int(row["reserved_at"]) if row["reserved_at"] is not None else None,
                status=str(row["status"]),

                flight_id=int(flight["id"]) if flight else None,
                flight_letter=str(flight["flight_letter"]) if flight else fallback_letter,
                flight_name=str(flight["flight_name"]) if flight else fallback_name,
                aircraft=clean_text(flight["aircraft"]) if flight else None,
                aircraft_count=int(flight["aircraft_count"]) if flight and flight["aircraft_count"] is not None else None,
                slot_count=int(flight["slot_count"]) if flight and flight["slot_count"] is not None else 1,
                description=clean_text(flight["description"]) if flight else None,
            )
        )

    return slots


def count_user_reserved_slots_in_next_days(
    *,
    discord_id: str,
    days: int = 7,
) -> int:
    start_ts = now_ts()
    end_ts = start_ts + (int(days) * 86400)

    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM op_reservations
            JOIN op_events
                ON op_events.event_id = op_reservations.op_event_id
            WHERE op_reservations.reserved_by = ?
              AND op_reservations.status IN ('reserved', 'locked')
              AND op_events.status = 'Scheduled'
              AND op_events.scheduled_at >= ?
              AND op_events.scheduled_at <= ?
            """,
            (
                str(discord_id),
                int(start_ts),
                int(end_ts),
            ),
        ).fetchone()

    if row is None:
        return 0

    return int(row["count"] or 0)


def unreserve_all_user_slots_in_next_days(
    *,
    discord_id: str,
    days: int = 7,
) -> int:
    start_ts = now_ts()
    end_ts = start_ts + (int(days) * 86400)

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT op_reservations.id
            FROM op_reservations
            JOIN op_events
                ON op_events.event_id = op_reservations.op_event_id
            WHERE op_reservations.reserved_by = ?
              AND op_reservations.status = 'reserved'
              AND op_events.status = 'Scheduled'
              AND op_events.scheduled_at >= ?
              AND op_events.scheduled_at <= ?
            """,
            (
                str(discord_id),
                int(start_ts),
                int(end_ts),
            ),
        ).fetchall()

        reservation_ids = [int(row["id"]) for row in rows]

        if not reservation_ids:
            return 0

        placeholders = ",".join("?" for _ in reservation_ids)

        conn.execute(
            f"""
            UPDATE op_reservations
            SET reserved_by = NULL,
                reserved_at = NULL,
                status = 'open'
            WHERE id IN ({placeholders})
            """,
            reservation_ids,
        )

    return len(reservation_ids)



def get_user_reserved_slot(
    *,
    event_id: int,
    discord_id: str,
) -> FlightLeadSlot | None:
    for slot in flightlead_slots_for_event(event_id):
        if slot.is_reserved_by(discord_id):
            return slot

    return None


def get_slot(
    *,
    event_id: int,
    reservation_id: int,
) -> FlightLeadSlot | None:
    for slot in flightlead_slots_for_event(event_id):
        if slot.reservation_id == int(reservation_id):
            return slot

    return None


def reserve_flightlead_slot(
    *,
    event_id: int,
    reservation_id: int,
    discord_id: str,
) -> None:
    ts = now_ts()

    with get_connection() as conn:
        event_row = conn.execute(
            """
            SELECT status
            FROM op_events
            WHERE event_id = ?
            LIMIT 1
            """,
            (int(event_id),),
        ).fetchone()

        if event_row is None:
            raise ValueError("That scheduled op no longer exists.")

        if str(event_row["status"]) not in ACTIVE_EVENT_STATUSES:
            raise ValueError("That op has already started or is no longer open for flight lead reservations.")

        target = conn.execute(
            """
            SELECT
                id,
                op_event_id,
                reserved_by,
                status
            FROM op_reservations
            WHERE id = ?
              AND op_event_id = ?
            LIMIT 1
            """,
            (
                int(reservation_id),
                int(event_id),
            ),
        ).fetchone()

        if target is None:
            raise ValueError("That flight lead slot no longer exists.")

        target_status = str(target["status"])
        target_reserved_by = clean_text(target["reserved_by"])

        if target_reserved_by == str(discord_id):
            return

        if target_reserved_by or target_status != "open":
            raise ValueError("That flight lead slot is already taken.")

        locked_existing = conn.execute(
            """
            SELECT id
            FROM op_reservations
            WHERE op_event_id = ?
              AND reserved_by = ?
              AND status = 'locked'
            LIMIT 1
            """,
            (
                int(event_id),
                str(discord_id),
            ),
        ).fetchone()

        if locked_existing is not None:
            raise ValueError("Your existing flight lead slot is locked and cannot be changed.")

        # A user can only hold one flight lead slot per op.
        conn.execute(
            """
            UPDATE op_reservations
            SET reserved_by = NULL,
                reserved_at = NULL,
                status = 'open'
            WHERE op_event_id = ?
              AND reserved_by = ?
              AND status = 'reserved'
            """,
            (
                int(event_id),
                str(discord_id),
            ),
        )

        conn.execute(
            """
            UPDATE op_reservations
            SET reserved_by = ?,
                reserved_at = ?,
                status = 'reserved'
            WHERE id = ?
              AND op_event_id = ?
              AND reserved_by IS NULL
              AND status = 'open'
            """,
            (
                str(discord_id),
                ts,
                int(reservation_id),
                int(event_id),
            ),
        )


def unreserve_flightlead_slot(
    *,
    event_id: int,
    reservation_id: int,
    discord_id: str,
) -> None:
    with get_connection() as conn:
        target = conn.execute(
            """
            SELECT
                id,
                op_event_id,
                reserved_by,
                status
            FROM op_reservations
            WHERE id = ?
              AND op_event_id = ?
            LIMIT 1
            """,
            (
                int(reservation_id),
                int(event_id),
            ),
        ).fetchone()

        if target is None:
            raise ValueError("That flight lead slot no longer exists.")

        if clean_text(target["reserved_by"]) != str(discord_id):
            raise ValueError("You do not have that flight lead slot reserved.")

        if str(target["status"]) == "locked":
            raise ValueError("That flight lead slot is locked and cannot be unreserved.")

        conn.execute(
            """
            UPDATE op_reservations
            SET reserved_by = NULL,
                reserved_at = NULL,
                status = 'open'
            WHERE id = ?
              AND op_event_id = ?
              AND reserved_by = ?
            """,
            (
                int(reservation_id),
                int(event_id),
                str(discord_id),
            ),
        )
