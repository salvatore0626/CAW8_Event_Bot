from __future__ import annotations

import json
from dataclasses import asdict, dataclass
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
class RecordEditOp:
    event_id: int
    op_template_id: int
    op_name: str
    op_type: str
    scheduled_at: int
    status: str


@dataclass
class AttendanceRecord:
    entry_id: int
    scheduled_op_id: int | None
    op_template_name: str | None
    entry_slot_index: int | None
    discord_id: str | None
    user_name: str | None
    slot: str | None
    aircraft: str | None
    combat_deaths: int | None
    landing_type: str | None
    wires: int | None
    bolters: int | None
    attend_type: str | None
    op_remarks: str | None
    fl_remarks: str | None
    note_remarks: str | None
    flight_lead_rating: int | None
    status: str | None
    type: str | None
    created_at: int | None
    logged_at: int | None
    updated_at: int | None


@dataclass
class FlightTemplate:
    flight_index: int
    flight_letter: str
    flight_name: str
    aircraft: str | None
    aircraft_count: int
    slot_count: int
    description: str | None


@dataclass
class RecordValidation:
    overfull_flights: set[str]
    overfull_slots: set[str]
    duplicate_discord_ids: set[str]
    record_warnings: dict[int, list[str]]


def normalize_aircraft_name(value: str | None) -> str:
    return (clean_text(value) or "").strip().lower()


def aircraft_option_name(option: Any) -> str:
    if isinstance(option, dict):
        return str(option.get("name") or option.get("label") or option.get("value") or "")

    if isinstance(option, (tuple, list)) and option:
        return str(option[0])

    return str(option or "")


def aircraft_option_max_seats(option: Any) -> int | None:
    if isinstance(option, dict):
        try:
            seats = int(option.get("max_seats"))
        except Exception:
            return None
        return max(1, seats)

    if isinstance(option, (tuple, list)) and len(option) >= 2:
        try:
            seats = int(option[1])
        except Exception:
            return None
        return max(1, seats)

    return None


def aircraft_names_match(stored_aircraft: str, option_aircraft: str) -> bool:
    stored = normalize_aircraft_name(stored_aircraft)
    option = normalize_aircraft_name(option_aircraft)

    if not stored or not option:
        return False

    return stored == option or stored.startswith(option) or option.startswith(stored)


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
            configured = aircraft_option_max_seats(option)
            if configured is not None:
                return configured
            return fallback_max_seats_for_aircraft(wanted)

    return fallback_max_seats_for_aircraft(wanted)


def row_to_op(row: Any) -> RecordEditOp:
    return RecordEditOp(
        event_id=int(row["event_id"]),
        op_template_id=int(row["op_template_id"]),
        op_name=str(row["op_name"]),
        op_type=str(row["op_type"]),
        scheduled_at=int(row["scheduled_at"]),
        status=str(row["status"]),
    )


def search_recordedit_ops(
    *,
    query: str | None = None,
    limit: int = 25,
) -> list[RecordEditOp]:
    q = clean_text(query)
    params: list[Any] = []

    q_sql = ""

    if q:
        q_sql = """
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
                op_events.status
            FROM op_events
            JOIN op_templates
                ON op_templates.id = op_events.op_template_id
            WHERE op_events.status IN ('Open', 'Complete')
            {q_sql}
            ORDER BY
                CASE op_events.status
                    WHEN 'Open' THEN 0
                    WHEN 'Complete' THEN 1
                    ELSE 2
                END,
                op_events.scheduled_at DESC,
                op_events.event_id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()

    return [row_to_op(row) for row in rows]


def get_recordedit_op(event_id: int) -> RecordEditOp | None:
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
              AND op_events.status IN ('Open', 'Complete')
            LIMIT 1
            """,
            (int(event_id),),
        ).fetchone()

    if row is None:
        return None

    return row_to_op(row)


