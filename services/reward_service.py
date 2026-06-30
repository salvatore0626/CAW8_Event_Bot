from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Any

from database import get_connection
from services.display_name_service import prune_display_name


AUTO_AWARD_TYPES = ("ACE", "GOLDEN_WRENCH", "SAFETY_S")

_RECONCILE_TASK: asyncio.Task | None = None
_RECONCILE_REASONS: set[str] = set()


@dataclass(frozen=True)
class AwardCandidate:
    award_type: str
    award_key: str
    discord_id: str
    source_event_id: int | None
    source_attendance_entry_id: int | None
    earned_at: int
    details: dict[str, Any]


@dataclass(frozen=True)
class AwardReconciliationResult:
    granted: int
    reactivated: int
    revoked: int
    unchanged: int


@dataclass(frozen=True)
class AwardRecord:
    award_id: int
    discord_id: str
    award_type: str
    award_source: str
    award_key: str
    source_event_id: int | None
    source_attendance_entry_id: int | None
    earned_at: int
    awarded_at: int
    status: str
    revoked_at: int | None
    granted_by_id: str | None
    notes: str | None
    details_json: str | None


def now_ts() -> int:
    return int(time.time())


def clean_text(value: Any) -> str | None:
    if value is None:
        return None

    text = str(value).strip()
    return text or None



def configured_manual_awards() -> list[str]:
    try:
        from config import MANUAL_AWARDS
    except Exception:
        MANUAL_AWARDS = ["Battle E"]

    awards: list[str] = []
    seen: set[str] = set()

    for raw_award in MANUAL_AWARDS or []:
        name = clean_text(raw_award)
        if not name:
            continue

        key = manual_award_type_key(name)
        if key in seen:
            continue

        seen.add(key)
        awards.append(name)

    if not awards:
        awards.append("Battle E")

    return awards


def manual_award_type_key(award_name: Any) -> str:
    text = clean_text(award_name) or ""
    text = text.upper()
    text = re.sub(r"[^A-Z0-9]+", "_", text).strip("_")

    if not text:
        text = "MANUAL_AWARD"

    return text[:64]


def manual_award_display_name(award_type: Any, details_json: str | None = None) -> str:
    details: dict[str, Any] = {}

    if details_json:
        try:
            loaded = json.loads(details_json)
            if isinstance(loaded, dict):
                details = loaded
        except Exception:
            details = {}

    configured = {
        manual_award_type_key(name): name
        for name in configured_manual_awards()
    }

    key = manual_award_type_key(award_type)
    return (
        clean_text(details.get("award_name"))
        or configured.get(key)
        or str(award_type).replace("_", " ").title()
    )


def manual_award_choices() -> list[tuple[str, str]]:
    return [
        (name, manual_award_type_key(name))
        for name in configured_manual_awards()
    ]



def configured_reward_update_delay() -> float:
    try:
        from config import REWARD_RECONCILE_DELAY_SECONDS

        return max(0.0, float(REWARD_RECONCILE_DELAY_SECONDS))
    except Exception:
        return 5.0


