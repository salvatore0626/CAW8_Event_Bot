from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from database import get_connection

try:
    from config import SCHEDULE_DEFAULT_TIMEZONE
except ImportError:
    SCHEDULE_DEFAULT_TIMEZONE = "America/New_York"

try:
    from config import SCHEDULE_DEFAULT_SLOTS
except ImportError:
    SCHEDULE_DEFAULT_SLOTS = [
        {"label": "Saturday 2 PM ET", "weekday": 5, "hour": 14, "minute": 0},
        {"label": "Saturday 4 PM ET", "weekday": 5, "hour": 16, "minute": 0},
        {"label": "Sunday 2 PM ET", "weekday": 6, "hour": 14, "minute": 0},
        {"label": "Sunday 4 PM ET", "weekday": 6, "hour": 16, "minute": 0},
        {"label": "Monday 2 PM ET", "weekday": 0, "hour": 14, "minute": 0},
        {"label": "Friday 8 PM ET", "weekday": 4, "hour": 20, "minute": 0},
    ]


OPEN_EVENT_STATUSES = ("Scheduled", "Briefing", "Open")


@dataclass
class DefaultScheduleSlot:
    label: str
    weekday: int
    hour: int
    minute: int


@dataclass
class OpTemplateSummary:
    id: int
    name: str
    op_type: str
    description: str | None
    total_players: int
    flight_count: int


@dataclass
class FlightTemplateSummary:
    id: int
    op_template_id: int
    flight_index: int
    flight_letter: str
    flight_name: str
    aircraft: str | None
    aircraft_count: int | None
    slot_count: int


@dataclass
class OpEventRecord:
    event_id: int
    op_template_id: int
    op_name: str
    op_type: str
    scheduled_by: str | None
    scheduled_at: int
    server_event_id: str | None
    reservation_board_message_id: str | None
    status: str
    reservation_slots: int
    created_at: int
    updated_at: int


def now_ts() -> int:
    return int(time.time())


def new_schedule_series_id() -> str:
    return uuid.uuid4().hex


def safe_zoneinfo(timezone_name: str | None) -> ZoneInfo:
    try:
        if timezone_name:
            return ZoneInfo(str(timezone_name))
    except ZoneInfoNotFoundError:
        pass

    return ZoneInfo(str(SCHEDULE_DEFAULT_TIMEZONE or "America/New_York"))


def clean_text(value: Any) -> str | None:
    if value is None:
        return None

    text = str(value).strip()

    return text or None


def int_or_none(value: Any) -> int | None:
    if value is None:
        return None

    try:
        return int(value)
    except Exception:
        return None


def normalize_default_slots() -> list[DefaultScheduleSlot]:
    slots: list[DefaultScheduleSlot] = []

    for item in SCHEDULE_DEFAULT_SLOTS:
        if isinstance(item, dict):
            slots.append(
                DefaultScheduleSlot(
                    label=str(item["label"]),
                    weekday=int(item["weekday"]),
                    hour=int(item["hour"]),
                    minute=int(item.get("minute", 0)),
                )
            )
        else:
            label, weekday, hour, minute = item
            slots.append(
                DefaultScheduleSlot(
                    label=str(label),
                    weekday=int(weekday),
                    hour=int(hour),
                    minute=int(minute),
                )
            )

    return slots


def table_columns(table_name: str) -> set[str]:
    with get_connection() as conn:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()

    return {str(row["name"]) for row in rows}


