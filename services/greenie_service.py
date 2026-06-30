from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

import discord

from database import get_connection
from services.wire_gpa_service import (
    bolter_score,
    gpa_scale_footer_text,
    wire_score_map,
)


try:
    from config import GREENIE_ATTEMPT_HISTORY_LENGTH
except ImportError:
    GREENIE_ATTEMPT_HISTORY_LENGTH = 16

try:
    from config import GREENIE_NORMAL_OPS_ONLY
except ImportError:
    GREENIE_NORMAL_OPS_ONLY = True

try:
    from config import GREENIE_AIRFRAME_ORDER
except ImportError:
    GREENIE_AIRFRAME_ORDER = [
        "AV-42C",
        "F/A-26B",
        "F-45A",
        "T-55",
        "EF-24G",
    ]



WIRE_EMOJIS = {
    1: "🟥",
    2: "🟨",
    3: "🟩",
    4: "🟧",
}
BOLTER_EMOJI = "🟦"
CAG_DCAG_BOLTER_EMOJI = "💀"
SPECIAL_SPEAKING_BOLTER_EMOJI = "🗣️"


@dataclass(frozen=True)
class GreenieAirframe:
    aircraft: str
    attempts: list[str]
    gpa_attempt_count: int
    gpa: float | None


@dataclass(frozen=True)
class GreenieBoard:
    discord_id: str
    player_name: str
    airframes: list[GreenieAirframe]
    total_gpa_attempt_count: int
    total_gpa: float | None


def clean_text(value: Any) -> str | None:
    if value is None:
        return None

    value = str(value).strip()
    return value or None


def history_length() -> int:
    try:
        return max(1, min(50, int(GREENIE_ATTEMPT_HISTORY_LENGTH)))
    except (TypeError, ValueError):
        return 16


def normal_ops_only() -> bool:
    return bool(GREENIE_NORMAL_OPS_ONLY)


def normalize_aircraft_key(value: Any) -> str:
    text = clean_text(value) or ""
    text = text.upper().replace("–", "-").replace("—", "-")
    text = re.sub(r"[^A-Z0-9]+", "", text)

    aliases = {
        "FA26": "FA26B",
        "FA26B": "FA26B",
        "FA26C": "FA26B",
        "EF24": "EF24G",
        "EF24G": "EF24G",
        "F45": "F45A",
        "F45A": "F45A",
        "AV42": "AV42C",
        "AV42C": "AV42C",
    }

    return aliases.get(text, text)


def configured_airframe_order() -> list[str]:
    values = GREENIE_AIRFRAME_ORDER

    if not isinstance(values, (list, tuple)):
        values = ["AV-42C", "F/A-26B", "F-45A", "T-55", "EF-24G"]

    output: list[str] = []
    seen: set[str] = set()

    for value in values:
        display = clean_text(value)
        key = normalize_aircraft_key(display)

        if not display or not key or key in seen:
            continue

        seen.add(key)
        output.append(display)

    return output


def canonical_airframe_display(value: Any) -> str:
    raw = clean_text(value) or "Unknown"
    key = normalize_aircraft_key(raw)

    for configured in configured_airframe_order():
        if normalize_aircraft_key(configured) == key:
            return configured

    return raw


def safe_int(value: Any, *, minimum: int = 0, maximum: int = 24) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return 0

    return max(minimum, min(maximum, result))


