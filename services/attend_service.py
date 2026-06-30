from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from database import get_connection

from services.schedule_service import (
    clean_text,
    format_timestamp_short,
    get_user_timezone,
    now_ts,
)

try:
    from config import AIRCRAFT_OPTIONS
except ImportError:
    AIRCRAFT_OPTIONS = [
        {"name": "AV-42C", "max_seats": 1},
        {"name": "F/A-26", "max_seats": 1},
        {"name": "F-45A", "max_seats": 1},
        {"name": "EF-24", "max_seats": 2},
        {"name": "T-55", "max_seats": 2},
        {"name": "AH-94", "max_seats": 2},
    ]


# Used as a safety fallback when config.py still has old string-style AIRCRAFT_OPTIONS.
# Matching is intentionally forgiving, so EF-24, EF-24G, and "EF-24 Growler" can resolve.
DEFAULT_AIRCRAFT_MAX_SEATS = {
    "av-42c": 1,
    "f/a-26": 1,
    "fa-26": 1,
    "f-45a": 1,
    "ef-24": 2,
    "ef-24g": 2,
    "t-55": 2,
    "ah-94": 2,
}


@dataclass
class OpenOp:
    event_id: int
    op_template_id: int
    op_name: str
    op_type: str
    scheduled_at: int
    status: str


@dataclass
class AttendFlight:
    flight_index: int
    flight_letter: str
    flight_name: str
    aircraft: str | None
    aircraft_count: int
    slot_count: int
    description: str | None


@dataclass
class FlightAvailability:
    flight: AttendFlight
    submitted_count: int
    max_count: int
    user_in_flight: bool

    @property
    def is_available(self) -> bool:
        return self.submitted_count < self.max_count or self.user_in_flight

    @property
    def label(self) -> str:
        return f"{self.flight.flight_name} ({self.submitted_count}/{self.max_count})"


@dataclass
class SlotAvailability:
    slot: str
    aircraft: str | None
    submitted_count: int
    max_count: int
    user_in_slot: bool

    @property
    def is_available(self) -> bool:
        return self.submitted_count < self.max_count or self.user_in_slot

    @property
    def label(self) -> str:
        return f"{self.slot} ({self.submitted_count}/{self.max_count})"


@dataclass
class AttendSubmission:
    scheduled_op_id: int
    op_template_name: str
    discord_id: str
    user_name: str
    slot: str
    aircraft: str | None
    combat_deaths: int
    landing_type: str
    wires: int | None
    bolters: int | None
    flight_lead_rating: int | None
    op_remarks: str | None
    fl_remarks: str | None
    note_remarks: str | None


def normalize_aircraft_name(value: str | None) -> str:
    return (clean_text(value) or "").strip().lower()


def aircraft_option_name(option: Any) -> str:
    if isinstance(option, dict):
        return str(option.get("name") or option.get("label") or option.get("value") or "")

    return str(option or "")


def aircraft_option_max_seats(option: Any) -> int | None:
    if isinstance(option, dict):
        try:
            seats = int(option.get("max_seats"))
        except Exception:
            return None

        return max(1, seats)

    # Supports optional tuple/list format:
    # ("EF-24", 2)
    if isinstance(option, (tuple, list)) and len(option) >= 2:
        try:
            seats = int(option[1])
        except Exception:
            return None

        return max(1, seats)

    # Old string-style config has no seat metadata.
    return None


def aircraft_names_match(stored_aircraft: str, option_aircraft: str) -> bool:
    stored = normalize_aircraft_name(stored_aircraft)
    option = normalize_aircraft_name(option_aircraft)

    if not stored or not option:
        return False

    if stored == option:
        return True

    # Handles names like EF-24G, EF-24 Growler, T-55 Trainer, etc.
    return stored.startswith(option) or option.startswith(stored)


def fallback_max_seats_for_aircraft(aircraft: str | None) -> int:
    wanted = normalize_aircraft_name(aircraft)

    if not wanted:
        return 1

    for key, seats in DEFAULT_AIRCRAFT_MAX_SEATS.items():
        if wanted == key or wanted.startswith(key) or key.startswith(wanted):
            return int(seats)

    return 1


def max_seats_for_aircraft(aircraft: str | None) -> int:
    wanted = clean_text(aircraft)

    if not wanted:
        return 1

    for option in AIRCRAFT_OPTIONS:
        option_name = aircraft_option_name(option)

        if aircraft_names_match(wanted, option_name):
            configured_seats = aircraft_option_max_seats(option)

            if configured_seats is not None:
                return configured_seats

            # Old string-style config entry matched. Use the built-in fallback map.
            return fallback_max_seats_for_aircraft(wanted)

    return fallback_max_seats_for_aircraft(wanted)


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


