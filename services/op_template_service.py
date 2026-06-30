from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from database import get_connection

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

try:
    from config import OP_TYPES as CONFIG_OP_TYPES
except ImportError:
    CONFIG_OP_TYPES = ["Normal", "Mini", "Arcade", "Tournament"]

# Keep Training available even if config.py has not been updated yet.
OP_TYPES = list(dict.fromkeys([*CONFIG_OP_TYPES, "Training"]))


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
class OpTemplateRow:
    id: int
    name: str
    total_players: int
    flight_count: int
    runtime_count: int
    op_type: str
    briefing: str | None
    creator: str | None
    creator_display_name: str | None
    completed_event_ids: list[int]


@dataclass
class FlightTemplateDraft:
    id: int | None = None
    flight_index: int = 1
    flight_letter: str | None = None
    flight_name: str | None = None
    aircraft: str | None = None
    aircraft_count: int | None = None
    slot_count: int | None = None
    description: str | None = None


@dataclass
class OpTemplateEditDraft:
    id: int
    name: str
    description: str | None
    op_type: str
    total_players: int
    flight_count: int
    briefing: str | None
    creator: str | None
    used_count: int
    flights: list[FlightTemplateDraft] = field(default_factory=list)
    selected_flight_index: int = 0

    @property
    def full_edit_allowed(self) -> bool:
        return self.used_count <= 0


def clean_text(value: Any) -> str | None:
    if value is None:
        return None

    text = str(value).strip()

    return text or None


def now_ts() -> int:
    return int(time.time())


def ensure_op_template_columns() -> None:
    with get_connection() as conn:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(op_templates)").fetchall()
        }

        if "briefing" not in columns:
            conn.execute("ALTER TABLE op_templates ADD COLUMN briefing TEXT")

        if "creator" not in columns:
            conn.execute("ALTER TABLE op_templates ADD COLUMN creator TEXT")


def runtime_count_for_template(conn, template_id: int) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM op_events
        WHERE op_template_id = ?
        """,
        (int(template_id),),
    ).fetchone()

    return int(row["count"] or 0)


def completed_event_ids_for_template(conn, template_id: int) -> list[int]:
    rows = conn.execute(
        """
        SELECT event_id
        FROM op_events
        WHERE op_template_id = ?
          AND status = 'Complete'
        ORDER BY event_id DESC
        """,
        (int(template_id),),
    ).fetchall()

    return [
        int(row["event_id"])
        for row in rows
    ]


def creator_display_name_from_id(conn, creator: str | None) -> str | None:
    creator_id = clean_text(creator)

    if not creator_id:
        return None

    row = conn.execute(
        """
        SELECT discord_username, display_name
        FROM users
        WHERE discord_id = ?
        LIMIT 1
        """,
        (creator_id,),
    ).fetchone()

    if row is None:
        return creator_id

    return (
        clean_text(row["display_name"])
        or clean_text(row["discord_username"])
        or creator_id
    )


def creator_display_name(creator: str | None) -> str | None:
    with get_connection() as conn:
        return creator_display_name_from_id(conn, creator)


def list_op_templates() -> list[OpTemplateRow]:
    ensure_op_template_columns()

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                ot.id,
                ot.name,
                ot.total_players,
                ot.flight_count,
                ot.type,
                ot.briefing,
                ot.creator,
                COUNT(oe.event_id) AS runtime_count
            FROM op_templates ot
            LEFT JOIN op_events oe ON oe.op_template_id = ot.id
            GROUP BY ot.id
            ORDER BY ot.id DESC
            """
        ).fetchall()

        result: list[OpTemplateRow] = []

        for row in rows:
            creator = clean_text(row["creator"])
            result.append(
                OpTemplateRow(
                    id=int(row["id"]),
                    name=str(row["name"]),
                    total_players=int(row["total_players"] or 0),
                    flight_count=int(row["flight_count"] or 0),
                    runtime_count=int(row["runtime_count"] or 0),
                    op_type=str(row["type"] or "Normal"),
                    briefing=clean_text(row["briefing"]),
                    creator=creator,
                    creator_display_name=creator_display_name_from_id(conn, creator),
                    completed_event_ids=completed_event_ids_for_template(conn, int(row["id"])),
                )
            )

    return result


