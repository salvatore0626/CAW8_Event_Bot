from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from database import get_connection
from services.wire_gpa_service import (
    gpa_scale_sentence,
    sql_gpa_score_expression,
)
from services.reward_service import (
    configured_manual_awards,
    manual_award_display_name,
    manual_award_type_key,
)


try:
    from config import LEADERBOARD_MIN_SURVIVAL_OPS
except ImportError:
    LEADERBOARD_MIN_SURVIVAL_OPS = 5

try:
    from config import LEADERBOARD_MIN_WIRE_SAMPLES
except ImportError:
    LEADERBOARD_MIN_WIRE_SAMPLES = 3

try:
    from config import RANK_ROLES
except ImportError:
    RANK_ROLES = []




@dataclass(frozen=True)
class LookupSummary:
    discord_id: str
    display_name: str
    stored_rank: str | None
    highest_qualified_rank: str
    ops_attended: int
    unique_ops_attended: int
    deathless_current_streak: int
    deathless_total: int
    bolterless_current_streak: int
    bolterless_total: int
    safety_s_awards: int
    golden_wrench_awards: int
    ace_awards: int
    battle_e_awards: int
    manual_award_counts: dict[str, int]
    career_gpa: float | None
    career_gpa_attempts: int
    attendance_position: int | None
    wire_gpa_position: int | None
    survival_position: int | None


def clean_text(value: Any) -> str | None:
    if value is None:
        return None

    result = str(value).strip()
    return result or None


def compact_text(value: Any) -> str:
    return " ".join((clean_text(value) or "").split())


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def min_survival_ops() -> int:
    try:
        return max(1, int(LEADERBOARD_MIN_SURVIVAL_OPS))
    except (TypeError, ValueError):
        return 5


def min_gpa_attempts() -> int:
    """Existing config name retained; bolters now count as GPA attempts."""
    try:
        return max(1, int(LEADERBOARD_MIN_WIRE_SAMPLES))
    except (TypeError, ValueError):
        return 3



AUTOMATIC_AWARD_TYPES = {
    "ACE",
    "GOLDEN_WRENCH",
    "SAFETY_S",
}


def manual_award_count_items(counts: dict[str, int]) -> list[tuple[str, int]]:
    items: list[tuple[str, int]] = []
    used: set[str] = set()

    for award_name in configured_manual_awards():
        key = manual_award_type_key(award_name)
        if key in AUTOMATIC_AWARD_TYPES:
            continue

        used.add(key)
        items.append((award_name, safe_int(counts.get(key, 0))))

    for award_type, count in sorted(counts.items()):
        if award_type in used or award_type in AUTOMATIC_AWARD_TYPES:
            continue

        items.append((manual_award_display_name(award_type), safe_int(count)))

    return items


def manual_award_summary_lines(counts: dict[str, int]) -> list[str]:
    return [
        f"**{name}:** {count}"
        for name, count in manual_award_count_items(counts)
    ]

def table_exists(conn, table_name: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        """,
        (str(table_name),),
    ).fetchone()
    return row is not None


def timestamp_text(value: Any) -> str:
    timestamp = safe_int(value)

    if timestamp <= 0:
        return "Unknown"

    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )


def lookup_display_name(
    *,
    discord_id: str,
    fallback_name: str | None = None,
) -> tuple[str, str | None]:
    """Prefer a live Discord display name, then DB display name/history."""
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                u.rank AS stored_rank,
                u.display_name,
                u.discord_username,
                (
                    SELECT a.user_name
                    FROM attendance a
                    WHERE a.discord_id = ?
                      AND NULLIF(TRIM(a.user_name), '') IS NOT NULL
                    ORDER BY
                        COALESCE(a.created_at, 0) DESC,
                        COALESCE(a.logged_at, 0) DESC,
                        a.entry_id DESC
                    LIMIT 1
                ) AS historical_name
            FROM (SELECT ? AS discord_id) requested
            LEFT JOIN users u
                ON u.discord_id = requested.discord_id
            """,
            (str(discord_id), str(discord_id)),
        ).fetchone()

    if row is None:
        return clean_text(fallback_name) or str(discord_id), None

    display_name = (
        clean_text(fallback_name)
        or clean_text(row["display_name"])
        or clean_text(row["historical_name"])
        or clean_text(row["discord_username"])
        or str(discord_id)
    )
    return display_name, clean_text(row["stored_rank"])