def table_create_sql(table_name: str) -> str:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT sql
            FROM sqlite_master
            WHERE type = 'table'
              AND name = ?
            LIMIT 1
            """,
            (table_name,),
        ).fetchone()

    if row is None or row["sql"] is None:
        return ""

    return str(row["sql"])


def ensure_op_reservations_allows_canceled() -> None:
    """
    Older DB schema only allowed reservation status:
    open, reserved, locked

    Canceling an event needs to set reservation rows to:
    canceled

    SQLite cannot ALTER a CHECK constraint directly, so this rebuilds
    op_reservations once if the CHECK does not already include canceled.
    """
    create_sql = table_create_sql("op_reservations")

    if "'canceled'" in create_sql or '"canceled"' in create_sql:
        return

    conn = get_connection()

    try:
        conn.execute("PRAGMA foreign_keys = OFF;")
        conn.execute("BEGIN;")

        conn.execute(
            """
            CREATE TABLE op_reservations_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                op_event_id INTEGER NOT NULL,

                slot_index INTEGER NOT NULL,
                slot_label TEXT NOT NULL,

                reserved_by TEXT,
                reserved_at INTEGER,

                status TEXT NOT NULL DEFAULT 'open',

                FOREIGN KEY (op_event_id) REFERENCES op_events(event_id) ON DELETE CASCADE,
                FOREIGN KEY (reserved_by) REFERENCES users(discord_id),

                UNIQUE (op_event_id, slot_index),

                CHECK (status IN ('open', 'reserved', 'locked', 'canceled'))
            )
            """
        )

        conn.execute(
            """
            INSERT INTO op_reservations_new (
                id,
                op_event_id,
                slot_index,
                slot_label,
                reserved_by,
                reserved_at,
                status
            )
            SELECT
                id,
                op_event_id,
                slot_index,
                slot_label,
                reserved_by,
                reserved_at,
                CASE
                    WHEN status IN ('open', 'reserved', 'locked', 'canceled') THEN status
                    ELSE 'open'
                END
            FROM op_reservations
            """
        )

        conn.execute("DROP TABLE op_reservations")
        conn.execute("ALTER TABLE op_reservations_new RENAME TO op_reservations")
        conn.execute("COMMIT;")
    except Exception:
        conn.execute("ROLLBACK;")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.close()


def ensure_schedule_schema() -> None:
    """
    Uses your existing schema:
    - op_events
    - op_reservations

    Adds lightweight compatibility columns/indexes when needed.
    """
    ensure_op_reservations_allows_canceled()

    columns = table_columns("op_events")

    with get_connection() as conn:
        if "schedule_series_id" not in columns:
            conn.execute("ALTER TABLE op_events ADD COLUMN schedule_series_id TEXT")

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_op_events_status_scheduled_at
            ON op_events(status, scheduled_at)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_op_events_schedule_series
            ON op_events(schedule_series_id)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_op_events_template
            ON op_events(op_template_id)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_op_reservations_event
            ON op_reservations(op_event_id)
            """
        )


def get_user_timezone(discord_id: str) -> str:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT timezone
            FROM user_settings
            WHERE discord_id = ?
            LIMIT 1
            """,
            (str(discord_id),),
        ).fetchone()

    if row and row["timezone"]:
        return str(row["timezone"])

    return str(SCHEDULE_DEFAULT_TIMEZONE or "America/New_York")


def get_op_templates(limit: int = 25, search_text: str = "") -> list[OpTemplateSummary]:
    search_text = str(search_text or "").strip()

    params: list[Any] = []
    where = "1 = 1"

    if search_text:
        where += " AND name LIKE ?"
        params.append(f"%{search_text}%")

    params.append(int(limit))

    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT
                id,
                name,
                description,
                type,
                total_players,
                flight_count
            FROM op_templates
            WHERE {where}
            ORDER BY name COLLATE NOCASE ASC, id ASC
            LIMIT ?
            """,
            params,
        ).fetchall()

    return [
        OpTemplateSummary(
            id=int(row["id"]),
            name=str(row["name"]),
            op_type=str(row["type"]),
            description=clean_text(row["description"]),
            total_players=int(row["total_players"]),
            flight_count=int(row["flight_count"]),
        )
        for row in rows
    ]


def get_op_template(op_template_id: int) -> OpTemplateSummary | None:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                id,
                name,
                description,
                type,
                total_players,
                flight_count
            FROM op_templates
            WHERE id = ?
            LIMIT 1
            """,
            (int(op_template_id),),
        ).fetchone()

    if row is None:
        return None

    return OpTemplateSummary(
        id=int(row["id"]),
        name=str(row["name"]),
        op_type=str(row["type"]),
        description=clean_text(row["description"]),
        total_players=int(row["total_players"]),
        flight_count=int(row["flight_count"]),
    )


def resolve_op_template(opname_value: str) -> OpTemplateSummary | None:
    value = str(opname_value).strip()

    if value.isdigit():
        template = get_op_template(int(value))
        if template:
            return template

    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT id
            FROM op_templates
            WHERE name = ?
            COLLATE NOCASE
            LIMIT 1
            """,
            (value,),
        ).fetchone()

    if row is None:
        return None

    return get_op_template(int(row["id"]))