def ensure_reward_schema() -> None:
    with get_connection() as conn:
        existing = conn.execute(
            """
            SELECT sql
            FROM sqlite_master
            WHERE type = 'table'
              AND name = 'player_awards'
            """
        ).fetchone()

        if existing is not None and "CHECK (award_type IN" in str(existing["sql"]):
            conn.execute("ALTER TABLE player_awards RENAME TO player_awards_old")
            conn.execute(
                """
                CREATE TABLE player_awards (
                    award_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    discord_id TEXT NOT NULL,
                    award_type TEXT NOT NULL,
                    award_source TEXT NOT NULL
                        CHECK (award_source IN ('auto', 'manual')),
                    award_key TEXT NOT NULL UNIQUE,
                    source_event_id INTEGER,
                    source_attendance_entry_id INTEGER,
                    earned_at INTEGER NOT NULL,
                    awarded_at INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'revoked')),
                    revoked_at INTEGER,
                    granted_by_id TEXT,
                    notes TEXT,
                    details_json TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO player_awards (
                    award_id,
                    discord_id,
                    award_type,
                    award_source,
                    award_key,
                    source_event_id,
                    source_attendance_entry_id,
                    earned_at,
                    awarded_at,
                    status,
                    revoked_at,
                    granted_by_id,
                    notes,
                    details_json,
                    created_at,
                    updated_at
                )
                SELECT
                    award_id,
                    discord_id,
                    award_type,
                    award_source,
                    award_key,
                    source_event_id,
                    source_attendance_entry_id,
                    earned_at,
                    awarded_at,
                    status,
                    revoked_at,
                    granted_by_id,
                    notes,
                    details_json,
                    created_at,
                    updated_at
                FROM player_awards_old
                """
            )
            conn.execute("DROP TABLE player_awards_old")
        else:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS player_awards (
                    award_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    discord_id TEXT NOT NULL,
                    award_type TEXT NOT NULL,
                    award_source TEXT NOT NULL
                        CHECK (award_source IN ('auto', 'manual')),
                    award_key TEXT NOT NULL UNIQUE,
                    source_event_id INTEGER,
                    source_attendance_entry_id INTEGER,
                    earned_at INTEGER NOT NULL,
                    awarded_at INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'revoked')),
                    revoked_at INTEGER,
                    granted_by_id TEXT,
                    notes TEXT,
                    details_json TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_player_awards_player_type_date
            ON player_awards(discord_id, award_type, earned_at DESC)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_player_awards_status_date
            ON player_awards(status, earned_at DESC)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_player_awards_event
            ON player_awards(source_event_id)
            """
        )



def _row_to_award(row: Any) -> AwardRecord:
    return AwardRecord(
        award_id=int(row["award_id"]),
        discord_id=str(row["discord_id"]),
        award_type=str(row["award_type"]),
        award_source=str(row["award_source"]),
        award_key=str(row["award_key"]),
        source_event_id=(
            int(row["source_event_id"])
            if row["source_event_id"] is not None
            else None
        ),
        source_attendance_entry_id=(
            int(row["source_attendance_entry_id"])
            if row["source_attendance_entry_id"] is not None
            else None
        ),
        earned_at=int(row["earned_at"]),
        awarded_at=int(row["awarded_at"]),
        status=str(row["status"]),
        revoked_at=int(row["revoked_at"]) if row["revoked_at"] is not None else None,
        granted_by_id=clean_text(row["granted_by_id"]),
        notes=clean_text(row["notes"]),
        details_json=clean_text(row["details_json"]),
    )


def normal_completed_events() -> list[dict[str, Any]]:
    """All completed Normal events in chronological order for auto-award rules."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                oe.event_id,
                oe.scheduled_at,
                ot.name AS op_name
            FROM op_events oe
            JOIN op_templates ot
                ON ot.id = oe.op_template_id
            WHERE oe.status = 'Complete'
              AND ot.type = 'Normal'
            ORDER BY oe.scheduled_at ASC, oe.event_id ASC
            """
        ).fetchall()

    return [dict(row) for row in rows]


def _auto_ace_candidates() -> list[AwardCandidate]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                a.entry_id,
                a.discord_id,
                a.slot,
                a.aircraft,
                oe.event_id,
                oe.scheduled_at
            FROM attendance a
            JOIN op_events oe
                ON oe.event_id = a.scheduled_op_id
            JOIN op_templates ot
                ON ot.id = oe.op_template_id
            WHERE oe.status = 'Complete'
              AND ot.type = 'Normal'
              AND a.status IN ('submitted', 'complete')
              AND a.discord_id IS NOT NULL
              AND a.landing_type = 'Arrested'
              AND a.wires = 3
              AND a.bolters = 0
              AND a.combat_deaths = 0
            ORDER BY oe.scheduled_at ASC, oe.event_id ASC, a.entry_id ASC
            """
        ).fetchall()

    candidates: list[AwardCandidate] = []

    for row in rows:
        entry_id = int(row["entry_id"])
        candidates.append(
            AwardCandidate(
                award_type="ACE",
                award_key=f"ace:attendance:{entry_id}",
                discord_id=str(row["discord_id"]),
                source_event_id=int(row["event_id"]),
                source_attendance_entry_id=entry_id,
                earned_at=int(row["scheduled_at"]),
                details={
                    "slot": clean_text(row["slot"]),
                    "aircraft": clean_text(row["aircraft"]),
                    "wire": 3,
                    "bolters": 0,
                    "combat_deaths": 0,
                },
            )
        )

    return candidates