def highest_qualified_rank(
    *,
    member_role_ids: set[int] | None,
    stored_rank: str | None,
) -> str:
    """Resolve the highest configured rank currently held by the member."""
    normalized_roles = member_role_ids or set()
    best_rank: str | None = None

    if isinstance(RANK_ROLES, (list, tuple)):
        for row in RANK_ROLES:
            if not isinstance(row, dict):
                continue

            rank_name = clean_text(row.get("rank"))
            role_id = safe_int(row.get("role_id"))

            if rank_name and role_id and role_id in normalized_roles:
                # RANK_ROLES is ordered lowest to highest in the current config.
                best_rank = rank_name

    return best_rank or clean_text(stored_rank) or "Unqualified"


def normal_event_rows(discord_id: str) -> list[dict[str, Any]]:
    """One event-level row for completed Normal ops attended by this player."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                oe.event_id,
                oe.scheduled_at,
                COALESCE(ot.name, a.op_template_name, 'Unknown Operation') AS op_name,
                MAX(CASE WHEN COALESCE(a.combat_deaths, 0) > 0 THEN 1 ELSE 0 END)
                    AS has_death
            FROM attendance a
            JOIN op_events oe
                ON oe.event_id = a.scheduled_op_id
            JOIN op_templates ot
                ON ot.id = oe.op_template_id
            WHERE a.discord_id = ?
              AND oe.status = 'Complete'
              AND ot.type = 'Normal'
              AND a.status IN ('submitted', 'complete')
            GROUP BY oe.event_id, oe.scheduled_at, op_name
            ORDER BY oe.scheduled_at ASC, oe.event_id ASC
            """,
            (str(discord_id),),
        ).fetchall()

    return [dict(row) for row in rows]


def carrier_event_rows(discord_id: str) -> list[dict[str, Any]]:
    """Event-level carrier records for the Safety S-style bolter streak."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                oe.event_id,
                oe.scheduled_at,
                MAX(CASE WHEN COALESCE(a.bolters, 0) > 0 THEN 1 ELSE 0 END)
                    AS has_bolter
            FROM attendance a
            JOIN op_events oe
                ON oe.event_id = a.scheduled_op_id
            JOIN op_templates ot
                ON ot.id = oe.op_template_id
            WHERE a.discord_id = ?
              AND oe.status = 'Complete'
              AND ot.type = 'Normal'
              AND a.status IN ('submitted', 'complete')
              AND a.landing_type = 'Arrested'
              AND a.bolters IS NOT NULL
            GROUP BY oe.event_id, oe.scheduled_at
            ORDER BY oe.scheduled_at ASC, oe.event_id ASC
            """,
            (str(discord_id),),
        ).fetchall()

    return [dict(row) for row in rows]


def clean_streak(rows: list[dict[str, Any]], failure_key: str) -> tuple[int, int]:
    total = 0
    current = 0

    for row in rows:
        if safe_int(row.get(failure_key)) == 0:
            total += 1
            current += 1
        else:
            current = 0

    return current, total


def career_gpa(discord_id: str) -> tuple[float | None, int]:
    """All-time completed-Normal carrier GPA using configured GPA points."""
    gpa_score_expression = sql_gpa_score_expression("a.wires", "a.bolters")

    with get_connection() as conn:
        row = conn.execute(
            f"""
            SELECT
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
            WHERE a.discord_id = ?
              AND oe.status = 'Complete'
              AND ot.type = 'Normal'
              AND a.status IN ('submitted', 'complete')
              AND a.landing_type = 'Arrested'
              AND (
                    a.wires BETWEEN 1 AND 4
                 OR COALESCE(a.bolters, 0) > 0
              )
            """,
            (str(discord_id),),
        ).fetchone()

    attempts = safe_int(row["attempts"]) if row else 0
    gpa_total = float(row["gpa_total"] or 0.0) if row else 0.0
    return ((gpa_total / attempts) if attempts else None), attempts