def resolve_greenie_player_name(discord_id: str, fallback_name: str | None = None) -> str:
    """Use current display name when available, otherwise historical attendance."""
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                COALESCE(
                    NULLIF(u.display_name, ''),
                    NULLIF(recent_attendance.user_name, ''),
                    NULLIF(u.discord_username, ''),
                    ?
                ) AS player_name
            FROM (SELECT ? AS discord_id) requested
            LEFT JOIN users u
                ON u.discord_id = requested.discord_id
            LEFT JOIN attendance recent_attendance
                ON recent_attendance.entry_id = (
                    SELECT candidate.entry_id
                    FROM attendance candidate
                    WHERE candidate.discord_id = requested.discord_id
                      AND NULLIF(TRIM(candidate.user_name), '') IS NOT NULL
                    ORDER BY candidate.created_at DESC,
                             candidate.logged_at DESC,
                             candidate.entry_id DESC
                    LIMIT 1
                )
            """,
            (clean_text(fallback_name) or str(discord_id), str(discord_id)),
        ).fetchone()

    if row and clean_text(row["player_name"]):
        return str(row["player_name"])

    return clean_text(fallback_name) or str(discord_id)


def load_greenie_board(
    *,
    discord_id: str,
    fallback_name: str | None = None,
    bolter_emoji: str | None = None,
) -> GreenieBoard:
    """Load all carrier attempts and wire GPA history for one user.

    Attempt history is kept per airframe. A row with two bolters and a 3-wire
    expands to: 🟦🟦🟩. Only the latest configured number of attempts per
    airframe is displayed, but GPA uses all qualifying historical carrier
    attempts. Each bolter contributes a 0.0 score and one GPA attempt.
    """
    operation_type_filter = "AND ot.type = 'Normal'" if normal_ops_only() else ""
    selected_bolter_emoji = clean_text(bolter_emoji) or BOLTER_EMOJI

    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT
                a.entry_id,
                a.aircraft,
                a.wires,
                a.bolters,
                oe.event_id,
                oe.scheduled_at
            FROM attendance a
            JOIN op_events oe
                ON oe.event_id = a.scheduled_op_id
            JOIN op_templates ot
                ON ot.id = oe.op_template_id
            WHERE a.discord_id = ?
              AND oe.status = 'Complete'
              {operation_type_filter}
              AND a.status IN ('submitted', 'complete')
              AND a.landing_type = 'Arrested'
              AND (
                    a.wires BETWEEN 1 AND 4
                 OR COALESCE(a.bolters, 0) > 0
              )
            ORDER BY oe.scheduled_at ASC, oe.event_id ASC, a.entry_id ASC
            """,
            (str(discord_id),),
        ).fetchall()

    attempts_by_airframe: dict[str, list[str]] = defaultdict(list)
    gpa_scores_by_airframe: dict[str, list[float]] = defaultdict(list)

    for row in rows:
        aircraft = canonical_airframe_display(row["aircraft"])
        wire = safe_int(row["wires"], minimum=0, maximum=4)
        bolters = safe_int(row["bolters"], minimum=0, maximum=24)

        # A bolter occurs before the wire caught on the same recovery attempt.
        attempts_by_airframe[aircraft].extend([selected_bolter_emoji] * bolters)
        gpa_scores_by_airframe[aircraft].extend([bolter_score()] * bolters)

        scores = wire_score_map()
        if wire in scores:
            attempts_by_airframe[aircraft].append(WIRE_EMOJIS[wire])
            gpa_scores_by_airframe[aircraft].append(scores[wire])

    order = configured_airframe_order()
    order_keys = {
        normalize_aircraft_key(name): index
        for index, name in enumerate(order)
    }

    known_airframes = set(attempts_by_airframe) | set(gpa_scores_by_airframe)
    ordered_airframes = sorted(
        known_airframes,
        key=lambda aircraft: (
            order_keys.get(normalize_aircraft_key(aircraft), 10_000),
            aircraft.casefold(),
        ),
    )

    airframes: list[GreenieAirframe] = []

    for aircraft in ordered_airframes:
        attempts = attempts_by_airframe.get(aircraft, [])[-history_length():]
        scores = gpa_scores_by_airframe.get(aircraft, [])
        gpa = (sum(scores) / len(scores)) if scores else None

        airframes.append(
            GreenieAirframe(
                aircraft=aircraft,
                attempts=attempts,
                gpa_attempt_count=len(scores),
                gpa=gpa,
            )
        )

    all_scores = [
        score
        for scores in gpa_scores_by_airframe.values()
        for score in scores
    ]

    total_gpa = (sum(all_scores) / len(all_scores)) if all_scores else None

    return GreenieBoard(
        discord_id=str(discord_id),
        player_name=resolve_greenie_player_name(discord_id, fallback_name),
        airframes=airframes,
        total_gpa_attempt_count=len(all_scores),
        total_gpa=total_gpa,
    )


def make_attempt_lines(board: GreenieBoard) -> str:
    if not board.airframes:
        return "No qualifying arrested carrier attempts found."

    label_width = max(len(airframe.aircraft) for airframe in board.airframes)
    lines: list[str] = []

    for airframe in board.airframes:
        history = "".join(airframe.attempts) or "—"
        lines.append(f"`{airframe.aircraft:<{label_width}}` {history}")

    return "\n".join(lines)[:1024]


def make_gpa_lines(board: GreenieBoard) -> str:
    """Render career GPA values at three decimals for the Greenie Board only."""
    rows: list[tuple[str, str]] = []

    if board.total_gpa is not None:
        rows.append(("Career Total", f"{board.total_gpa:.3f}"))
    else:
        rows.append(("Career Total", "—"))

    for airframe in board.airframes:
        value = f"{airframe.gpa:.3f}" if airframe.gpa is not None else "—"
        rows.append((airframe.aircraft, value))

    label_width = max(len(label) for label, _value in rows)
    return (
        "```\n"
        + "\n".join(f"{label:<{label_width}}  {value}" for label, value in rows)
        + "\n```"
    )[:1024]



def build_greenie_embed(
    *,
    discord_id: str,
    fallback_name: str | None = None,
    bolter_emoji: str | None = None,
) -> discord.Embed:
    board = load_greenie_board(
        discord_id=str(discord_id),
        fallback_name=fallback_name,
        bolter_emoji=bolter_emoji,
    )

    embed = discord.Embed(
        title=f"Greenie Board for {board.player_name}",
        description=(
            f"**Last {history_length()} carrier attempts per airframe**\n"
            "🟧 4-wire  •  🟩 3-wire  •  🟨 2-wire  •  "
            "🟥 1-wire  •  🟦 Bolter"
        ),
    )

    embed.add_field(
        name="Recent Attempts",
        value=make_attempt_lines(board),
        inline=False,
    )
    embed.add_field(
        name="Career Wire GPA (4.000 is highest)",
        value=make_gpa_lines(board),
        inline=False,
    )
    embed.set_footer(
        text=(
            "GPA scale: "
            + gpa_scale_footer_text()
            + " | Career total includes all qualifying attempts"
            + (
                " | Completed Normal ops only"
                if normal_ops_only()
                else " | All completed op types"
            )
        )
    )

    return embed