def get_template_row(template_id: int) -> OpTemplateRow | None:
    ensure_op_template_columns()

    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                ot.id,
                ot.name,
                ot.total_players,
                ot.flight_count,
                ot.type,
                ot.briefing,
                ot.creator,
                COUNT(oe.event_id) AS runtime_count
            FROM op_templates ot
            LEFT JOIN op_events oe ON oe.op_template_id = ot.id
            WHERE ot.id = ?
            GROUP BY ot.id
            LIMIT 1
            """,
            (int(template_id),),
        ).fetchone()

        if row is None:
            return None

        creator = clean_text(row["creator"])

        return OpTemplateRow(
            id=int(row["id"]),
            name=str(row["name"]),
            total_players=int(row["total_players"] or 0),
            flight_count=int(row["flight_count"] or 0),
            runtime_count=int(row["runtime_count"] or 0),
            op_type=str(row["type"] or "Normal"),
            briefing=clean_text(row["briefing"]),
            creator=creator,
            creator_display_name=creator_display_name_from_id(conn, creator),
            completed_event_ids=completed_event_ids_for_template(conn, int(row["id"])),
        )


def load_edit_draft(template_id: int) -> OpTemplateEditDraft | None:
    ensure_op_template_columns()

    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM op_templates
            WHERE id = ?
            LIMIT 1
            """,
            (int(template_id),),
        ).fetchone()

        if row is None:
            return None

        used_count = runtime_count_for_template(conn, int(template_id))

        flight_rows = conn.execute(
            """
            SELECT *
            FROM flight_templates
            WHERE op_template_id = ?
            ORDER BY flight_index ASC
            """,
            (int(template_id),),
        ).fetchall()

    flights = [
        FlightTemplateDraft(
            id=int(flight["id"]) if flight["id"] is not None else None,
            flight_index=int(flight["flight_index"] or 0),
            flight_letter=clean_text(flight["flight_letter"]),
            flight_name=clean_text(flight["flight_name"]),
            aircraft=clean_text(flight["aircraft"]),
            aircraft_count=int(flight["aircraft_count"]) if flight["aircraft_count"] is not None else None,
            slot_count=int(flight["slot_count"]) if flight["slot_count"] is not None else None,
            description=clean_text(flight["description"]),
        )
        for flight in flight_rows
    ]

    return OpTemplateEditDraft(
        id=int(row["id"]),
        name=str(row["name"]),
        description=clean_text(row["description"]),
        op_type=str(row["type"] or "Normal"),
        total_players=int(row["total_players"] or 0),
        flight_count=int(row["flight_count"] or len(flights)),
        briefing=clean_text(row["briefing"]) if "briefing" in row.keys() else None,
        creator=clean_text(row["creator"]) if "creator" in row.keys() else None,
        used_count=used_count,
        flights=flights,
    )


def get_active_airframes() -> list[dict[str, Any]]:
    airframes: list[dict[str, Any]] = []

    for index, option in enumerate(AIRCRAFT_OPTIONS):
        if isinstance(option, dict):
            name = str(option.get("name") or "").strip()
            max_seats = int(option.get("max_seats") or 1)
        elif isinstance(option, (tuple, list)):
            name = str(option[0] if option else "").strip()
            max_seats = int(option[1] if len(option) > 1 else 1)
        else:
            name = str(option).strip()
            max_seats = int(DEFAULT_AIRCRAFT_MAX_SEATS.get(normalize_aircraft_name(name), 1))

        if name:
            airframes.append(
                {
                    "id": index,
                    "name": name,
                    "max_seats": max(1, max_seats),
                }
            )

    return airframes


def normalize_aircraft_name(value: str | None) -> str:
    if not value:
        return ""

    text = str(value).lower().strip()
    text = text.replace("–", "-").replace("—", "-")
    text = text.replace(" ", "")

    return text


def max_seats_for_aircraft(aircraft: str | None) -> int:
    normalized = normalize_aircraft_name(aircraft)

    for option in get_active_airframes():
        if normalize_aircraft_name(option["name"]) == normalized:
            return int(option["max_seats"])

    return int(DEFAULT_AIRCRAFT_MAX_SEATS.get(normalized, 1))


def player_slot_values_for_flight(flight: FlightTemplateDraft) -> list[int]:
    if not flight.aircraft or not flight.aircraft_count:
        return []

    min_slots = max(1, int(flight.aircraft_count))
    max_slots = max(min_slots, int(flight.aircraft_count) * max_seats_for_aircraft(flight.aircraft))

    return list(range(min_slots, max_slots + 1))


def normalize_op_name(value: str | None) -> str:
    return (clean_text(value) or "").upper()