def active_award_counts(discord_id: str) -> dict[str, int]:
    output = {
        "ACE": 0,
        "GOLDEN_WRENCH": 0,
        "SAFETY_S": 0,
    }

    for award_name in configured_manual_awards():
        output.setdefault(manual_award_type_key(award_name), 0)

    with get_connection() as conn:
        if not table_exists(conn, "player_awards"):
            return output

        rows = conn.execute(
            """
            SELECT award_type, COUNT(*) AS count
            FROM player_awards
            WHERE discord_id = ?
              AND status = 'active'
            GROUP BY award_type
            """,
            (str(discord_id),),
        ).fetchall()

    for row in rows:
        award_type = str(row["award_type"])
        output[award_type] = safe_int(row["count"])

    return output


def global_attendance_rows() -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT
                a.discord_id,
                COALESCE(
                    NULLIF(u.display_name, ''),
                    NULLIF(a.user_name, ''),
                    NULLIF(u.discord_username, ''),
                    a.discord_id
                ) AS player_name,
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
              AND a.status IN ('submitted', 'complete')
              AND a.discord_id IS NOT NULL
            GROUP BY a.discord_id, player_name
            ORDER BY ops DESC, player_name COLLATE NOCASE ASC
            """
        ).fetchall()

    return [dict(row) for row in rows]


def global_survival_rows() -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                a.discord_id,
                COALESCE(
                    NULLIF(u.display_name, ''),
                    NULLIF(a.user_name, ''),
                    NULLIF(u.discord_username, ''),
                    a.discord_id
                ) AS player_name,
                oe.event_id,
                MAX(CASE WHEN COALESCE(a.combat_deaths, 0) > 0 THEN 1 ELSE 0 END)
                    AS has_death
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
            GROUP BY a.discord_id, player_name, oe.event_id
            """
        ).fetchall()

    totals: dict[str, dict[str, Any]] = {}

    for row in rows:
        discord_id = str(row["discord_id"])
        entry = totals.setdefault(
            discord_id,
            {
                "discord_id": discord_id,
                "player_name": clean_text(row["player_name"]) or discord_id,
                "ops": 0,
                "deathless_ops": 0,
            },
        )
        entry["ops"] += 1
        if safe_int(row["has_death"]) == 0:
            entry["deathless_ops"] += 1

    eligible = [
        {
            **entry,
            "survival_rate": (
                float(entry["deathless_ops"]) / float(entry["ops"])
                if entry["ops"]
                else 0.0
            ),
        }
        for entry in totals.values()
        if int(entry["ops"]) >= min_survival_ops()
    ]

    return sorted(
        eligible,
        key=lambda row: (
            -float(row["survival_rate"]),
            -int(row["ops"]),
            str(row["player_name"]).casefold(),
        ),
    )