def parse_event_id(value: str | int | None) -> int | None:
    if value is None:
        return None

    text = str(value).strip()

    if text.startswith("#"):
        text = text[1:].strip()

    first = text.split(" ", 1)[0].strip()

    if first.isdigit():
        return int(first)

    return None


def recordedit_op_option_label(op: RecordEditOp, timezone_name: str) -> str:
    return f"#{op.event_id} {format_timestamp_short(op.scheduled_at, timezone_name)} {op.op_name}"[:100]


def recordedit_op_option_description(op: RecordEditOp) -> str:
    return f"{op.status} | {op.op_type}"[:100]


def row_to_attendance(row: Any) -> AttendanceRecord:
    return AttendanceRecord(
        entry_id=int(row["entry_id"]),
        scheduled_op_id=int(row["scheduled_op_id"]) if row["scheduled_op_id"] is not None else None,
        op_template_name=clean_text(row["op_template_name"]),
        entry_slot_index=int(row["entry_slot_index"]) if row["entry_slot_index"] is not None else None,
        discord_id=clean_text(row["discord_id"]),
        user_name=clean_text(row["user_name"]),
        slot=clean_text(row["slot"]),
        aircraft=clean_text(row["aircraft"]),
        combat_deaths=int(row["combat_deaths"]) if row["combat_deaths"] is not None else None,
        landing_type=clean_text(row["landing_type"]),
        wires=int(row["wires"]) if row["wires"] is not None else None,
        bolters=int(row["bolters"]) if row["bolters"] is not None else None,
        attend_type=clean_text(row["attend_type"]),
        op_remarks=clean_text(row["op_remarks"]) if "op_remarks" in row.keys() else None,
        fl_remarks=clean_text(row["fl_remarks"]) if "fl_remarks" in row.keys() else None,
        note_remarks=clean_text(row["note_remarks"]) if "note_remarks" in row.keys() else None,
        flight_lead_rating=int(row["flight_lead_rating"]) if row["flight_lead_rating"] is not None else None,
        status=clean_text(row["status"]),
        type=clean_text(row["type"]),
        created_at=int(row["created_at"]) if row["created_at"] is not None else None,
        logged_at=int(row["logged_at"]) if row["logged_at"] is not None else None,
        updated_at=int(row["updated_at"]) if row["updated_at"] is not None else None,
    )




def attendance_to_dict(record: AttendanceRecord | None) -> dict[str, Any] | None:
    if record is None:
        return None

    return asdict(record)


def attendance_json(record: AttendanceRecord | None) -> str | None:
    data = attendance_to_dict(record)

    if data is None:
        return None

    return json.dumps(data, sort_keys=True)


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


def insert_admin_log(
    conn,
    *,
    action: str,
    user_discord_id: str | None,
    performed_by_id: str | None,
    before_record: AttendanceRecord | None,
    after_record: AttendanceRecord | None,
    created_at: int,
) -> None:
    ensure_admin_log_table(conn)

    conn.execute(
        """
        INSERT INTO admin_log (
            action,
            user_discord_id,
            performed_by_id,
            before_json,
            after_json,
            reason,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            clean_text(action),
            clean_text(user_discord_id),
            clean_text(performed_by_id),
            attendance_json(before_record),
            attendance_json(after_record),
            None,
            int(created_at),
        ),
    )


def fetch_attendance_record_in_conn(conn, entry_id: int) -> AttendanceRecord | None:
    row = conn.execute(
        """
        SELECT *
        FROM attendance
        WHERE entry_id = ?
        LIMIT 1
        """,
        (int(entry_id),),
    ).fetchone()

    if row is None:
        return None

    return row_to_attendance(row)


def get_attendance_records(event_id: int) -> list[AttendanceRecord]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM attendance
            WHERE scheduled_op_id = ?
            ORDER BY
                COALESCE(entry_slot_index, 999999) ASC,
                entry_id ASC
            """,
            (int(event_id),),
        ).fetchall()

    return [row_to_attendance(row) for row in rows]