def normalize_flight_name(value: str | None) -> str:
    text = clean_text(value) or ""

    return text.title()


def auto_flight_letter_from_name(value: str | None) -> str:
    text = clean_text(value) or ""

    for char in text.upper():
        if "A" <= char <= "Z":
            return char

    return ""


def resize_flights(draft: OpTemplateEditDraft, new_count: int) -> None:
    new_count = max(1, min(25, int(new_count)))

    while len(draft.flights) < new_count:
        next_index = len(draft.flights) + 1
        draft.flights.append(FlightTemplateDraft(flight_index=next_index))

    if len(draft.flights) > new_count:
        draft.flights = draft.flights[:new_count]

    for index, flight in enumerate(draft.flights, start=1):
        flight.flight_index = index

    draft.flight_count = new_count
    draft.selected_flight_index = max(0, min(draft.selected_flight_index, len(draft.flights) - 1))


def validate_edit_draft(draft: OpTemplateEditDraft) -> list[str]:
    errors: list[str] = []

    if not draft.name:
        errors.append("Op name is required.")

    if not draft.op_type:
        errors.append("Op type is required.")

    if not draft.total_players or draft.total_players <= 0:
        errors.append("Max players must be at least 1.")

    if not draft.flights:
        errors.append("At least one flight is required.")

    letters: list[str] = []

    for flight in draft.flights:
        if not flight.flight_name:
            errors.append(f"Flight {flight.flight_index}: name is required.")
        if not flight.flight_letter:
            errors.append(f"Flight {flight.flight_index}: letter is required.")

        if flight.flight_letter:
            letters.append(flight.flight_letter)

        if not flight.aircraft:
            errors.append(f"Flight {flight.flight_index}: airframe is required.")

        if not flight.aircraft_count:
            errors.append(f"Flight {flight.flight_index}: aircraft count is required.")

        valid_slots = player_slot_values_for_flight(flight)
        if not valid_slots:
            errors.append(f"Flight {flight.flight_index}: player slots require aircraft info.")
        elif flight.slot_count not in valid_slots:
            errors.append(
                f"Flight {flight.flight_index}: player slots must be {valid_slots[0]}-{valid_slots[-1]}."
            )

    if len(letters) != len(set(letters)):
        errors.append("Flight letters cannot repeat.")

    return errors


def save_edit_draft(draft: OpTemplateEditDraft) -> None:
    ensure_op_template_columns()
    ts = now_ts()

    with get_connection() as conn:
        used_count = runtime_count_for_template(conn, draft.id)

        if used_count > 0:
            conn.execute(
                """
                UPDATE op_templates
                SET description = ?,
                    briefing = ?,
                    creator = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    clean_text(draft.description),
                    clean_text(draft.briefing),
                    clean_text(draft.creator),
                    ts,
                    int(draft.id),
                ),
            )

            for flight in draft.flights:
                conn.execute(
                    """
                    UPDATE flight_templates
                    SET description = ?
                    WHERE id = ?
                      AND op_template_id = ?
                    """,
                    (
                        clean_text(flight.description),
                        int(flight.id),
                        int(draft.id),
                    ),
                )

            return

        errors = validate_edit_draft(draft)
        if errors:
            raise ValueError("; ".join(errors))

        conn.execute(
            """
            UPDATE op_templates
            SET name = ?,
                description = ?,
                type = ?,
                total_players = ?,
                flight_count = ?,
                briefing = ?,
                creator = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                normalize_op_name(draft.name),
                clean_text(draft.description),
                clean_text(draft.op_type) or "Normal",
                int(draft.total_players or 0),
                len(draft.flights),
                clean_text(draft.briefing),
                clean_text(draft.creator),
                ts,
                int(draft.id),
            ),
        )

        conn.execute(
            """
            DELETE FROM flight_templates
            WHERE op_template_id = ?
            """,
            (int(draft.id),),
        )

        for index, flight in enumerate(draft.flights, start=1):
            conn.execute(
                """
                INSERT INTO flight_templates (
                    op_template_id,
                    flight_index,
                    flight_letter,
                    flight_name,
                    aircraft,
                    aircraft_count,
                    slot_count,
                    description
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(draft.id),
                    index,
                    clean_text(flight.flight_letter),
                    clean_text(flight.flight_name),
                    clean_text(flight.aircraft),
                    int(flight.aircraft_count or 0),
                    int(flight.slot_count or 0),
                    clean_text(flight.description),
                ),
            )