def get_flight_templates(op_template_id: int) -> list[FlightTemplateSummary]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                id,
                op_template_id,
                flight_index,
                flight_letter,
                flight_name,
                aircraft,
                aircraft_count,
                slot_count
            FROM flight_templates
            WHERE op_template_id = ?
            ORDER BY flight_index ASC, id ASC
            """,
            (int(op_template_id),),
        ).fetchall()

    return [
        FlightTemplateSummary(
            id=int(row["id"]),
            op_template_id=int(row["op_template_id"]),
            flight_index=int(row["flight_index"]),
            flight_letter=str(row["flight_letter"]),
            flight_name=str(row["flight_name"]),
            aircraft=clean_text(row["aircraft"]),
            aircraft_count=int_or_none(row["aircraft_count"]),
            slot_count=int(row["slot_count"]),
        )
        for row in rows
    ]


def op_event_from_row(row: Any) -> OpEventRecord:
    return OpEventRecord(
        event_id=int(row["event_id"]),
        op_template_id=int(row["op_template_id"]),
        op_name=str(row["op_name"]),
        op_type=str(row["op_type"]),
        scheduled_by=clean_text(row["scheduled_by"]),
        scheduled_at=int(row["scheduled_at"]),
        server_event_id=clean_text(row["server_event_id"]),
        reservation_board_message_id=clean_text(row["reservation_board_message_id"]),
        status=str(row["status"]),
        reservation_slots=int(row["reservation_slots"]),
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
    )


def get_scheduled_op_events(
    limit: int = 150,
    sort_by: str = "time",
) -> list[OpEventRecord]:
    ensure_schedule_schema()

    if sort_by == "id":
        order_clause = "op_events.event_id ASC"
    else:
        order_clause = "op_events.scheduled_at ASC, op_events.event_id ASC"

    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT
                op_events.event_id,
                op_events.op_template_id,
                op_templates.name AS op_name,
                op_templates.type AS op_type,
                op_events.scheduled_by,
                op_events.scheduled_at,
                op_events.server_event_id,
                op_events.reservation_board_message_id,
                op_events.status,
                op_events.reservation_slots,
                op_events.created_at,
                op_events.updated_at
            FROM op_events
            JOIN op_templates
                ON op_templates.id = op_events.op_template_id
            WHERE op_events.status IN ('Scheduled', 'Briefing', 'Open', 'Canceled')
            ORDER BY {order_clause}
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()

    return [op_event_from_row(row) for row in rows]


def get_op_event(event_id: int) -> OpEventRecord | None:
    ensure_schedule_schema()

    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                op_events.event_id,
                op_events.op_template_id,
                op_templates.name AS op_name,
                op_templates.type AS op_type,
                op_events.scheduled_by,
                op_events.scheduled_at,
                op_events.server_event_id,
                op_events.reservation_board_message_id,
                op_events.status,
                op_events.reservation_slots,
                op_events.created_at,
                op_events.updated_at
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

    return op_event_from_row(row)



def next_default_slot_datetime_after(
    slot: DefaultScheduleSlot,
    after_dt: datetime,
) -> datetime:
    days_ahead = (slot.weekday - after_dt.weekday()) % 7
    candidate = after_dt + timedelta(days=days_ahead)
    candidate = candidate.replace(
        hour=slot.hour,
        minute=slot.minute,
        second=0,
        microsecond=0,
    )

    if candidate <= after_dt:
        candidate += timedelta(days=7)

    return candidate


def next_default_slot_timestamp(slot: DefaultScheduleSlot) -> int:
    default_tz = safe_zoneinfo(SCHEDULE_DEFAULT_TIMEZONE)
    now = datetime.now(default_tz)

    return int(next_default_slot_datetime_after(slot, now).timestamp())


def get_next_default_slot_timestamps() -> list[tuple[DefaultScheduleSlot, int]]:
    slots = normalize_default_slots()

    if not slots:
        return []

    default_tz = safe_zoneinfo(SCHEDULE_DEFAULT_TIMEZONE)
    cursor = datetime.now(default_tz)
    rows: list[tuple[DefaultScheduleSlot, int]] = []

    # Respect SCHEDULE_DEFAULT_SLOTS exactly as written.
    # Each slot is scheduled after the previous configured slot.
    for slot in slots:
        candidate = next_default_slot_datetime_after(slot, cursor)
        rows.append((slot, int(candidate.timestamp())))
        cursor = candidate

    return rows


def format_timestamp_local(timestamp: int, timezone_name: str) -> str:
    tz = safe_zoneinfo(timezone_name)
    dt = datetime.fromtimestamp(int(timestamp), tz)

    hour = dt.hour % 12
    if hour == 0:
        hour = 12

    return f"{dt.strftime('%A %B')} {dt.day}, {hour}:{dt.minute:02d} {dt.strftime('%p')}"


def format_timestamp_short(timestamp: int, timezone_name: str) -> str:
    tz = safe_zoneinfo(timezone_name)
    dt = datetime.fromtimestamp(int(timestamp), tz)

    hour = dt.hour % 12
    if hour == 0:
        hour = 12

    return f"{dt.strftime('%a %b')} {dt.day} {hour}:{dt.minute:02d}{dt.strftime('%p')}"


def is_default_slot_timestamp(timestamp: int) -> bool:
    """
    Returns True if the event time lines up with one of the configured
    default schedule slots in SCHEDULE_DEFAULT_TIMEZONE.
    """
    default_tz = safe_zoneinfo(SCHEDULE_DEFAULT_TIMEZONE)
    dt = datetime.fromtimestamp(int(timestamp), default_tz)

    for slot in normalize_default_slots():
        if (
            dt.weekday() == slot.weekday
            and dt.hour == slot.hour
            and dt.minute == slot.minute
        ):
            return True

    return False


def build_day_choices(timezone_name: str, days: int = 25) -> list[tuple[str, str]]:
    tz = safe_zoneinfo(timezone_name)
    today = datetime.now(tz).date()

    choices: list[tuple[str, str]] = []

    for offset in range(days):
        current = today + timedelta(days=offset)
        label = f"{current.strftime('%A %B')} {current.day}"
        value = current.isoformat()
        choices.append((label, value))

    return choices


def custom_datetime_timestamp(
    *,
    day_iso: str,
    hour_value: int,
    minute_value: int,
    timezone_name: str,
) -> int:
    tz = safe_zoneinfo(timezone_name)

    selected_date = date.fromisoformat(str(day_iso))

    hour_value = int(hour_value)
    minute_value = int(minute_value)

    # UI uses 1-24. 24 is treated as 00:00 on the selected date.
    hour = 0 if hour_value == 24 else hour_value

    dt = datetime(
        selected_date.year,
        selected_date.month,
        selected_date.day,
        hour,
        minute_value,
        tzinfo=tz,
    )

    return int(dt.timestamp())


def reservation_slot_label(flight: FlightTemplateSummary) -> str:
    aircraft = f" — {flight.aircraft}" if flight.aircraft else ""

    return f"{flight.flight_letter} | {flight.flight_name}{aircraft}"[:100]


def create_op_event(
    *,
    op_template_id: int,
    scheduled_at: int,
    scheduled_by: str,
    server_event_id: str | None = None,
    schedule_series_id: str | None = None,
) -> int:
    ensure_schedule_schema()

    template = get_op_template(op_template_id)

    if template is None:
        raise ValueError("Selected op template does not exist.")

    if int(scheduled_at) <= now_ts():
        raise ValueError("Scheduled time must be in the future.")

    flights = get_flight_templates(op_template_id)
    reservation_slots = len(flights)
    ts = now_ts()

    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO op_events (
                op_template_id,
                scheduled_by,
                scheduled_at,
                server_event_id,
                schedule_series_id,
                reservation_board_message_id,
                status,
                reservation_slots,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, NULL, 'Scheduled', ?, ?, ?)
            """,
            (
                int(op_template_id),
                str(scheduled_by),
                int(scheduled_at),
                clean_text(server_event_id),
                clean_text(schedule_series_id),
                int(reservation_slots),
                ts,
                ts,
            ),
        )

        event_id = int(cur.lastrowid)

        for index, flight in enumerate(flights, start=1):
            conn.execute(
                """
                INSERT INTO op_reservations (
                    op_event_id,
                    slot_index,
                    slot_label,
                    reserved_by,
                    reserved_at,
                    status
                )
                VALUES (?, ?, ?, NULL, NULL, 'open')
                """,
                (
                    event_id,
                    index,
                    reservation_slot_label(flight),
                ),
            )

    return event_id