def get_attendance_record(entry_id: int) -> AttendanceRecord | None:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM attendance
            WHERE entry_id = ?
            LIMIT 1
            """,
            (int(entry_id),),
        ).fetchone()

    if row is None:
        return None

    return row_to_attendance(row)


def get_flight_templates(event_id: int) -> list[FlightTemplate]:
    op = get_recordedit_op(event_id)

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

    templates: list[FlightTemplate] = []

    for row in rows:
        letter = clean_text(row["flight_letter"]) or "?"
        name = clean_text(row["flight_name"]) or letter
        aircraft_count = int(row["aircraft_count"] or 0)

        if aircraft_count <= 0:
            aircraft_count = 1

        templates.append(
            FlightTemplate(
                flight_index=int(row["flight_index"] or 0),
                flight_letter=letter,
                flight_name=name,
                aircraft=clean_text(row["aircraft"]),
                aircraft_count=aircraft_count,
                slot_count=int(row["slot_count"] or 0),
                description=clean_text(row["description"]),
            )
        )

    return templates


def slot_options_for_flight(flight: FlightTemplate) -> list[str]:
    base = clean_text(flight.flight_name) or clean_text(flight.flight_letter) or "Flight"
    count = max(1, int(flight.aircraft_count or 1))

    return [
        f"{base} 1-{number}"
        for number in range(1, count + 1)
    ]


def all_slot_options_for_event(event_id: int) -> list[tuple[str, str | None, str]]:
    options: list[tuple[str, str | None, str]] = []

    for flight in get_flight_templates(event_id):
        for slot in slot_options_for_flight(flight):
            options.append((slot, flight.aircraft, flight.flight_name))

    return options


def selected_slot_aircraft(event_id: int, slot: str | None) -> str | None:
    if not slot:
        return None

    for option_slot, aircraft, _flight_name in all_slot_options_for_event(event_id):
        if option_slot == slot:
            return aircraft

    return None


def flight_for_slot(event_id: int, slot: str | None) -> FlightTemplate | None:
    if not slot:
        return None

    for flight in get_flight_templates(event_id):
        if slot in slot_options_for_flight(flight):
            return flight

    return None


PILOT_LANDING_TYPES = {"Arrested", "Airfield", "Vertical"}
NON_PILOT_COMPATIBLE_LANDING_TYPES = {"Non-Pilot", "DNF"}


def add_record_warning(
    warnings: dict[int, list[str]],
    record: AttendanceRecord,
    message: str,
) -> None:
    warnings.setdefault(record.entry_id, [])

    if message not in warnings[record.entry_id]:
        warnings[record.entry_id].append(message)


def is_attendance_filled(record: AttendanceRecord) -> bool:
    return bool(
        record.discord_id
        or record.user_name
        or record.slot
        or record.landing_type
        or record.combat_deaths is not None
        or record.wires is not None
        or record.bolters is not None
    )


def validate_records(event_id: int) -> RecordValidation:
    """Warning-only validation for record editing.

    This function is intentionally single-pass/cached because it runs while
    building Discord views. It should never do nested DB calls.
    """
    all_records = get_attendance_records(event_id)
    records = [
        record
        for record in all_records
        if is_attendance_filled(record)
    ]
    flights = get_flight_templates(event_id)

    overfull_flights: set[str] = set()
    overfull_slots: set[str] = set()
    duplicate_discord_ids: set[str] = set()
    record_warnings: dict[int, list[str]] = {}

    slot_to_flight: dict[str, FlightTemplate] = {}

    for flight in flights:
        for slot in slot_options_for_flight(flight):
            slot_to_flight[slot] = flight

    # Rule: any entry with a discord ID must have basic required fields.
    for record in records:
        if record.discord_id:
            if not record.slot:
                add_record_warning(record_warnings, record, "Discord ID entry is missing a slot.")

            if not record.landing_type:
                add_record_warning(record_warnings, record, "Discord ID entry is missing a landing type.")

            if record.combat_deaths is None:
                add_record_warning(record_warnings, record, "Discord ID entry is missing combat deaths.")

        if record.landing_type == "Arrested":
            if record.wires is None:
                add_record_warning(record_warnings, record, "Arrested landing is missing wire.")

            if record.bolters is None:
                add_record_warning(record_warnings, record, "Arrested landing is missing bolters.")

        if record.slot and record.slot not in slot_to_flight:
            add_record_warning(record_warnings, record, f"Slot `{record.slot}` does not match this op template.")

    # Rule: same Discord ID should not appear multiple times in one op.
    by_discord_id: dict[str, list[AttendanceRecord]] = {}

    for record in records:
        if not record.discord_id:
            continue

        by_discord_id.setdefault(str(record.discord_id), []).append(record)

    for discord_id, matching_records in by_discord_id.items():
        if len(matching_records) > 1:
            user_label = username_for_discord_id(discord_id) or discord_id
            duplicate_discord_ids.add(user_label)

            for record in matching_records:
                add_record_warning(
                    record_warnings,
                    record,
                    f"Discord user `{user_label}` has multiple attendance entries in this op.",
                )

    # Build slot counts once.
    records_by_slot: dict[str, list[AttendanceRecord]] = {}

    for record in records:
        if not record.slot:
            continue

        records_by_slot.setdefault(record.slot, []).append(record)

    # Rule: flight max player slots and slot seat logic.
    for flight in flights:
        flight_slots = set(slot_options_for_flight(flight))
        flight_records: list[AttendanceRecord] = []

        for slot in flight_slots:
            flight_records.extend(records_by_slot.get(slot, []))

        flight_max = int(flight.slot_count or 0)
        if flight_max and len(flight_records) > flight_max:
            overfull_flights.add(flight.flight_name)

            for record in flight_records:
                add_record_warning(
                    record_warnings,
                    record,
                    (
                        f"Flight `{flight.flight_name}` has too many attendance entries "
                        f"({len(flight_records)}/{flight_max})."
                    ),
                )

        slot_max = max_seats_for_aircraft(flight.aircraft)

        for slot in flight_slots:
            slot_records = records_by_slot.get(slot, [])

            if len(slot_records) > slot_max:
                overfull_slots.add(slot)

                for record in slot_records:
                    add_record_warning(
                        record_warnings,
                        record,
                        f"Slot `{slot}` has too many entries ({len(slot_records)}/{slot_max}).",
                    )

            if len(slot_records) <= 1:
                continue

            if slot_max <= 1:
                for record in slot_records:
                    add_record_warning(
                        record_warnings,
                        record,
                        f"Slot `{slot}` is a one-seat aircraft slot but has multiple entries.",
                    )
                continue

            if slot_max == 2 and len(slot_records) == 2:
                non_pilot_or_dnf_count = sum(
                    1
                    for record in slot_records
                    if record.landing_type in NON_PILOT_COMPATIBLE_LANDING_TYPES
                )

                if non_pilot_or_dnf_count == 0:
                    for record in slot_records:
                        add_record_warning(
                            record_warnings,
                            record,
                            (
                                f"Two-seat slot `{slot}` has two pilot-style attendance entries. "
                                "One should usually be Non-Pilot or DNF."
                            ),
                        )

                elif non_pilot_or_dnf_count == 2:
                    for record in slot_records:
                        add_record_warning(
                            record_warnings,
                            record,
                            (
                                f"Two-seat slot `{slot}` has two non-pilot/DNF-style entries. "
                                "One entry should usually be a pilot landing type."
                            ),
                        )

            elif slot_max == 2 and len(slot_records) > 2:
                for record in slot_records:
                    add_record_warning(
                        record_warnings,
                        record,
                        f"Two-seat slot `{slot}` has more than two entries.",
                    )

    return RecordValidation(
        overfull_flights=overfull_flights,
        overfull_slots=overfull_slots,
        duplicate_discord_ids=duplicate_discord_ids,
        record_warnings=record_warnings,
    )


def record_validation_warnings(record: AttendanceRecord, validation: RecordValidation) -> list[str]:
    return validation.record_warnings.get(record.entry_id, [])


def record_has_validation_error(record: AttendanceRecord, validation: RecordValidation, event_id: int) -> bool:
    return bool(record_validation_warnings(record, validation))


def fmt_ts(ts: int | None, timezone_name: str) -> str:
    if ts is None:
        return "N/A"

    try:
        tz = ZoneInfo(timezone_name)
    except Exception:
        tz = ZoneInfo("UTC")

    dt = datetime.fromtimestamp(int(ts), tz)
    return dt.strftime("%a %b %d %I:%M%p").replace(" 0", " ")


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
        # Fallback keeps user_name tied to the ID instead of a random/freeform name.
        return did

    return (
        clean_text(row["discord_username"])
        or clean_text(row["display_name"])
        or did
    )


def update_attendance_record(
    *,
    entry_id: int,
    discord_id: str | None,
    slot: str | None,
    aircraft: str | None,
    combat_deaths: int | None,
    landing_type: str | None,
    wires: int | None,
    bolters: int | None,
    attend_type: str | None,
    performed_by_id: str | None = None,
) -> None:
    ts = now_ts()
    did = clean_text(discord_id)
    resolved_user_name = username_for_discord_id(did)

    with get_connection() as conn:
        before_record = fetch_attendance_record_in_conn(conn, int(entry_id))
        action = "record_edit" if before_record and before_record.discord_id else "record_add"

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
                attend_type = ?,
                type = 'manual',
                status = CASE
                    WHEN status IN ('open', 'complete') THEN 'submitted'
                    ELSE status
                END,
                logged_at = COALESCE(logged_at, ?),
                updated_at = ?
            WHERE entry_id = ?
            """,
            (
                did,
                resolved_user_name,
                clean_text(slot),
                clean_text(aircraft),
                int(combat_deaths) if combat_deaths is not None else None,
                clean_text(landing_type),
                int(wires) if wires is not None else None,
                int(bolters) if bolters is not None else None,
                clean_text(attend_type),
                ts,
                ts,
                int(entry_id),
            ),
        )

        after_record = fetch_attendance_record_in_conn(conn, int(entry_id))

        insert_admin_log(
            conn,
            action=action,
            user_discord_id=did,
            performed_by_id=clean_text(performed_by_id),
            before_record=before_record,
            after_record=after_record,
            created_at=ts,
        )