def global_gpa_rows() -> list[dict[str, Any]]:
    gpa_score_expression = sql_gpa_score_expression("a.wires", "a.bolters")

    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT
                a.discord_id,
                COALESCE(
                    NULLIF(u.display_name, ''),
                    NULLIF(a.user_name, ''),
                    NULLIF(u.discord_username, ''),
                    a.discord_id
                ) AS player_name,
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
              AND a.status IN ('submitted', 'complete')
              AND a.discord_id IS NOT NULL
              AND a.landing_type = 'Arrested'
              AND (
                    a.wires BETWEEN 1 AND 4
                 OR COALESCE(a.bolters, 0) > 0
              )
            GROUP BY a.discord_id, player_name
            """
        ).fetchall()

    eligible = [
        {
            **dict(row),
            "wire_gpa": (
                float(row["gpa_total"] or 0.0) / float(row["attempts"])
                if safe_int(row["attempts"])
                else 0.0
            ),
        }
        for row in rows
        if safe_int(row["attempts"]) >= min_gpa_attempts()
    ]

    return sorted(
        eligible,
        key=lambda row: (
            -float(row["wire_gpa"]),
            -safe_int(row["attempts"]),
            str(row["player_name"]).casefold(),
        ),
    )


def lookup_position(rows: list[dict[str, Any]], discord_id: str) -> int | None:
    for index, row in enumerate(rows, start=1):
        if str(row.get("discord_id")) == str(discord_id):
            return index
    return None


def build_lookup_summary(
    *,
    discord_id: str,
    fallback_name: str | None = None,
    member_role_ids: set[int] | None = None,
) -> LookupSummary:
    display_name, stored_rank = lookup_display_name(
        discord_id=str(discord_id),
        fallback_name=fallback_name,
    )
    events = normal_event_rows(str(discord_id))
    carrier_events = carrier_event_rows(str(discord_id))
    deathless_current, deathless_total = clean_streak(events, "has_death")
    bolterless_current, bolterless_total = clean_streak(
        carrier_events,
        "has_bolter",
    )
    awards = active_award_counts(str(discord_id))
    gpa, attempts = career_gpa(str(discord_id))

    unique_ops = {
        compact_text(row.get("op_name")).casefold()
        for row in events
        if compact_text(row.get("op_name"))
    }

    return LookupSummary(
        discord_id=str(discord_id),
        display_name=display_name,
        stored_rank=stored_rank,
        highest_qualified_rank=highest_qualified_rank(
            member_role_ids=member_role_ids,
            stored_rank=stored_rank,
        ),
        ops_attended=len(events),
        unique_ops_attended=len(unique_ops),
        deathless_current_streak=deathless_current,
        deathless_total=deathless_total,
        bolterless_current_streak=bolterless_current,
        bolterless_total=bolterless_total,
        safety_s_awards=awards.get("SAFETY_S", 0),
        golden_wrench_awards=awards.get("GOLDEN_WRENCH", 0),
        ace_awards=awards.get("ACE", 0),
        battle_e_awards=awards.get("BATTLE_E", 0),
        manual_award_counts=awards,
        career_gpa=gpa,
        career_gpa_attempts=attempts,
        attendance_position=lookup_position(
            global_attendance_rows(),
            str(discord_id),
        ),
        wire_gpa_position=lookup_position(
            global_gpa_rows(),
            str(discord_id),
        ),
        survival_position=lookup_position(
            global_survival_rows(),
            str(discord_id),
        ),
    )


def attendance_export_rows(discord_id: str) -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                a.*,
                oe.event_id,
                oe.scheduled_at,
                oe.status AS operation_status,
                COALESCE(ot.name, a.op_template_name, 'Unknown Operation')
                    AS operation_name,
                COALESCE(ot.type, 'Unknown') AS operation_type
            FROM attendance a
            LEFT JOIN op_events oe
                ON oe.event_id = a.scheduled_op_id
            LEFT JOIN op_templates ot
                ON ot.id = oe.op_template_id
            WHERE a.discord_id = ?
            ORDER BY
                COALESCE(
                    oe.scheduled_at,
                    a.created_at,
                    a.logged_at,
                    0
                ) ASC,
                a.entry_id ASC
            """,
            (str(discord_id),),
        ).fetchall()

    return [dict(row) for row in rows]


def award_export_rows(discord_id: str) -> list[dict[str, Any]]:
    with get_connection() as conn:
        if not table_exists(conn, "player_awards"):
            return []

        rows = conn.execute(
            """
            SELECT
                pa.*,
                ot.name AS operation_name,
                oe.scheduled_at AS operation_scheduled_at
            FROM player_awards pa
            LEFT JOIN op_events oe
                ON oe.event_id = pa.source_event_id
            LEFT JOIN op_templates ot
                ON ot.id = oe.op_template_id
            WHERE pa.discord_id = ?
            ORDER BY pa.earned_at ASC, pa.award_id ASC
            """,
            (str(discord_id),),
        ).fetchall()

    return [dict(row) for row in rows]


def format_nullable(value: Any) -> str:
    text = clean_text(value)
    return text if text is not None else "—"