def get_current_open_op() -> OpenOp | None:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                op_events.event_id,
                op_events.op_template_id,
                op_templates.name AS op_name,
                op_templates.type AS op_type,
                op_events.scheduled_at,
                op_events.status
            FROM op_events
            JOIN op_templates
                ON op_templates.id = op_events.op_template_id
            WHERE op_events.status = 'Open'
            ORDER BY op_events.updated_at DESC, op_events.event_id DESC
            LIMIT 1
            """
        ).fetchone()

    if row is None:
        return None

    return OpenOp(
        event_id=int(row["event_id"]),
        op_template_id=int(row["op_template_id"]),
        op_name=str(row["op_name"]),
        op_type=str(row["op_type"]),
        scheduled_at=int(row["scheduled_at"]),
        status=str(row["status"]),
    )


def get_open_op_by_id(event_id: int) -> OpenOp | None:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                op_events.event_id,
                op_events.op_template_id,
                op_templates.name AS op_name,
                op_templates.type AS op_type,
                op_events.scheduled_at,
                op_events.status
            FROM op_events
            JOIN op_templates
                ON op_templates.id = op_events.op_template_id
            WHERE op_events.event_id = ?
              AND op_events.status = 'Open'
            LIMIT 1
            """,
            (int(event_id),),
        ).fetchone()

    if row is None:
        return None

    return OpenOp(
        event_id=int(row["event_id"]),
        op_template_id=int(row["op_template_id"]),
        op_name=str(row["op_name"]),
        op_type=str(row["op_type"]),
        scheduled_at=int(row["scheduled_at"]),
        status=str(row["status"]),
    )


def get_flights_for_open_op(event_id: int) -> list[AttendFlight]:
    op = get_open_op_by_id(event_id)

    if op is None:
        return []

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
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

    flights: list[AttendFlight] = []

    for row in rows:
        flight_letter = clean_text(row["flight_letter"]) or "?"
        flight_name = clean_text(row["flight_name"]) or flight_letter
        aircraft_count = int(row["aircraft_count"] or 0)

        if aircraft_count <= 0:
            aircraft_count = 1

        flights.append(
            AttendFlight(
                flight_index=int(row["flight_index"] or 0),
                flight_letter=flight_letter,
                flight_name=flight_name,
                aircraft=clean_text(row["aircraft"]),
                aircraft_count=aircraft_count,
                slot_count=int(row["slot_count"] or 0),
                description=clean_text(row["description"]),
            )
        )

    return flights


def get_flight_by_index(
    *,
    event_id: int,
    flight_index: int,
) -> AttendFlight | None:
    for flight in get_flights_for_open_op(event_id):
        if flight.flight_index == int(flight_index):
            return flight

    return None


def slot_options_for_flight(flight: AttendFlight) -> list[str]:
    base = clean_text(flight.flight_name) or clean_text(flight.flight_letter) or "Flight"
    count = max(1, int(flight.aircraft_count or 1))

    return [
        f"{base} 1-{number}"
        for number in range(1, count + 1)
    ]


def submitted_attendance_rows_for_event(event_id: int) -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM attendance
            WHERE scheduled_op_id = ?
              AND status = 'submitted'
            """,
            (int(event_id),),
        ).fetchall()

    return [dict(row) for row in rows]


def count_submitted_for_slot(
    *,
    event_id: int,
    slot: str,
) -> int:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM attendance
            WHERE scheduled_op_id = ?
              AND slot = ?
              AND status = 'submitted'
            """,
            (
                int(event_id),
                clean_text(slot),
            ),
        ).fetchone()

    if row is None:
        return 0

    return int(row["count"] or 0)


def user_submitted_for_slot(
    *,
    event_id: int,
    slot: str,
    discord_id: str,
) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM attendance
            WHERE scheduled_op_id = ?
              AND slot = ?
              AND discord_id = ?
              AND status = 'submitted'
            LIMIT 1
            """,
            (
                int(event_id),
                clean_text(slot),
                str(discord_id),
            ),
        ).fetchone()

    return row is not None


def slot_availabilities_for_flight(
    *,
    event_id: int,
    flight: AttendFlight,
    discord_id: str,
) -> list[SlotAvailability]:
    max_count = max_seats_for_aircraft(flight.aircraft)
    slots: list[SlotAvailability] = []

    for slot in slot_options_for_flight(flight):
        submitted_count = count_submitted_for_slot(
            event_id=event_id,
            slot=slot,
        )

        user_in_slot = user_submitted_for_slot(
            event_id=event_id,
            slot=slot,
            discord_id=discord_id,
        )

        availability = SlotAvailability(
            slot=slot,
            aircraft=flight.aircraft,
            submitted_count=submitted_count,
            max_count=max_count,
            user_in_slot=user_in_slot,
        )

        if availability.is_available:
            slots.append(availability)

    return slots


def count_submitted_for_flight(
    *,
    event_id: int,
    flight: AttendFlight,
) -> int:
    slots = slot_options_for_flight(flight)

    if not slots:
        return 0

    placeholders = ",".join("?" for _ in slots)

    with get_connection() as conn:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM attendance
            WHERE scheduled_op_id = ?
              AND slot IN ({placeholders})
              AND status = 'submitted'
            """,
            [
                int(event_id),
                *slots,
            ],
        ).fetchone()

    if row is None:
        return 0

    return int(row["count"] or 0)