def _auto_golden_wrench_candidates() -> list[AwardCandidate]:
    """Award at 5, 10, 15... attended Normal completed ops without a death.

    The streak continues after each award. Only a later attended Normal op with
    one or more combat deaths resets it.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                a.discord_id,
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
            WHERE oe.status = 'Complete'
              AND ot.type = 'Normal'
              AND a.status IN ('submitted', 'complete')
              AND a.discord_id IS NOT NULL
            GROUP BY a.discord_id, oe.event_id, oe.scheduled_at
            ORDER BY a.discord_id ASC, oe.scheduled_at ASC, oe.event_id ASC
            """
        ).fetchall()

    candidates: list[AwardCandidate] = []
    current_user: str | None = None
    streak = 0

    for row in rows:
        discord_id = str(row["discord_id"])

        if discord_id != current_user:
            current_user = discord_id
            streak = 0

        has_death = int(row["has_death"] or 0) > 0

        if has_death:
            streak = 0
            continue

        streak += 1

        if streak % 5 != 0:
            continue

        event_id = int(row["event_id"])
        candidates.append(
            AwardCandidate(
                award_type="GOLDEN_WRENCH",
                award_key=(
                    f"golden_wrench:player:{discord_id}:"
                    f"event:{event_id}:milestone:{streak}"
                ),
                discord_id=discord_id,
                source_event_id=event_id,
                source_attendance_entry_id=None,
                earned_at=int(row["scheduled_at"]),
                details={
                    "death_free_operation_streak": streak,
                    "milestone": streak,
                },
            )
        )

    return candidates


def _auto_safety_s_candidates() -> list[AwardCandidate]:
    """Award at 5, 10, 15... clean arrested recoveries in Normal completed ops.

    Only Arrested attendance records participate. Every other landing type is
    ignored completely. Any Arrested landing with one or more bolters resets the
    arrested-landing streak.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                a.entry_id,
                a.discord_id,
                a.slot,
                a.aircraft,
                a.bolters,
                oe.event_id,
                oe.scheduled_at
            FROM attendance a
            JOIN op_events oe
                ON oe.event_id = a.scheduled_op_id
            JOIN op_templates ot
                ON ot.id = oe.op_template_id
            WHERE oe.status = 'Complete'
              AND ot.type = 'Normal'
              AND a.status IN ('submitted', 'complete')
              AND a.discord_id IS NOT NULL
              AND a.landing_type = 'Arrested'
              AND a.bolters IS NOT NULL
            ORDER BY a.discord_id ASC, oe.scheduled_at ASC, oe.event_id ASC, a.entry_id ASC
            """
        ).fetchall()

    candidates: list[AwardCandidate] = []
    current_user: str | None = None
    streak = 0

    for row in rows:
        discord_id = str(row["discord_id"])

        if discord_id != current_user:
            current_user = discord_id
            streak = 0

        bolters = int(row["bolters"] or 0)

        if bolters > 0:
            streak = 0
            continue

        streak += 1

        if streak % 5 != 0:
            continue

        entry_id = int(row["entry_id"])
        candidates.append(
            AwardCandidate(
                award_type="SAFETY_S",
                award_key=(
                    f"safety_s:player:{discord_id}:"
                    f"attendance:{entry_id}:milestone:{streak}"
                ),
                discord_id=discord_id,
                source_event_id=int(row["event_id"]),
                source_attendance_entry_id=entry_id,
                earned_at=int(row["scheduled_at"]),
                details={
                    "clean_arrested_landing_streak": streak,
                    "milestone": streak,
                    "slot": clean_text(row["slot"]),
                    "aircraft": clean_text(row["aircraft"]),
                    "bolters": 0,
                },
            )
        )

    return candidates