def delete_attendance_record(entry_id: int, performed_by_id: str | None = None) -> None:
    """Clear an attendance row back to an empty reusable placeholder.

    We intentionally do not delete the row because completed/open ops keep their
    attendance slot placeholders. Staff can Add a new person into this row later.
    """
    ts = now_ts()

    with get_connection() as conn:
        before_record = fetch_attendance_record_in_conn(conn, int(entry_id))
        user_discord_id = before_record.discord_id if before_record is not None else None

        conn.execute(
            """
            UPDATE attendance
            SET discord_id = NULL,
                user_name = NULL,
                slot = NULL,
                aircraft = NULL,
                combat_deaths = NULL,
                landing_type = NULL,
                wires = NULL,
                bolters = NULL,
                op_remarks = NULL,
                fl_remarks = NULL,
                note_remarks = NULL,
                flight_lead_rating = NULL,
                status = 'open',
                attend_type = 'normal',
                type = 'manual',
                logged_at = NULL,
                updated_at = ?
            WHERE entry_id = ?
            """,
            (ts, int(entry_id)),
        )

        after_record = fetch_attendance_record_in_conn(conn, int(entry_id))

        insert_admin_log(
            conn,
            action="record_delete",
            user_discord_id=user_discord_id,
            performed_by_id=clean_text(performed_by_id),
            before_record=before_record,
            after_record=after_record,
            created_at=ts,
        )
