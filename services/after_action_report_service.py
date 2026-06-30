from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from database import get_connection
from services.wire_gpa_service import bolter_score, wire_score


try:
    from config import SCHEDULE_DEFAULT_TIMEZONE
except ImportError:
    SCHEDULE_DEFAULT_TIMEZONE = "America/New_York"


@dataclass(frozen=True)
class AfterActionEvent:
    event_id: int
    op_template_id: int | None
    op_name: str
    op_type: str | None
    scheduled_at: int | None
    status: str | None


@dataclass(frozen=True)
class AfterActionAttendance:
    entry_id: int
    slot: str | None
    name: str
    aircraft: str | None
    landing_type: str | None
    wires: int | None
    bolters: int
    combat_deaths: int
    attend_type: str | None
    status: str | None


@dataclass(frozen=True)
class AfterActionAward:
    award_id: int
    award_type: str
    name: str
    discord_id: str | None
    notes: str | None


@dataclass(frozen=True)
class AfterActionReport:
    event: AfterActionEvent
    attendance: list[AfterActionAttendance]
    awards: list[AfterActionAward]
    operation_gpa: float | None
    operation_attempts: int


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


def table_exists(conn, table_name: str) -> bool:
    row = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        LIMIT 1
        """,
        (str(table_name),),
    ).fetchone()

    return row is not None


def event_from_row(row: Any) -> AfterActionEvent:
    return AfterActionEvent(
        event_id=int(row["event_id"]),
        op_template_id=int_or_none(row["op_template_id"]),
        op_name=clean_text(row["op_name"]) or "Unknown Operation",
        op_type=clean_text(row["op_type"]),
        scheduled_at=int_or_none(row["scheduled_at"]),
        status=clean_text(row["status"]),
    )


def attendance_from_row(row: Any) -> AfterActionAttendance:
    return AfterActionAttendance(
        entry_id=int(row["entry_id"]),
        slot=clean_text(row["slot"]),
        name=(
            clean_text(row["display_name"])
            or clean_text(row["discord_username"])
            or clean_text(row["user_name"])
            or clean_text(row["discord_id"])
            or "Unknown"
        ),
        aircraft=clean_text(row["aircraft"]),
        landing_type=clean_text(row["landing_type"]),
        wires=int_or_none(row["wires"]),
        bolters=int_or_none(row["bolters"]) or 0,
        combat_deaths=int_or_none(row["combat_deaths"]) or 0,
        attend_type=clean_text(row["attend_type"]),
        status=clean_text(row["status"]),
    )


def award_from_row(row: Any) -> AfterActionAward:
    return AfterActionAward(
        award_id=int(row["award_id"]),
        award_type=clean_text(row["award_type"]) or "AWARD",
        name=(
            clean_text(row["display_name"])
            or clean_text(row["discord_username"])
            or clean_text(row["discord_id"])
            or "Unknown"
        ),
        discord_id=clean_text(row["discord_id"]),
        notes=clean_text(row["notes"]),
    )


def slot_sort_key(slot: str | None) -> tuple[str, int, int, str]:
    if not slot:
        return ("ZZZ", 999, 999, "")

    text = str(slot).strip().upper()

    # Expected examples: B1-1, C1-1, A2-3.
    letter = ""
    first_number = 999
    second_number = 999

    if text:
        letter = text[0]

    number_part = text[1:]
    if "-" in number_part:
        left, right = number_part.split("-", 1)
    else:
        left, right = number_part, ""

    try:
        first_number = int(left)
    except Exception:
        pass

    try:
        second_number = int(right)
    except Exception:
        pass

    return (letter, first_number, second_number, text)


def get_recent_events(limit: int = 25) -> list[AfterActionEvent]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                oe.event_id,
                oe.op_template_id,
                ot.name AS op_name,
                ot.type AS op_type,
                oe.scheduled_at,
                oe.status
            FROM op_events oe
            JOIN op_templates ot
                ON ot.id = oe.op_template_id
            WHERE oe.status = 'Complete'
            ORDER BY oe.scheduled_at DESC, oe.event_id DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()

    return [event_from_row(row) for row in rows]


def get_events_by_op_name(op_name: str, limit: int = 25) -> list[AfterActionEvent]:
    name = str(op_name or "").strip()

    if not name:
        return get_recent_events(limit=limit)

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                oe.event_id,
                oe.op_template_id,
                ot.name AS op_name,
                ot.type AS op_type,
                oe.scheduled_at,
                oe.status
            FROM op_events oe
            JOIN op_templates ot
                ON ot.id = oe.op_template_id
            WHERE oe.status = 'Complete'
              AND LOWER(ot.name) = LOWER(?)
            ORDER BY oe.scheduled_at DESC, oe.event_id DESC
            LIMIT ?
            """,
            (name, int(limit)),
        ).fetchall()

        if not rows:
            rows = conn.execute(
                """
                SELECT
                    oe.event_id,
                    oe.op_template_id,
                    ot.name AS op_name,
                    ot.type AS op_type,
                    oe.scheduled_at,
                    oe.status
                FROM op_events oe
                JOIN op_templates ot
                    ON ot.id = oe.op_template_id
                WHERE oe.status = 'Complete'
                  AND LOWER(ot.name) LIKE LOWER(?)
                ORDER BY oe.scheduled_at DESC, oe.event_id DESC
                LIMIT ?
                """,
                (f"%{name}%", int(limit)),
            ).fetchall()

    return [event_from_row(row) for row in rows]


