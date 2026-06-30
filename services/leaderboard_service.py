from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import discord

from database import get_connection
from services.display_name_service import prune_display_name
from services.reward_service import recent_active_awards, manual_award_display_name
from services.wire_gpa_service import (
    gpa_scale_footer_text,
    sql_gpa_score_expression,
)


try:
    from config import LEADERBOARD_CHANNEL_ID
except ImportError:
    LEADERBOARD_CHANNEL_ID = 0

try:
    from config import LEADERBOARD_WINDOW_DAYS
except ImportError:
    LEADERBOARD_WINDOW_DAYS = 50

try:
    from config import LEADERBOARD_STATE_FILE
except ImportError:
    LEADERBOARD_STATE_FILE = "leaderboard_messages.json"

try:
    from config import LEADERBOARD_UPDATE_DELAY_SECONDS
except ImportError:
    LEADERBOARD_UPDATE_DELAY_SECONDS = 5.0

try:
    from config import LEADERBOARD_MIN_WIRE_SAMPLES
except ImportError:
    LEADERBOARD_MIN_WIRE_SAMPLES = 3

try:
    from config import RECENT_OP_LEADERBOARD_MIN_WIRE_SAMPLES
except ImportError:
    RECENT_OP_LEADERBOARD_MIN_WIRE_SAMPLES = LEADERBOARD_MIN_WIRE_SAMPLES

try:
    from config import LEADERBOARD_MIN_SURVIVAL_OPS
except ImportError:
    LEADERBOARD_MIN_SURVIVAL_OPS = 5

try:
    from config import LEADERBOARD_TOP_LIMIT
except ImportError:
    LEADERBOARD_TOP_LIMIT = 5

try:
    from config import LEADERBOARD_COMMAND_ROWS
except ImportError:
    LEADERBOARD_COMMAND_ROWS = 20

try:
    from config import LEADERBOARD_AWARD_LIST_LIMIT
except ImportError:
    LEADERBOARD_AWARD_LIST_LIMIT = 8


try:
    from config import LEADERBOARD_AIRFRAME_SECTIONS
except ImportError:
    LEADERBOARD_AIRFRAME_SECTIONS = [
        "F/A-26B",
        "EF-24G",
        "F-45A",
        "AV-42C",
    ]

try:
    from config import LEADERBOARD_DECORATION_INTROS
except ImportError:
    LEADERBOARD_DECORATION_INTROS = {}


try:
    from config import LEADERBOARD_RECENT_HIGHLIGHT_DAYS
except ImportError:
    LEADERBOARD_RECENT_HIGHLIGHT_DAYS = 7


DEFAULT_DECORATION_INTROS = {
    "MANUAL": {
        "single": (
            "A pilot received a manual award for a great feat "
            "during an operation!"
        ),
        "plural": (
            "Pilots received manual awards for great feats "
            "during their operations!"
        ),
    },
    "FIRST_TIME": {
        "single": "Congratulations to our first-time op attender!",
        "plural": "Congratulations to our first-time op attenders!",
    },
    "ACE": {
        "single": (
            "A pilot completed an operation without losing any aircraft "
            "and caught a 3 wire without boltering!"
        ),
        "plural": (
            "We have some pilots who completed operations without losing any "
            "aircraft and caught a 3 wire without boltering!"
        ),
    },
    "GOLDEN_WRENCH": {
        "single": (
            "A pilot received the award for 5 operations in a row "
            "without losing an aircraft."
        ),
        "plural": (
            "A few pilots received the award for 5 operations in a row "
            "without losing an aircraft."
        ),
    },
    "SAFETY_S": {
        "single": (
            "A pilot received an award for 5 clean arrested landings "
            "in a row without a single bolter."
        ),
        "plural": (
            "A few pilots received an award for 5 clean arrested landings "
            "in a row without a single bolter."
        ),
    },
}


@dataclass(frozen=True)
class LeaderboardBoard:
    key: str
    embed: discord.Embed


_DEBOUNCE_TASK: asyncio.Task | None = None
_DEBOUNCE_REASONS: set[str] = set()
_REFRESH_LOCK = asyncio.Lock()


def now_ts() -> int:
    return int(time.time())


def clean_text(value: Any) -> str | None:
    if value is None:
        return None

    text = str(value).strip()
    return text or None


DISPLAY_NAME_ERROR_FALLBACK = "ERROR"


def pruned_player_name(value: Any) -> str:
    return prune_display_name(value, fallback=DISPLAY_NAME_ERROR_FALLBACK)


def configured_channel_id() -> int:
    try:
        return int(LEADERBOARD_CHANNEL_ID or 0)
    except (TypeError, ValueError):
        return 0


def window_days() -> int:
    try:
        return max(1, int(LEADERBOARD_WINDOW_DAYS))
    except (TypeError, ValueError):
        return 50


def window_start_ts() -> int:
    return now_ts() - (window_days() * 24 * 60 * 60)


def configured_delay() -> float:
    try:
        return max(0.0, float(LEADERBOARD_UPDATE_DELAY_SECONDS))
    except (TypeError, ValueError):
        return 5.0