def user_submitted_for_flight(
    *,
    event_id: int,
    flight: AttendFlight,
    discord_id: str,
) -> bool:
    slots = slot_options_for_flight(flight)

    if not slots:
        return False

    placeholders = ",".join("?" for _ in slots)

    with get_connection() as conn:
        row = conn.execute(
            f"""
            SELECT 1
            FROM attendance
            WHERE scheduled_op_id = ?
              AND discord_id = ?
              AND slot IN ({placeholders})
              AND status = 'submitted'
            LIMIT 1
            """,
            [
                int(event_id),
                str(discord_id),
                *slots,
            ],
        ).fetchone()

    return row is not None


def flight_availabilities_for_open_op(
    *,
    event_id: int,
    discord_id: str,
) -> list[FlightAvailability]:
    flights = get_flights_for_open_op(event_id)
    availability_rows: list[FlightAvailability] = []

    for flight in flights:
        submitted_count = count_submitted_for_flight(
            event_id=event_id,
            flight=flight,
        )

        max_count = max(0, int(flight.slot_count or 0))

        user_in_flight = user_submitted_for_flight(
            event_id=event_id,
            flight=flight,
            discord_id=discord_id,
        )

        availability = FlightAvailability(
            flight=flight,
            submitted_count=submitted_count,
            max_count=max_count,
            user_in_flight=user_in_flight,
        )

        if availability.is_available:
            availability_rows.append(availability)

    return availability_rows


def get_existing_user_attendance(
    *,
    event_id: int,
    discord_id: str,
) -> dict[str, Any] | None:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM attendance
            WHERE scheduled_op_id = ?
              AND discord_id = ?
              AND status = 'submitted'
            ORDER BY updated_at DESC, entry_id DESC
            LIMIT 1
            """,
            (
                int(event_id),
                str(discord_id),
            ),
        ).fetchone()

    if row is None:
        return None

    return dict(row)


def find_flight_index_for_slot(
    *,
    event_id: int,
    slot: str | None,
) -> int | None:
    if not slot:
        return None

    for flight in get_flights_for_open_op(event_id):
        if slot in slot_options_for_flight(flight):
            return flight.flight_index

    return None


def validate_attendance_capacity(
    *,
    event_id: int,
    flight_index: int | None,
    slot: str,
    aircraft: str | None,
    discord_id: str,
) -> None:
    if flight_index is None:
        raise ValueError("Select a flight first.")

    flight = get_flight_by_index(
        event_id=event_id,
        flight_index=flight_index,
    )

    if flight is None:
        raise ValueError("That flight is no longer available.")

    flight_count = count_submitted_for_flight(
        event_id=event_id,
        flight=flight,
    )

    user_in_flight = user_submitted_for_flight(
        event_id=event_id,
        flight=flight,
        discord_id=discord_id,
    )

    if flight_count >= int(flight.slot_count or 0) and not user_in_flight:
        raise ValueError("That flight is already full. Go back and pick another flight.")

    slot_count = count_submitted_for_slot(
        event_id=event_id,
        slot=slot,
    )

    user_in_slot = user_submitted_for_slot(
        event_id=event_id,
        slot=slot,
        discord_id=discord_id,
    )

    max_count = max_seats_for_aircraft(aircraft)

    if slot_count >= max_count and not user_in_slot:
        raise ValueError("That slot is already full. Go back and pick another slot.")


def next_entry_slot_index(event_id: int) -> int:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(MAX(entry_slot_index), -1) + 1 AS next_index
            FROM attendance
            WHERE scheduled_op_id = ?
            """,
            (int(event_id),),
        ).fetchone()

    if row is None:
        return 0

    return int(row["next_index"] or 0)