def calculate_auto_award_candidates() -> list[AwardCandidate]:
    ensure_reward_schema()

    candidates = [
        *_auto_ace_candidates(),
        *_auto_golden_wrench_candidates(),
        *_auto_safety_s_candidates(),
    ]

    return candidates


def reconcile_auto_rewards() -> AwardReconciliationResult:
    """Recalculate all automatic awards from completed Normal-op records.

    This is intentionally full-history reconciliation. It keeps streak awards
    correct after staff repair an old attendance entry.
    """
    ensure_reward_schema()
    desired = {
        candidate.award_key: candidate
        for candidate in calculate_auto_award_candidates()
    }
    timestamp = now_ts()
    granted = 0
    reactivated = 0
    revoked = 0
    unchanged = 0

    with get_connection() as conn:
        existing_rows = conn.execute(
            """
            SELECT *
            FROM player_awards
            WHERE award_source = 'auto'
              AND award_type IN ('ACE', 'GOLDEN_WRENCH', 'SAFETY_S')
            """
        ).fetchall()
        existing = {
            str(row["award_key"]): _row_to_award(row)
            for row in existing_rows
        }

        for key, candidate in desired.items():
            existing_award = existing.get(key)
            details_json = json.dumps(candidate.details, sort_keys=True)

            if existing_award is None:
                conn.execute(
                    """
                    INSERT INTO player_awards (
                        discord_id,
                        award_type,
                        award_source,
                        award_key,
                        source_event_id,
                        source_attendance_entry_id,
                        earned_at,
                        awarded_at,
                        status,
                        revoked_at,
                        granted_by_id,
                        notes,
                        details_json,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, 'auto', ?, ?, ?, ?, ?, 'active',
                            NULL, NULL, NULL, ?, ?, ?)
                    """,
                    (
                        candidate.discord_id,
                        candidate.award_type,
                        candidate.award_key,
                        candidate.source_event_id,
                        candidate.source_attendance_entry_id,
                        candidate.earned_at,
                        timestamp,
                        details_json,
                        timestamp,
                        timestamp,
                    ),
                )
                granted += 1
                continue

            if existing_award.status == "revoked":
                conn.execute(
                    """
                    UPDATE player_awards
                    SET discord_id = ?,
                        award_type = ?,
                        source_event_id = ?,
                        source_attendance_entry_id = ?,
                        earned_at = ?,
                        status = 'active',
                        revoked_at = NULL,
                        details_json = ?,
                        updated_at = ?
                    WHERE award_id = ?
                    """,
                    (
                        candidate.discord_id,
                        candidate.award_type,
                        candidate.source_event_id,
                        candidate.source_attendance_entry_id,
                        candidate.earned_at,
                        details_json,
                        timestamp,
                        existing_award.award_id,
                    ),
                )
                reactivated += 1
                continue

            conn.execute(
                """
                UPDATE player_awards
                SET discord_id = ?,
                    award_type = ?,
                    source_event_id = ?,
                    source_attendance_entry_id = ?,
                    earned_at = ?,
                    details_json = ?,
                    updated_at = ?
                WHERE award_id = ?
                """,
                (
                    candidate.discord_id,
                    candidate.award_type,
                    candidate.source_event_id,
                    candidate.source_attendance_entry_id,
                    candidate.earned_at,
                    details_json,
                    timestamp,
                    existing_award.award_id,
                ),
            )
            unchanged += 1

        desired_keys = set(desired)

        for key, award in existing.items():
            if award.status != "active" or key in desired_keys:
                continue

            conn.execute(
                """
                UPDATE player_awards
                SET status = 'revoked',
                    revoked_at = ?,
                    updated_at = ?
                WHERE award_id = ?
                """,
                (
                    timestamp,
                    timestamp,
                    award.award_id,
                ),
            )
            revoked += 1

    return AwardReconciliationResult(
        granted=granted,
        reactivated=reactivated,
        revoked=revoked,
        unchanged=unchanged,
    )