def build_lookup_export(
    *,
    summary: LookupSummary,
) -> str:
    """Create a complete portable text export for a user's records."""
    attendance = attendance_export_rows(summary.discord_id)
    awards = award_export_rows(summary.discord_id)
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        "BOOKKEEPER USER LOOKUP EXPORT",
        "=" * 78,
        f"Generated: {created_at}",
        f"Display Name: {summary.display_name}",
        f"Discord ID: {summary.discord_id}",
        "",
        "CAREER SUMMARY",
        "-" * 78,
        f"Highest Qualified Rank: {summary.highest_qualified_rank}",
        f"Ops Attended: {summary.ops_attended}",
        f"Unique Ops Attended: {summary.unique_ops_attended}",
        (
            "Ops Without Death: "
            f"Current Streak {summary.deathless_current_streak} | "
            f"Total {summary.deathless_total}"
        ),
        (
            "Ops Without Bolter: "
            f"Current Streak {summary.bolterless_current_streak} | "
            f"Total {summary.bolterless_total}"
        ),
        (
            "Career Wire GPA: "
            f"{summary.career_gpa:.3f}"
            if summary.career_gpa is not None
            else "Career Wire GPA: —"
        ),
        f"Career GPA Attempts: {summary.career_gpa_attempts}",
        (
            "Leaderboard Positions: "
            f"Attendance {summary.attendance_position or '—'} | "
            f"Wire GPA {summary.wire_gpa_position or '—'} | "
            f"Survival {summary.survival_position or '—'}"
        ),
        "",
        "ACTIVE AWARD COUNTS",
        "-" * 78,
        f"ACE: {summary.ace_awards}",
        f"Golden Wrench: {summary.golden_wrench_awards}",
        f"Safety S: {summary.safety_s_awards}",
        *[
            f"{name}: {count}"
            for name, count in manual_award_count_items(summary.manual_award_counts)
        ],
        "",
        f"AWARD HISTORY ({len(awards)})",
        "-" * 78,
    ]

    if awards:
        for award in awards:
            event_label = (
                f"#{award['source_event_id']} "
                f"{format_nullable(award.get('operation_name'))}"
                if award.get("source_event_id") is not None
                else "No linked operation"
            )
            lines.extend(
                [
                    (
                        f"[{format_nullable(award.get('status')).upper()}] "
                        f"{manual_award_display_name(award.get('award_type'), award.get('details_json'))} | "
                        f"Earned {timestamp_text(award.get('earned_at'))} | "
                        f"{event_label} | "
                        f"Source: {format_nullable(award.get('award_source'))}"
                    ),
                ]
            )

            if clean_text(award.get("notes")):
                lines.append(f"  Notes: {compact_text(award.get('notes'))}")

            if award.get("revoked_at"):
                lines.append(
                    f"  Revoked: {timestamp_text(award.get('revoked_at'))}"
                )

            lines.append("")
    else:
        lines.append("No award history recorded.")
        lines.append("")

    lines.extend(
        [
            f"ATTENDANCE HISTORY ({len(attendance)})",
            "-" * 78,
            (
                "Columns: Date | Event | Operation [Type/Status] | Slot | "
                "Aircraft | Landing | Wires | Bolters | Deaths | Source"
            ),
        ]
    )

    if attendance:
        for row in attendance:
            event_label = (
                f"#{row['event_id']}"
                if row.get("event_id") is not None
                else "No Event"
            )
            date_value = (
                row.get("scheduled_at")
                or row.get("created_at")
                or row.get("logged_at")
            )
            lines.append(
                " | ".join(
                    [
                        timestamp_text(date_value),
                        event_label,
                        (
                            f"{format_nullable(row.get('operation_name'))} "
                            f"[{format_nullable(row.get('operation_type'))}/"
                            f"{format_nullable(row.get('operation_status'))}]"
                        ),
                        format_nullable(row.get("slot")),
                        format_nullable(row.get("aircraft")),
                        format_nullable(row.get("landing_type")),
                        format_nullable(row.get("wires")),
                        format_nullable(row.get("bolters")),
                        format_nullable(row.get("combat_deaths")),
                        format_nullable(row.get("type")),
                    ]
                )
            )

            notes = [
                ("Op remarks", row.get("op_remarks")),
                ("FL remarks", row.get("fl_remarks")),
                ("Notes", row.get("note_remarks")),
            ]

            for label, value in notes:
                if clean_text(value):
                    lines.append(f"  {label}: {compact_text(value)}")
    else:
        lines.append("No attendance records found for this Discord ID.")

    lines.extend(
        [
            "",
            "NOTES",
            "-" * 78,
            (
                "Career/summary statistics use completed Normal operations "
                "with submitted or complete attendance rows."
            ),
            (
                gpa_scale_sentence()
            ),
            (
                "Attendance export includes every stored attendance row for "
                "the Discord ID, including legacy/manual rows and non-Normal "
                "operation types when present."
            ),
        ]
    )

    return "\n".join(lines) + "\n"