def get_event_by_id(event_id: int) -> AfterActionEvent | None:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                oe.event_id,
                oe.op_template_id,
                ot.name AS op_name,
                ot.type AS op_type,
                oe.scheduled_at,
                oe.status
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

    return event_from_row(row)


def get_attendance_for_event(event_id: int) -> list[AfterActionAttendance]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                a.entry_id,
                a.slot,
                a.user_name,
                a.discord_id,
                a.aircraft,
                a.landing_type,
                a.wires,
                a.bolters,
                a.combat_deaths,
                a.attend_type,
                a.status,
                u.display_name,
                u.discord_username
            FROM attendance a
            LEFT JOIN users u
                ON u.discord_id = a.discord_id
            WHERE a.scheduled_op_id = ?
              AND COALESCE(a.status, '') NOT IN ('deleted', 'reset')
            ORDER BY a.slot ASC, a.entry_id ASC
            """,
            (int(event_id),),
        ).fetchall()

    attendance = [attendance_from_row(row) for row in rows]
    attendance.sort(key=lambda row: (slot_sort_key(row.slot), row.entry_id))
    return attendance


def get_awards_for_event(event_id: int) -> list[AfterActionAward]:
    with get_connection() as conn:
        if not table_exists(conn, "player_awards"):
            return []

        rows = conn.execute(
            """
            SELECT
                pa.award_id,
                pa.award_type,
                pa.discord_id,
                pa.notes,
                u.display_name,
                u.discord_username
            FROM player_awards pa
            LEFT JOIN users u
                ON u.discord_id = pa.discord_id
            WHERE pa.source_event_id = ?
              AND pa.status = 'active'
            ORDER BY pa.award_type ASC, pa.award_id ASC
            """,
            (int(event_id),),
        ).fetchall()

    return [award_from_row(row) for row in rows]


def compute_operation_gpa(attendance: list[AfterActionAttendance]) -> tuple[float | None, int]:
    points = 0.0
    attempts = 0

    for row in attendance:
        landing = (row.landing_type or "").strip().lower()
        if landing != "arrested":
            continue

        if row.wires is not None:
            score = wire_score(row.wires)
            if score is not None:
                points += float(score)
                attempts += 1

        if row.bolters > 0:
            points += float(row.bolters) * float(bolter_score())
            attempts += int(row.bolters)

    if attempts <= 0:
        return None, 0

    return points / attempts, attempts


def get_after_action_report(event_id: int) -> AfterActionReport | None:
    event = get_event_by_id(event_id)

    if event is None:
        return None

    attendance = get_attendance_for_event(event.event_id)
    awards = get_awards_for_event(event.event_id)
    operation_gpa, operation_attempts = compute_operation_gpa(attendance)

    return AfterActionReport(
        event=event,
        attendance=attendance,
        awards=awards,
        operation_gpa=operation_gpa,
        operation_attempts=operation_attempts,
    )


def autocomplete_op_template_names(current: str, limit: int = 25) -> list[str]:
    query = str(current or "").strip()

    with get_connection() as conn:
        if query:
            rows = conn.execute(
                """
                SELECT DISTINCT name
                FROM op_templates
                WHERE LOWER(name) LIKE LOWER(?)
                ORDER BY name COLLATE NOCASE ASC
                LIMIT ?
                """,
                (f"%{query}%", int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT DISTINCT name
                FROM op_templates
                ORDER BY updated_at DESC, name COLLATE NOCASE ASC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()

    return [str(row["name"]) for row in rows if row["name"]]


def configured_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(str(SCHEDULE_DEFAULT_TIMEZONE or "America/New_York"))
    except Exception:
        return ZoneInfo("America/New_York")


def format_event_datetime(timestamp: int | None) -> str:
    if not timestamp:
        return "Unknown"

    dt = datetime.fromtimestamp(int(timestamp), tz=configured_timezone())
    hour = dt.hour % 12 or 12
    ampm = "am" if dt.hour < 12 else "pm"

    # Avoid %-d / %-I because those fail on Windows.
    return f"{dt:%a} {dt:%b} {dt.day} {hour}:{dt:%M}{ampm}"



def format_event_select_datetime(timestamp: int | None) -> str:
    if not timestamp:
        return "Unknown"

    dt = datetime.fromtimestamp(int(timestamp), tz=configured_timezone())
    hour = dt.hour % 12 or 12
    ampm = "am" if dt.hour < 12 else "pm"

    return f"{dt:%m-%d-%Y} {hour}:{dt:%M}{ampm}"


def relative_time(timestamp: int | None) -> str:
    if not timestamp:
        return "unknown"

    now = int(datetime.now(tz=configured_timezone()).timestamp())
    delta = now - int(timestamp)

    future = delta < 0
    seconds = abs(delta)

    if seconds < 60:
        value = "just now"
    elif seconds < 3600:
        minutes = seconds // 60
        value = f"{minutes} minute{'s' if minutes != 1 else ''}"
    elif seconds < 86400:
        hours = seconds // 3600
        value = f"{hours} hour{'s' if hours != 1 else ''}"
    elif seconds < 604800:
        days = seconds // 86400
        value = f"{days} day{'s' if days != 1 else ''}"
    elif seconds < 2592000:
        weeks = seconds // 604800
        value = f"{weeks} week{'s' if weeks != 1 else ''}"
    elif seconds < 31536000:
        months = seconds // 2592000
        value = f"{months} month{'s' if months != 1 else ''}"
    else:
        years = seconds // 31536000
        value = f"{years} year{'s' if years != 1 else ''}"

    if value == "just now":
        return value

    return f"in {value}" if future else f"{value} ago"