def completed_normal_event(event_id: int) -> dict[str, Any] | None:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                oe.event_id,
                oe.scheduled_at,
                ot.name AS op_name,
                ot.type AS op_type,
                oe.status
            FROM op_events oe
            JOIN op_templates ot
                ON ot.id = oe.op_template_id
            WHERE oe.event_id = ?
              AND oe.status = 'Complete'
              AND ot.type = 'Normal'
            LIMIT 1
            """,
            (int(event_id),),
        ).fetchone()

    return dict(row) if row is not None else None


def grant_manual_award(
    *,
    discord_id: str,
    award_name: str,
    granted_by_id: str,
    notes: str | None = None,
    source_event_id: int | None = None,
) -> AwardRecord:
    """Grant one configured manual award to a player.

    Manual awards are config driven by config.MANUAL_AWARDS. A player can have
    one active row per configured manual award name. Re-granting the same award
    to the same player restores/updates the same row rather than creating a
    duplicate.
    """
    ensure_reward_schema()

    did = clean_text(discord_id)
    grantor = clean_text(granted_by_id)
    award_type = manual_award_type_key(award_name)
    configured = {
        manual_award_type_key(name): name
        for name in configured_manual_awards()
    }
    display_name = configured.get(award_type)

    if not did:
        raise ValueError("A player is required.")

    if not grantor:
        raise ValueError("The staff member granting the award is required.")

    if not display_name:
        raise ValueError("That manual award is not configured in config.MANUAL_AWARDS.")

    event = None
    if source_event_id is not None:
        event = completed_normal_event(int(source_event_id))
        if event is None:
            raise ValueError("Source operation must be a completed Normal op.")

    timestamp = now_ts()
    earned_at = int(event["scheduled_at"]) if event is not None else timestamp
    award_key = f"manual_award:player:{did}:award:{award_type}"
    if source_event_id is not None:
        award_key += f":event:{int(source_event_id)}"

    details_json = json.dumps(
        {
            "award_name": display_name,
            "award_type": award_type,
        },
        sort_keys=True,
    )

    with get_connection() as conn:
        existing = conn.execute(
            """
            SELECT *
            FROM player_awards
            WHERE award_key = ?
            LIMIT 1
            """,
            (award_key,),
        ).fetchone()

        if existing is None:
            cur = conn.execute(
                """
                INSERT INTO player_awards (
                    discord_id,
                    award_type,
                    award_source,
                    award_key,
                    source_event_id,
                    source_attendance_entry_id,
                    earned_at,
                    awarded_at,
                    status,
                    revoked_at,
                    granted_by_id,
                    notes,
                    details_json,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, 'manual', ?, ?, NULL, ?, ?,
                        'active', NULL, ?, ?, ?, ?, ?)
                """,
                (
                    did,
                    award_type,
                    award_key,
                    int(source_event_id) if source_event_id is not None else None,
                    earned_at,
                    timestamp,
                    grantor,
                    clean_text(notes),
                    details_json,
                    timestamp,
                    timestamp,
                ),
            )
            award_id = int(cur.lastrowid)
        else:
            award_id = int(existing["award_id"])
            conn.execute(
                """
                UPDATE player_awards
                SET discord_id = ?,
                    award_type = ?,
                    award_source = 'manual',
                    source_event_id = ?,
                    source_attendance_entry_id = NULL,
                    earned_at = ?,
                    awarded_at = ?,
                    status = 'active',
                    revoked_at = NULL,
                    granted_by_id = ?,
                    notes = ?,
                    details_json = ?,
                    updated_at = ?
                WHERE award_id = ?
                """,
                (
                    did,
                    award_type,
                    int(source_event_id) if source_event_id is not None else None,
                    earned_at,
                    timestamp,
                    grantor,
                    clean_text(notes),
                    details_json,
                    timestamp,
                    award_id,
                ),
            )

        row = conn.execute(
            """
            SELECT *
            FROM player_awards
            WHERE award_id = ?
            LIMIT 1
            """,
            (award_id,),
        ).fetchone()

    if row is None:
        raise RuntimeError("Manual award was not saved.")

    return _row_to_award(row)


def grant_battle_e(
    *,
    discord_id: str,
    source_event_id: int,
    granted_by_id: str,
    notes: str,
) -> AwardRecord:
    """Backward-compatible Battle E wrapper."""
    return grant_manual_award(
        discord_id=discord_id,
        award_name="Battle E",
        source_event_id=source_event_id,
        granted_by_id=granted_by_id,
        notes=notes,
    )


def revoke_manual_award(
    *,
    award_id: int,
    revoked_by_id: str,
    reason: str | None = None,
) -> AwardRecord:
    ensure_reward_schema()
    timestamp = now_ts()

    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM player_awards
            WHERE award_id = ?
              AND award_source = 'manual'
            LIMIT 1
            """,
            (int(award_id),),
        ).fetchone()

        if row is None:
            raise ValueError("That manual award does not exist.")

        old = _row_to_award(row)
        new_notes = clean_text(reason) or old.notes

        conn.execute(
            """
            UPDATE player_awards
            SET status = 'revoked',
                revoked_at = ?,
                granted_by_id = ?,
                notes = ?,
                updated_at = ?
            WHERE award_id = ?
            """,
            (
                timestamp,
                clean_text(revoked_by_id),
                new_notes,
                timestamp,
                int(award_id),
            ),
        )

        updated = conn.execute(
            """
            SELECT *
            FROM player_awards
            WHERE award_id = ?
            LIMIT 1
            """,
            (int(award_id),),
        ).fetchone()

    if updated is None:
        raise RuntimeError("Manual award revoke was not saved.")

    return _row_to_award(updated)