def set_event_server_event_id(
    *,
    event_id: int,
    server_event_id: str | None,
) -> None:
    ensure_schedule_schema()

    with get_connection() as conn:
        conn.execute(
            """
            UPDATE op_events
            SET server_event_id = ?,
                updated_at = ?
            WHERE event_id = ?
            """,
            (
                clean_text(server_event_id),
                now_ts(),
                int(event_id),
            ),
        )


def cancel_op_event(event_id: int) -> None:
    ensure_schedule_schema()

    with get_connection() as conn:
        conn.execute(
            """
            UPDATE op_events
            SET status = 'Canceled',
                updated_at = ?
            WHERE event_id = ?
              AND status IN ('Scheduled', 'Briefing', 'Open')
            """,
            (now_ts(), int(event_id)),
        )

        conn.execute(
            """
            UPDATE op_reservations
            SET status = 'canceled'
            WHERE op_event_id = ?
              AND status IN ('open', 'reserved', 'locked')
            """,
            (int(event_id),),
        )




def uncancel_op_event(event_id: int) -> None:
    ensure_schedule_schema()

    with get_connection() as conn:
        conn.execute(
            """
            UPDATE op_events
            SET status = 'Scheduled',
                updated_at = ?
            WHERE event_id = ?
              AND status = 'Canceled'
            """,
            (now_ts(), int(event_id)),
        )

        conn.execute(
            """
            UPDATE op_reservations
            SET status = 'open',
                reserved_by = NULL,
                reserved_at = NULL
            WHERE op_event_id = ?
              AND status = 'canceled'
            """,
            (int(event_id),),
        )


def get_reservations_for_event(event_id: int) -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                slot_index,
                slot_label,
                reserved_by,
                reserved_at,
                status
            FROM op_reservations
            WHERE op_event_id = ?
            ORDER BY slot_index ASC
            """,
            (int(event_id),),
        ).fetchall()

    return [dict(row) for row in rows]