def username_for_discord_id(discord_id: str | None) -> str | None:
    did = clean_text(discord_id)

    if not did:
        return None

    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                discord_username,
                display_name
            FROM users
            WHERE discord_id = ?
            LIMIT 1
            """,
            (did,),
        ).fetchone()

    if row is None:
        return did

    return (
        clean_text(row["discord_username"])
        or clean_text(row["display_name"])
        or did
    )


def submit_attendance(
    *,
    submission: AttendSubmission,
    flight_index: int | None,
) -> int:
    ts = now_ts()
    resolved_user_name = username_for_discord_id(submission.discord_id)

    validate_attendance_capacity(
        event_id=submission.scheduled_op_id,
        flight_index=flight_index,
        slot=submission.slot,
        aircraft=submission.aircraft,
        discord_id=submission.discord_id,
    )

    with get_connection() as conn:
        existing_user_row = conn.execute(
            """
            SELECT entry_id
            FROM attendance
            WHERE scheduled_op_id = ?
              AND discord_id = ?
              AND status = 'submitted'
            ORDER BY updated_at DESC, entry_id DESC
            LIMIT 1
            """,
            (
                int(submission.scheduled_op_id),
                str(submission.discord_id),
            ),
        ).fetchone()

        if existing_user_row is not None:
            entry_id = int(existing_user_row["entry_id"])

            conn.execute(
                """
                UPDATE attendance
                SET user_name = ?,
                    slot = ?,
                    aircraft = ?,
                    combat_deaths = ?,
                    landing_type = ?,
                    wires = ?,
                    bolters = ?,
                    attend_type = 'normal',
                    op_remarks = ?,
                    fl_remarks = ?,
                    note_remarks = ?,
                    flight_lead_rating = ?,
                    status = 'submitted',
                    type = 'normal',
                    logged_at = COALESCE(logged_at, ?),
                    updated_at = ?
                WHERE entry_id = ?
                """,
                (
                    resolved_user_name,
                    clean_text(submission.slot),
                    clean_text(submission.aircraft),
                    int(submission.combat_deaths),
                    clean_text(submission.landing_type),
                    int(submission.wires) if submission.wires is not None else None,
                    int(submission.bolters) if submission.bolters is not None else None,
                    clean_text(submission.op_remarks),
                    clean_text(submission.fl_remarks),
                    clean_text(submission.note_remarks),
                    int(submission.flight_lead_rating) if submission.flight_lead_rating is not None else None,
                    ts,
                    ts,
                    entry_id,
                ),
            )

            return entry_id

        open_row = conn.execute(
            """
            SELECT entry_id
            FROM attendance
            WHERE scheduled_op_id = ?
              AND status = 'open'
              AND discord_id IS NULL
            ORDER BY entry_slot_index ASC, entry_id ASC
            LIMIT 1
            """,
            (int(submission.scheduled_op_id),),
        ).fetchone()

        if open_row is not None:
            entry_id = int(open_row["entry_id"])

            conn.execute(
                """
                UPDATE attendance
                SET discord_id = ?,
                    user_name = ?,
                    slot = ?,
                    aircraft = ?,
                    combat_deaths = ?,
                    landing_type = ?,
                    wires = ?,
                    bolters = ?,
                    attend_type = 'normal',
                    op_remarks = ?,
                    fl_remarks = ?,
                    note_remarks = ?,
                    flight_lead_rating = ?,
                    status = 'submitted',
                    type = 'normal',
                    logged_at = ?,
                    updated_at = ?
                WHERE entry_id = ?
                """,
                (
                    str(submission.discord_id),
                    resolved_user_name,
                    clean_text(submission.slot),
                    clean_text(submission.aircraft),
                    int(submission.combat_deaths),
                    clean_text(submission.landing_type),
                    int(submission.wires) if submission.wires is not None else None,
                    int(submission.bolters) if submission.bolters is not None else None,
                    clean_text(submission.op_remarks),
                    clean_text(submission.fl_remarks),
                    clean_text(submission.note_remarks),
                    int(submission.flight_lead_rating) if submission.flight_lead_rating is not None else None,
                    ts,
                    ts,
                    entry_id,
                ),
            )

            return entry_id

        entry_slot_index = next_entry_slot_index(submission.scheduled_op_id)

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
                ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                'normal',
                ?, ?, ?,
                ?,
                'submitted',
                'normal',
                ?, ?, ?
            )
            """,
            (
                int(submission.scheduled_op_id),
                clean_text(submission.op_template_name),
                int(entry_slot_index),
                str(submission.discord_id),
                resolved_user_name,
                clean_text(submission.slot),
                clean_text(submission.aircraft),
                int(submission.combat_deaths),
                clean_text(submission.landing_type),
                int(submission.wires) if submission.wires is not None else None,
                int(submission.bolters) if submission.bolters is not None else None,
                clean_text(submission.op_remarks),
                clean_text(submission.fl_remarks),
                clean_text(submission.note_remarks),
                int(submission.flight_lead_rating) if submission.flight_lead_rating is not None else None,
                ts,
                ts,
                ts,
            ),
        )

        return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