def revoke_manual_battle_e(
    *,
    award_id: int,
    revoked_by_id: str,
    reason: str | None = None,
) -> AwardRecord:
    """Backward-compatible Battle E revoke wrapper."""
    return revoke_manual_award(
        award_id=award_id,
        revoked_by_id=revoked_by_id,
        reason=reason,
    )


def active_manual_awards_for_player(
    discord_id: str,
    *,
    limit: int = 25,
) -> list[AwardRecord]:
    ensure_reward_schema()

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM player_awards
            WHERE discord_id = ?
              AND status = 'active'
              AND award_source = 'manual'
            ORDER BY earned_at DESC, award_id DESC
            LIMIT ?
            """,
            (str(discord_id), max(1, int(limit))),
        ).fetchall()

    return [_row_to_award(row) for row in rows]




def active_awards_for_player(
    discord_id: str,
    *,
    limit: int = 50,
) -> list[AwardRecord]:
    ensure_reward_schema()

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM player_awards
            WHERE discord_id = ?
              AND status = 'active'
            ORDER BY earned_at DESC, award_id DESC
            LIMIT ?
            """,
            (str(discord_id), max(1, int(limit))),
        ).fetchall()

    return [_row_to_award(row) for row in rows]


def recent_active_awards(
    *,
    since_ts: int,
    limit_per_type: int = 12,
) -> dict[str, list[dict[str, Any]]]:
    """Active awards earned in the board's rolling date window."""
    ensure_reward_schema()

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                pa.*,
                NULLIF(TRIM(u.display_name), '') AS player_name,
                ot.name AS op_name
            FROM player_awards pa
            LEFT JOIN users u
                ON u.discord_id = pa.discord_id
            LEFT JOIN op_events oe
                ON oe.event_id = pa.source_event_id
            LEFT JOIN op_templates ot
                ON ot.id = oe.op_template_id
            WHERE pa.status = 'active'
              AND pa.earned_at >= ?
            ORDER BY pa.earned_at DESC, pa.award_id DESC
            """,
            (int(since_ts),),
        ).fetchall()

    grouped: dict[str, list[dict[str, Any]]] = {
        "MANUAL": [],
        "ACE": [],
        "GOLDEN_WRENCH": [],
        "SAFETY_S": [],
    }

    for row in rows:
        row_dict = dict(row)
        award_type = str(row["award_type"])
        group_key = "MANUAL" if str(row["award_source"]) == "manual" else award_type

        if group_key not in grouped:
            continue

        if len(grouped[group_key]) >= int(limit_per_type):
            continue

        row_dict["player_name"] = prune_display_name(
            row_dict.get("player_name"),
            fallback="ERROR",
        )

        if group_key == "MANUAL":
            row_dict["award_display_name"] = manual_award_display_name(
                award_type,
                clean_text(row_dict.get("details_json")),
            )

        grouped[group_key].append(row_dict)

    return grouped


def completed_normal_events_for_autocomplete(
    *,
    query: str | None = None,
    limit: int = 25,
) -> list[dict[str, Any]]:
    text = clean_text(query) or ""
    params: list[Any] = []

    query_sql = ""
    if text:
        like = f"%{text}%"
        query_sql = """
          AND (
                CAST(oe.event_id AS TEXT) LIKE ?
             OR ot.name LIKE ?
          )
        """
        params.extend([like, like])

    params.append(max(1, int(limit)))

    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT
                oe.event_id,
                oe.scheduled_at,
                ot.name AS op_name
            FROM op_events oe
            JOIN op_templates ot
                ON ot.id = oe.op_template_id
            WHERE oe.status = 'Complete'
              AND ot.type = 'Normal'
            {query_sql}
            ORDER BY oe.scheduled_at DESC, oe.event_id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()

    return [dict(row) for row in rows]


async def _run_debounced_reconciliation(bot: Any) -> None:
    global _RECONCILE_TASK

    try:
        await asyncio.sleep(configured_reward_update_delay())
        reconcile_auto_rewards()

        # Imported here to avoid a reward <-> leaderboard module import cycle.
        from services.leaderboard_service import queue_leaderboard_refresh

        queue_leaderboard_refresh(
            bot,
            reason="automatic reward reconciliation",
        )
    except Exception:
        # Award/board errors must never turn a normal command callback into a
        # Discord interaction failure.
        pass
    finally:
        _RECONCILE_REASONS.clear()
        _RECONCILE_TASK = None


def queue_reward_reconciliation(
    bot: Any,
    *,
    reason: str = "",
) -> None:
    """Coalesce completion/edit-triggered full award recalculations."""
    global _RECONCILE_TASK

    if reason:
        _RECONCILE_REASONS.add(str(reason))

    if _RECONCILE_TASK is not None and not _RECONCILE_TASK.done():
        return

    _RECONCILE_TASK = asyncio.create_task(_run_debounced_reconciliation(bot))


async def reconcile_rewards_and_refresh_leaderboard_now(bot: Any) -> AwardReconciliationResult:
    """Immediate startup/manual full calculation followed by board refresh."""
    result = reconcile_auto_rewards()

    from services.leaderboard_service import reconcile_leaderboard

    await reconcile_leaderboard(bot)

    return result