def top_limit() -> int:
    try:
        return max(1, min(15, int(LEADERBOARD_TOP_LIMIT)))
    except (TypeError, ValueError):
        return 5


def command_rows_limit() -> int:
    try:
        return max(1, min(25, int(LEADERBOARD_COMMAND_ROWS)))
    except (TypeError, ValueError):
        return 20


def award_list_limit() -> int:
    try:
        return max(1, min(20, int(LEADERBOARD_AWARD_LIST_LIMIT)))
    except (TypeError, ValueError):
        return 8


def min_wire_samples() -> int:
    try:
        return max(1, int(LEADERBOARD_MIN_WIRE_SAMPLES))
    except (TypeError, ValueError):
        return 3


def recent_op_min_wire_samples() -> int:
    """Minimum GPA attempts for the rolling persistent carrier board only."""
    try:
        return max(1, int(RECENT_OP_LEADERBOARD_MIN_WIRE_SAMPLES))
    except (TypeError, ValueError):
        return min_wire_samples()


def min_survival_ops() -> int:
    try:
        return max(1, int(LEADERBOARD_MIN_SURVIVAL_OPS))
    except (TypeError, ValueError):
        return 5


def state_file_path() -> Path:
    configured = Path(str(LEADERBOARD_STATE_FILE))

    if configured.is_absolute():
        return configured

    return Path.cwd() / configured


def default_state() -> dict[str, Any]:
    return {
        "channel_id": 0,
        "messages": {},
        "content_hashes": {},
        "order": [],
    }


def load_state() -> dict[str, Any]:
    path = state_file_path()

    if not path.exists():
        return default_state()

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default_state()

    if not isinstance(data, dict):
        return default_state()

    state = default_state()
    state["channel_id"] = int(data.get("channel_id") or 0)

    if isinstance(data.get("messages"), dict):
        state["messages"] = {
            str(key): int(value)
            for key, value in data["messages"].items()
            if str(value).isdigit()
        }

    if isinstance(data.get("content_hashes"), dict):
        state["content_hashes"] = {
            str(key): str(value)
            for key, value in data["content_hashes"].items()
        }

    if isinstance(data.get("order"), list):
        state["order"] = [str(value) for value in data["order"]]

    return state


def save_state(state: dict[str, Any]) -> None:
    path = state_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    path.write_text(
        json.dumps(
            {
                "channel_id": int(state.get("channel_id") or 0),
                "messages": state.get("messages", {}),
                "content_hashes": state.get("content_hashes", {}),
                "order": state.get("order", []),
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _player_name_expression() -> str:
    # Persistent boards should use users.display_name only.
    # This is aggregate-safe for grouped leaderboard queries.
    return "MAX(NULLIF(TRIM(u.display_name), ''))"





def rolling_attendance_event_rows(*, since_ts: int, until_ts: int) -> list[dict[str, Any]]:
    """One normalized player/event row for every completed Normal op attended."""
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT
                a.discord_id,
                {_player_name_expression()} AS player_name,
                oe.event_id,
                oe.scheduled_at,
                MAX(
                    CASE
                        WHEN COALESCE(a.combat_deaths, 0) > 0 THEN 1
                        ELSE 0
                    END
                ) AS has_death
            FROM attendance a
            JOIN op_events oe
                ON oe.event_id = a.scheduled_op_id
            JOIN op_templates ot
                ON ot.id = oe.op_template_id
            LEFT JOIN users u
                ON u.discord_id = a.discord_id
            WHERE oe.status = 'Complete'
              AND ot.type = 'Normal'
              AND oe.scheduled_at >= ?
              AND oe.scheduled_at <= ?
              AND a.status IN ('submitted', 'complete')
              AND a.discord_id IS NOT NULL
            GROUP BY a.discord_id, oe.event_id, oe.scheduled_at
            ORDER BY oe.scheduled_at ASC, oe.event_id ASC
            """,
            (int(since_ts), int(until_ts)),
        ).fetchall()

    return [dict(row) for row in rows]


def attendance_leaders(
    *,
    since_ts: int,
    until_ts: int,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    rows = rolling_attendance_event_rows(since_ts=since_ts, until_ts=until_ts)
    totals: dict[str, dict[str, Any]] = {}

    for row in rows:
        discord_id = str(row["discord_id"])
        value = totals.setdefault(
            discord_id,
            {
                "discord_id": discord_id,
                "player_name": pruned_player_name(row["player_name"]),
                "ops": 0,
            },
        )
        value["ops"] += 1

    result_limit = max(1, int(limit)) if limit is not None else top_limit()

    return sorted(
        totals.values(),
        key=lambda row: (-int(row["ops"]), str(row["player_name"]).lower()),
    )[:result_limit]


def survival_leaders(
    *,
    since_ts: int,
    until_ts: int,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    rows = rolling_attendance_event_rows(since_ts=since_ts, until_ts=until_ts)
    totals: dict[str, dict[str, Any]] = {}

    for row in rows:
        discord_id = str(row["discord_id"])
        value = totals.setdefault(
            discord_id,
            {
                "discord_id": discord_id,
                "player_name": pruned_player_name(row["player_name"]),
                "ops": 0,
                "deathless_ops": 0,
            },
        )
        value["ops"] += 1
        if int(row["has_death"] or 0) == 0:
            value["deathless_ops"] += 1

    eligible = [
        {
            **value,
            "survival_rate": (
                float(value["deathless_ops"]) / float(value["ops"])
                if int(value["ops"]) else 0.0
            ),
        }
        for value in totals.values()
        if int(value["ops"]) >= min_survival_ops()
    ]

    result_limit = max(1, int(limit)) if limit is not None else top_limit()

    return sorted(
        eligible,
        key=lambda row: (
            -float(row["survival_rate"]),
            -int(row["ops"]),
            str(row["player_name"]).lower(),
        ),
    )[:result_limit]


def overall_wire_gpa_leaders(
    *,
    since_ts: int,
    until_ts: int,
    limit: int | None = None,
    minimum_attempts: int | None = None,
) -> list[dict[str, Any]]:
    result_limit = max(1, int(limit)) if limit is not None else top_limit()
    gpa_score_expression = sql_gpa_score_expression("a.wires", "a.bolters")
    required_attempts = (
        max(1, int(minimum_attempts))
        if minimum_attempts is not None
        else min_wire_samples()
    )

    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT
                a.discord_id,
                {_player_name_expression()} AS player_name,
                SUM({gpa_score_expression}) AS gpa_total,
                SUM(
                    CASE
                        WHEN a.wires BETWEEN 1 AND 4 THEN 1
                        ELSE 0
                    END
                    + CASE
                        WHEN COALESCE(a.bolters, 0) > 0 THEN a.bolters
                        ELSE 0
                    END
                ) AS attempts
            FROM attendance a
            JOIN op_events oe
                ON oe.event_id = a.scheduled_op_id
            JOIN op_templates ot
                ON ot.id = oe.op_template_id
            LEFT JOIN users u
                ON u.discord_id = a.discord_id
            WHERE oe.status = 'Complete'
              AND ot.type = 'Normal'
              AND oe.scheduled_at >= ?
              AND oe.scheduled_at <= ?
              AND a.status IN ('submitted', 'complete')
              AND a.discord_id IS NOT NULL
              AND a.landing_type = 'Arrested'
              AND (
                    a.wires BETWEEN 1 AND 4
                 OR COALESCE(a.bolters, 0) > 0
              )
            GROUP BY a.discord_id
            HAVING SUM(
                CASE
                    WHEN a.wires BETWEEN 1 AND 4 THEN 1
                    ELSE 0
                END
                + CASE
                    WHEN COALESCE(a.bolters, 0) > 0 THEN a.bolters
                    ELSE 0
                END
            ) >= ?
            ORDER BY
                (CAST(gpa_total AS REAL) / NULLIF(attempts, 0)) DESC,
                attempts DESC,
                a.discord_id ASC
            LIMIT ?
            """,
            (
                int(since_ts),
                int(until_ts),
                required_attempts,
                result_limit,
            ),
        ).fetchall()

    result = [
        {
            **dict(row),
            "wire_gpa": (
                float(row["gpa_total"] or 0.0) / float(row["attempts"])
                if int(row["attempts"] or 0)
                else 0.0
            ),
        }
        for row in rows
    ]

    for row in result:
        row["player_name"] = pruned_player_name(row.get("player_name"))

    return result



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


def configured_airframe_sections() -> list[str]:
    values = LEADERBOARD_AIRFRAME_SECTIONS

    if not isinstance(values, (list, tuple)):
        values = ["F/A-26B", "EF-24G", "F-45A", "AV-42C"]

    result: list[str] = []
    seen: set[str] = set()

    for value in values:
        display = clean_text(value)
        key = normalize_aircraft_key(display)

        if not display or not key or key in seen:
            continue

        seen.add(key)
        result.append(display)

    return result or ["F/A-26B", "EF-24G", "F-45A", "AV-42C"]


def wire_gpa_leaders_for_airframe(
    *,
    airframe: str,
    since_ts: int,
    until_ts: int,
    limit: int | None = None,
    minimum_attempts: int | None = None,
) -> list[dict[str, Any]]:
    """Return ranked GPA rows for one canonical airframe section.

    A caught wire is one GPA attempt using the Greenie score scale. Every
    bolter is another GPA attempt worth 0.0, including bolter-only records.
    """
    requested_key = normalize_aircraft_key(airframe)
    result_limit = max(1, int(limit)) if limit is not None else top_limit()
    gpa_score_expression = sql_gpa_score_expression("a.wires", "a.bolters")
    required_attempts = (
        max(1, int(minimum_attempts))
        if minimum_attempts is not None
        else min_wire_samples()
    )

    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT
                a.aircraft,
                a.discord_id,
                {_player_name_expression()} AS player_name,
                SUM({gpa_score_expression}) AS gpa_total,
                SUM(
                    CASE
                        WHEN a.wires BETWEEN 1 AND 4 THEN 1
                        ELSE 0
                    END
                    + CASE
                        WHEN COALESCE(a.bolters, 0) > 0 THEN a.bolters
                        ELSE 0
                    END
                ) AS attempts
            FROM attendance a
            JOIN op_events oe
                ON oe.event_id = a.scheduled_op_id
            JOIN op_templates ot
                ON ot.id = oe.op_template_id
            LEFT JOIN users u
                ON u.discord_id = a.discord_id
            WHERE oe.status = 'Complete'
              AND ot.type = 'Normal'
              AND oe.scheduled_at >= ?
              AND oe.scheduled_at <= ?
              AND a.status IN ('submitted', 'complete')
              AND a.discord_id IS NOT NULL
              AND a.landing_type = 'Arrested'
              AND (
                    a.wires BETWEEN 1 AND 4
                 OR COALESCE(a.bolters, 0) > 0
              )
              AND NULLIF(TRIM(a.aircraft), '') IS NOT NULL
            GROUP BY a.aircraft, a.discord_id
            """,
            (int(since_ts), int(until_ts)),
        ).fetchall()

    grouped: dict[str, dict[str, Any]] = {}

    for row in rows:
        if normalize_aircraft_key(row["aircraft"]) != requested_key:
            continue

        discord_id = str(row["discord_id"])
        value = grouped.setdefault(
            discord_id,
            {
                "discord_id": discord_id,
                "player_name": pruned_player_name(row["player_name"]),
                "attempts": 0,
                "gpa_total": 0.0,
            },
        )
        value["attempts"] += int(row["attempts"] or 0)
        value["gpa_total"] += float(row["gpa_total"] or 0.0)

    leaders = [
        {
            **row,
            "wire_gpa": (
                float(row["gpa_total"]) / float(row["attempts"])
                if int(row["attempts"])
                else 0.0
            ),
        }
        for row in grouped.values()
        if int(row["attempts"]) >= required_attempts
    ]

    return sorted(
        leaders,
        key=lambda row: (
            -float(row["wire_gpa"]),
            -int(row["attempts"]),
            str(row["player_name"]).casefold(),
        ),
    )[:result_limit]



def short_name(value: Any, *, limit: int = 24) -> str:
    text = clean_text(value) or "Unknown"

    if len(text) <= limit:
        return text

    return text[: max(1, limit - 1)] + "…"


def player_display_name(row: dict[str, Any], *, limit: int = 24) -> str:
    return short_name(pruned_player_name(row.get("player_name")), limit=limit)


def no_data() -> str:
    return "*No qualifying data in this window yet.*"


def code_safe(value: Any) -> str:
    text = clean_text(value) or "Unknown"
    return " ".join(
        text.replace("```", "'''").replace("`", "'").split()
    )


ANSI_RESET = "\u001b[0m"
ANSI_RECENT_HIGHLIGHT = "\u001b[1;34m"  # bold blue


def ansi_code_block(lines: list[str]) -> str:
    if not lines:
        return "```\nNo qualifying data in this window yet.\n```"

    return f"```ansi\n{chr(10).join(lines)[:980]}\n```"


def leaderboard_code_block(
    rows: list[dict[str, Any]],
    formatter,
) -> str:
    """Plain ranked code block for Messages 1 and 2."""
    if not rows:
        return "```\nNo qualifying data in this window yet.\n```"

    lines = [
        f"{index}. {code_safe(formatter(row))}"
        for index, row in enumerate(rows, start=1)
    ]

    return f"```\n{chr(10).join(lines)[:980]}\n```"


def recent_highlight_cutoff_ts() -> int:
    try:
        days = max(1, int(LEADERBOARD_RECENT_HIGHLIGHT_DAYS))
    except (TypeError, ValueError):
        days = 7

    return now_ts() - (days * 24 * 60 * 60)


def highlighted_decoration_code_block(
    rows: list[tuple[str, bool]],
) -> str:
    if not rows:
        return "```\nNo qualifying data in this window yet.\n```"

    has_recent_highlight = any(is_recent for _line, is_recent in rows)

    if not has_recent_highlight:
        return f"```\n{chr(10).join(line for line, _recent in rows)[:980]}\n```"

    lines = [
        (
            f"{ANSI_RECENT_HIGHLIGHT}{line}{ANSI_RESET}"
            if is_recent
            else line
        )
        for line, is_recent in rows
    ]
    return ansi_code_block(lines)



def decoration_intro(category: str, count: int) -> str:
    defaults = DEFAULT_DECORATION_INTROS.get(category, {})
    configured = LEADERBOARD_DECORATION_INTROS

    selected = (
        configured.get(category, {})
        if isinstance(configured, dict)
        else {}
    )

    key = "single" if int(count) == 1 else "plural"
    return (
        clean_text(selected.get(key))
        or clean_text(defaults.get(key))
        or ""
    )



def first_time_normal_op_attenders(
    *,
    since_ts: int,
    until_ts: int,
) -> list[dict[str, Any]]:
    """Pilots whose first completed Normal op falls in this rolling window."""
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT
                a.discord_id,
                {_player_name_expression()} AS player_name,
                oe.event_id,
                oe.scheduled_at
            FROM attendance a
            JOIN op_events oe
                ON oe.event_id = a.scheduled_op_id
            JOIN op_templates ot
                ON ot.id = oe.op_template_id
            LEFT JOIN users u
                ON u.discord_id = a.discord_id
            WHERE oe.status = 'Complete'
              AND ot.type = 'Normal'
              AND a.status IN ('submitted', 'complete')
              AND a.discord_id IS NOT NULL
            GROUP BY a.discord_id, oe.event_id, oe.scheduled_at
            ORDER BY oe.scheduled_at ASC, oe.event_id ASC
            """
        ).fetchall()

    first_events: dict[str, dict[str, Any]] = {}

    for row in rows:
        discord_id = str(row["discord_id"])
        if discord_id not in first_events:
            first_events[discord_id] = dict(row)

    result = [
        row
        for row in first_events.values()
        if int(since_ts) <= int(row["scheduled_at"]) <= int(until_ts)
    ]

    for row in result:
        row["player_name"] = pruned_player_name(row.get("player_name"))

    return sorted(
        result,
        key=lambda row: (
            int(row["scheduled_at"]),
            str(row["player_name"]).casefold(),
        ),
        reverse=True,
    )[:award_list_limit()]


def rolling_window_description(*, since_ts: int, until_ts: int) -> str:
    return (
        f"Rolling **{window_days()}-day** snapshot\n"
        f"<t:{int(since_ts)}:d> through <t:{int(until_ts)}:d>\n"
        "Completed **Normal** operations only."
    )


def performance_embed(*, since_ts: int, until_ts: int) -> discord.Embed:
    attendance = attendance_leaders(since_ts=since_ts, until_ts=until_ts)
    survival = survival_leaders(since_ts=since_ts, until_ts=until_ts)

    embed = discord.Embed(
        title="Operation Performance",
        description=rolling_window_description(
            since_ts=since_ts,
            until_ts=until_ts,
        ),
    )
    embed.add_field(
        name="Most Attended",
        value=leaderboard_code_block(
            attendance,
            lambda row: (
                f"{player_display_name(row)} - {int(row['ops'])} ops"
            ),
        ),
        inline=False,
    )
    embed.add_field(
        name=f"Best Survival Rate (min. {min_survival_ops()} ops)",
        value=leaderboard_code_block(
            survival,
            lambda row: (
                f"{player_display_name(row)} - "
                f"{float(row['survival_rate']) * 100:.0f}% "
                f"({int(row['deathless_ops'])}/{int(row['ops'])})"
            ),
        ),
        inline=False,
    )
    embed.set_footer(text="Automatically recalculated from attendance records.")
    return embed



def carrier_embed(*, since_ts: int, until_ts: int) -> discord.Embed:
    recent_minimum = recent_op_min_wire_samples()
    overall = overall_wire_gpa_leaders(
        since_ts=since_ts,
        until_ts=until_ts,
        minimum_attempts=recent_minimum,
    )

    embed = discord.Embed(
        title="Carrier Operation Leaderboard",
        description=(
            rolling_window_description(since_ts=since_ts, until_ts=until_ts)
            + (
                f"\nMinimum `{recent_minimum}` carrier attempts per ranking "
                "for this rolling board."
            )
        ),
    )
    embed.add_field(
        name="Overall GPA",
        value=leaderboard_code_block(
            overall,
            lambda row: (
                f"{player_display_name(row)} - "
                f"{float(row['wire_gpa']):.2f} GPA "
                f"({int(row['attempts'])} attempts)"
            ),
        ),
        inline=False,
    )

    for airframe in configured_airframe_sections():
        leaders = wire_gpa_leaders_for_airframe(
            airframe=airframe,
            since_ts=since_ts,
            until_ts=until_ts,
            minimum_attempts=recent_minimum,
        )
        embed.add_field(
            name=airframe,
            value=leaderboard_code_block(
                leaders,
                lambda row: (
                    f"{player_display_name(row)} - "
                    f"{float(row['wire_gpa']):.2f} GPA "
                    f"({int(row['attempts'])} attempts)"
                ),
            ),
            inline=False,
        )

    return embed



def award_display_name(award_type: str, details_json: str | None = None) -> str:
    if details_json is not None:
        return manual_award_display_name(award_type, details_json)

    return {
        "ACE": "ACE",
        "GOLDEN_WRENCH": "Golden Wrench",
        "SAFETY_S": "Safety S",
        "BATTLE_E": "Battle E",
    }.get(award_type, award_type.replace("_", " ").title())


def award_lines(
    *,
    award_type: str,
    awards: list[dict[str, Any]],
) -> str:
    lines: list[tuple[str, bool]] = []
    highlight_cutoff = recent_highlight_cutoff_ts()
    should_highlight_recent = award_type in {
        "ACE",
        "GOLDEN_WRENCH",
        "SAFETY_S",
    }

    for row in awards:
        player = player_display_name(row, limit=24)

        if row.get("source_event_id") is not None:
            event_label = (
                f"#{int(row['source_event_id'])} "
                f"{short_name(row.get('op_name'), limit=28)}"
            ).strip()
        else:
            event_label = "Unknown operation"

        if str(row.get("award_source")) == "manual" or award_type == "MANUAL":
            reason = " ".join((clean_text(row.get("notes")) or "").split())
            award_name = (
                clean_text(row.get("award_display_name"))
                or award_display_name(str(row.get("award_type")), clean_text(row.get("details_json")))
            )
            line = (
                f"{player} - {award_name} - {event_label}"
                f" - {reason or 'No reason recorded'}"
            )
        else:
            line = f"{player} - {event_label}"

        earned_at = int(row.get("earned_at") or 0)
        is_recent = should_highlight_recent and earned_at >= highlight_cutoff
        lines.append((code_safe(line), is_recent))

    return highlighted_decoration_code_block(lines)



def decorations_embed(*, since_ts: int, until_ts: int) -> discord.Embed:
    grouped = recent_active_awards(
        since_ts=since_ts,
        limit_per_type=award_list_limit(),
    )
    first_time_attenders = first_time_normal_op_attenders(
        since_ts=since_ts,
        until_ts=until_ts,
    )

    embed = discord.Embed(
        title="Pilots Who Performed Great Feats During Their Operations!",
        description=(
            rolling_window_description(since_ts=since_ts, until_ts=until_ts)
            + "\nAward sections with no recipients are hidden."
        ),
    )

    sections = [
        ("MANUAL", "Manual Awards", grouped.get("MANUAL", [])),
        ("FIRST_TIME", "First Time Op Attenders", first_time_attenders),
        ("ACE", "ACE!", grouped.get("ACE", [])),
        ("GOLDEN_WRENCH", "Golden Wrench", grouped.get("GOLDEN_WRENCH", [])),
        ("SAFETY_S", "Safety S", grouped.get("SAFETY_S", [])),
    ]

    for category, title, rows in sections:
        if not rows:
            continue

        if category == "FIRST_TIME":
            highlight_cutoff = recent_highlight_cutoff_ts()
            names = [
                (
                    code_safe(player_display_name(row, limit=36)),
                    int(row.get("scheduled_at") or 0) >= highlight_cutoff,
                )
                for row in rows
            ]
            content = highlighted_decoration_code_block(names)
        else:
            content = award_lines(
                award_type=category,
                awards=rows,
            )

        intro = decoration_intro(category, len(rows))
        value = f"{intro}\n{content}" if intro else content

        embed.add_field(
            name=title,
            value=value[:1024],
            inline=False,
        )

    if not embed.fields:
        embed.description = (
            rolling_window_description(since_ts=since_ts, until_ts=until_ts)
            + "\nNo new awards or first-time op attenders in this window yet."
        )

    return embed



COMMAND_LEADERBOARD_TYPES = {
    "combat_deaths": {
        "title": "Combat Deaths Leaderboard",
        "field": "Most Combat Deaths",
    },
    "survival_rate": {
        "title": "Survival Rate Leaderboard",
        "field": "Best Survival Rate",
    },
    "ops_attended": {
        "title": "Operations Attended Leaderboard",
        "field": "Most Operations Attended",
    },
    "wire_gpa": {
        "title": "Overall Wire GPA Leaderboard",
        "field": "Overall GPA",
    },
    "fa26_gpa": {
        "title": "F/A-26B GPA Leaderboard",
        "field": "F/A-26B GPA",
        "airframe": "F/A-26B",
    },
    "av42c_gpa": {
        "title": "AV-42C GPA Leaderboard",
        "field": "AV-42C GPA",
        "airframe": "AV-42C",
    },
    "f45_gpa": {
        "title": "F-45A GPA Leaderboard",
        "field": "F-45A GPA",
        "airframe": "F-45A",
    },
    "ef24g_gpa": {
        "title": "EF-24G GPA Leaderboard",
        "field": "EF-24G GPA",
        "airframe": "EF-24G",
    },
    "t55_gpa": {
        "title": "T-55 GPA Leaderboard",
        "field": "T-55 GPA",
        "airframe": "T-55",
    },
}


def combat_death_leaders(
    *,
    since_ts: int,
    until_ts: int,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Rank players by total recorded combat deaths in completed Normal ops."""
    result_limit = max(1, int(limit)) if limit is not None else top_limit()

    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT
                a.discord_id,
                {_player_name_expression()} AS player_name,
                SUM(COALESCE(a.combat_deaths, 0)) AS combat_deaths,
                COUNT(DISTINCT oe.event_id) AS ops
            FROM attendance a
            JOIN op_events oe
                ON oe.event_id = a.scheduled_op_id
            JOIN op_templates ot
                ON ot.id = oe.op_template_id
            LEFT JOIN users u
                ON u.discord_id = a.discord_id
            WHERE oe.status = 'Complete'
              AND ot.type = 'Normal'
              AND oe.scheduled_at >= ?
              AND oe.scheduled_at <= ?
              AND a.status IN ('submitted', 'complete')
              AND a.discord_id IS NOT NULL
            GROUP BY a.discord_id
            HAVING SUM(COALESCE(a.combat_deaths, 0)) > 0
            ORDER BY combat_deaths DESC, ops DESC, a.discord_id ASC
            LIMIT ?
            """,
            (int(since_ts), int(until_ts), result_limit),
        ).fetchall()

    result = [dict(row) for row in rows]

    for row in result:
        row["player_name"] = pruned_player_name(row.get("player_name"))

    return result


def command_window_description(
    *,
    days_back: int,
    since_ts: int,
    until_ts: int,
) -> str:
    return (
        f"Last **{int(days_back)} day{'s' if int(days_back) != 1 else ''}**\n"
        f"<t:{int(since_ts)}:d> through <t:{int(until_ts)}:d>\n"
        "Completed **Normal** operations only."
    )


def build_command_leaderboard_embed(
    *,
    leaderboard_type: str,
    days_back: int,
) -> discord.Embed:
    """Build the on-demand /leaderboard result for one metric."""
    metric = COMMAND_LEADERBOARD_TYPES.get(leaderboard_type)

    if metric is None:
        raise ValueError("Unknown leaderboard type.")

    safe_days = max(1, int(days_back))
    until_ts = now_ts()
    since_ts = until_ts - (safe_days * 24 * 60 * 60)
    result_limit = command_rows_limit()

    if leaderboard_type == "combat_deaths":
        rows = combat_death_leaders(
            since_ts=since_ts,
            until_ts=until_ts,
            limit=result_limit,
        )
        formatter = lambda row: (
            f"{player_display_name(row)} - "
            f"{int(row['combat_deaths'])} deaths "
            f"({int(row['ops'])} ops)"
        )
        field_name = "Most Combat Deaths"

    elif leaderboard_type == "survival_rate":
        rows = survival_leaders(
            since_ts=since_ts,
            until_ts=until_ts,
            limit=result_limit,
        )
        formatter = lambda row: (
            f"{player_display_name(row)} - "
            f"{float(row['survival_rate']) * 100:.0f}% "
            f"({int(row['deathless_ops'])}/{int(row['ops'])})"
        )
        field_name = f"Best Survival Rate (min. {min_survival_ops()} ops)"

    elif leaderboard_type == "ops_attended":
        rows = attendance_leaders(
            since_ts=since_ts,
            until_ts=until_ts,
            limit=result_limit,
        )
        formatter = lambda row: (
            f"{player_display_name(row)} - {int(row['ops'])} ops"
        )
        field_name = "Most Operations Attended"

    elif leaderboard_type == "wire_gpa":
        rows = overall_wire_gpa_leaders(
            since_ts=since_ts,
            until_ts=until_ts,
            limit=result_limit,
        )
        formatter = lambda row: (
            f"{player_display_name(row)} - "
            f"{float(row['wire_gpa']):.2f} GPA "
            f"({int(row['attempts'])} attempts)"
        )
        field_name = f"Overall GPA (min. {min_wire_samples()} attempts)"

    else:
        airframe = str(metric["airframe"])
        rows = wire_gpa_leaders_for_airframe(
            airframe=airframe,
            since_ts=since_ts,
            until_ts=until_ts,
            limit=result_limit,
        )
        formatter = lambda row: (
            f"{player_display_name(row)} - "
            f"{float(row['wire_gpa']):.2f} GPA "
            f"({int(row['attempts'])} attempts)"
        )
        field_name = f"{airframe} GPA (min. {min_wire_samples()} attempts)"

    embed = discord.Embed(
        title=str(metric["title"]),
        description=command_window_description(
            days_back=safe_days,
            since_ts=since_ts,
            until_ts=until_ts,
        ),
    )
    embed.add_field(
        name=field_name,
        value=leaderboard_code_block(rows, formatter),
        inline=False,
    )
    embed.set_footer(
        text=(
            "GPA scale: " + gpa_scale_footer_text()
        )
        if "gpa" in leaderboard_type
        else "Calculated from attendance records."
    )
    return embed


def desired_boards() -> list[LeaderboardBoard]:
    since_ts = window_start_ts()
    until_ts = now_ts()

    return [
        safe_board(
            key="performance",
            title="Operation Performance",
            builder=lambda: performance_embed(since_ts=since_ts, until_ts=until_ts),
        ),
        safe_board(
            key="carrier",
            title="Carrier Operation Leaderboard",
            builder=lambda: carrier_embed(since_ts=since_ts, until_ts=until_ts),
        ),
        safe_board(
            key="decorations",
            title="Decorations/Awards Leaderboard",
            builder=lambda: decorations_embed(since_ts=since_ts, until_ts=until_ts),
        ),
    ]


def leaderboard_error_embed(board_name: str, error: Exception) -> discord.Embed:
    embed = discord.Embed(
        title=f"{board_name} Build Error",
        description=(
            "This persistent leaderboard board could not be built. "
            "The bot posted this message instead of silently failing.\n\n"
            f"```text\n{type(error).__name__}: {str(error)[:900]}\n```"
        ),
        color=discord.Color.red(),
    )
    return embed


def safe_board(
    *,
    key: str,
    title: str,
    builder,
) -> LeaderboardBoard:
    try:
        return LeaderboardBoard(key=key, embed=builder())
    except Exception as error:
        return LeaderboardBoard(key=key, embed=leaderboard_error_embed(title, error))


def embed_hash(embed: discord.Embed) -> str:
    payload = json.dumps(embed.to_dict(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def fetch_leaderboard_channel(bot: discord.Client) -> discord.TextChannel | None:
    channel_id = configured_channel_id()

    if not channel_id:
        return None

    channel = bot.get_channel(channel_id)

    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    return channel if isinstance(channel, discord.TextChannel) else None


async def delete_message_if_present(
    channel: discord.TextChannel,
    message_id: int,
) -> None:
    try:
        message = await channel.fetch_message(int(message_id))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return

    try:
        await message.delete()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return


async def delete_all_tracked_messages(
    *,
    channel: discord.TextChannel,
    state: dict[str, Any],
) -> None:
    for message_id in list(state.get("messages", {}).values()):
        await delete_message_if_present(channel, int(message_id))

    state["messages"] = {}
    state["content_hashes"] = {}
    state["order"] = []


async def send_or_edit_board(
    *,
    channel: discord.TextChannel,
    state: dict[str, Any],
    board: LeaderboardBoard,
) -> None:
    messages: dict[str, int] = state.setdefault("messages", {})
    content_hashes: dict[str, str] = state.setdefault("content_hashes", {})
    desired_hash = embed_hash(board.embed)
    message_id = messages.get(board.key)

    if message_id:
        try:
            message = await channel.fetch_message(int(message_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            message = None

        if message is not None:
            if content_hashes.get(board.key) != desired_hash:
                try:
                    await message.edit(embed=board.embed)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    message = None

            if message is not None:
                content_hashes[board.key] = desired_hash
                return

    message = await channel.send(embed=board.embed)
    messages[board.key] = int(message.id)
    content_hashes[board.key] = desired_hash


async def reconcile_leaderboard(
    bot: discord.Client,
    *,
    force_rebuild: bool = False,
) -> None:
    channel = await fetch_leaderboard_channel(bot)

    if channel is None:
        return

    async with _REFRESH_LOCK:
        state = load_state()

        if int(state.get("channel_id") or 0) != int(channel.id):
            state = default_state()

        if force_rebuild and state.get("messages"):
            await delete_all_tracked_messages(channel=channel, state=state)

        state["channel_id"] = int(channel.id)
        boards = desired_boards()
        active_keys = {board.key for board in boards}

        for key, message_id in list(state.get("messages", {}).items()):
            if key not in active_keys:
                await delete_message_if_present(channel, int(message_id))
                state["messages"].pop(key, None)
                state["content_hashes"].pop(key, None)

        for board in boards:
            await send_or_edit_board(
                channel=channel,
                state=state,
                board=board,
            )

        state["order"] = [board.key for board in boards]
        save_state(state)


def is_tracked_leaderboard_message(
    *,
    channel_id: int,
    message_id: int,
) -> bool:
    configured_id = configured_channel_id()

    if not configured_id or int(channel_id) != configured_id:
        return False

    state = load_state()

    saved_channel_id = int(state.get("channel_id") or 0)
    if saved_channel_id and saved_channel_id != configured_id:
        return False

    tracked_ids = {
        int(value)
        for value in state.get("messages", {}).values()
        if str(value).isdigit()
    }

    return int(message_id) in tracked_ids


async def _debounced_refresh(bot: discord.Client) -> None:
    global _DEBOUNCE_TASK

    try:
        await asyncio.sleep(configured_delay())
        await reconcile_leaderboard(bot)
    except Exception:
        pass
    finally:
        _DEBOUNCE_REASONS.clear()
        _DEBOUNCE_TASK = None


def queue_leaderboard_refresh(
    bot: discord.Client,
    *,
    reason: str = "",
) -> None:
    global _DEBOUNCE_TASK

    if not configured_channel_id():
        return

    if reason:
        _DEBOUNCE_REASONS.add(str(reason))

    if _DEBOUNCE_TASK is not None and not _DEBOUNCE_TASK.done():
        return

    _DEBOUNCE_TASK = asyncio.create_task(_debounced_refresh(bot))


def queue_leaderboard_refresh_now(bot: discord.Client) -> None:
    async def runner() -> None:
        try:
            await reconcile_leaderboard(bot)
        except Exception:
            pass

    asyncio.create_task(runner())


def queue_leaderboard_startup_rebuild(bot: discord.Client) -> None:
    """Recalculate auto awards, delete tracked board posts, then rebuild."""
    async def runner() -> None:
        try:
            from services.reward_service import reconcile_auto_rewards

            reconcile_auto_rewards()
            await reconcile_leaderboard(bot, force_rebuild=True)
        except Exception:
            pass

    asyncio.create_task(runner())
