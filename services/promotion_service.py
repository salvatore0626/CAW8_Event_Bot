from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from database import get_connection

from services.schedule_service import clean_text, now_ts

try:
    from config import RANK_ROLES
except ImportError:
    RANK_ROLES = [
        {"rank": "Recruit", "role_id": 0},
        {"rank": "ENS", "role_id": 0},
        {"rank": "LTJG", "role_id": 0},
        {"rank": "LT", "role_id": 0},
        {"rank": "LCDR", "role_id": 0},
        {"rank": "CDR", "role_id": 0},
        {"rank": "CAPT", "role_id": 0},
    ]

try:
    from config import PROMOTION_MANAGED_RANKS
except ImportError:
    PROMOTION_MANAGED_RANKS = [
        "Recruit",
        "ENS",
        "LTJG",
        "LT",
        "LCDR",
        "CDR",
        "CAPT",
    ]

try:
    from config import PROMOTION_REQUIREMENTS
except ImportError:
    PROMOTION_REQUIREMENTS = {
        "ENS": {"total_ops": 1, "unique_ops": 1},
        "LTJG": {"total_ops": 3, "unique_ops": 2},
        "LT": {"total_ops": 6, "unique_ops": 3},
        "LCDR": {"total_ops": 10, "unique_ops": 5},
        "CDR": {"total_ops": 16, "unique_ops": 8},
        "CAPT": {"total_ops": 24, "unique_ops": 12},
    }


RANK_ALIASES = {
    "Ensign": "ENS",
    "ENS": "ENS",
    "LTJG": "LTJG",
    "LT": "LT",
    "LCDR": "LCDR",
    "CDR": "CDR",
    "CAPT": "CAPT",
    "XO": "XO",
    "CO": "CO",
    "DCAG": "DCAG",
    "CAG": "CAG",
    "ADM": "ADM",
    "RADM": "RADM",
    "SECNAV": "SECNAV",
    "Recruit": "Recruit",
}


@dataclass
class PromotionCandidate:
    discord_id: str
    username: str
    current_rank: str
    next_rank: str
    total_ops: int
    unique_ops: int
    required_total_ops: int
    required_unique_ops: int
    max_rank: str | None


@dataclass
class PromotionBlock:
    discord_id: str
    username: str
    current_rank: str
    next_rank: str
    total_ops: int
    unique_ops: int
    max_rank: str


def normalize_rank(rank: str | None) -> str:
    cleaned = clean_text(rank) or "Recruit"

    return RANK_ALIASES.get(cleaned, cleaned)


def configured_rank_names() -> list[str]:
    names: list[str] = []

    for rank in PROMOTION_MANAGED_RANKS:
        normalized = normalize_rank(str(rank))
        if normalized not in names:
            names.append(normalized)

    for item in RANK_ROLES:
        if not isinstance(item, dict):
            continue

        normalized = normalize_rank(str(item.get("rank") or ""))
        if normalized and normalized not in names:
            names.append(normalized)

    return names


RANK_ORDER = configured_rank_names()


def rank_index(rank: str | None) -> int | None:
    normalized = normalize_rank(rank)

    try:
        return RANK_ORDER.index(normalized)
    except ValueError:
        return None


def rank_role_id(rank: str | None) -> int | None:
    normalized = normalize_rank(rank)

    for item in RANK_ROLES:
        if not isinstance(item, dict):
            continue

        item_rank = normalize_rank(str(item.get("rank") or ""))

        if item_rank != normalized:
            continue

        try:
            role_id = int(item.get("role_id") or 0)
        except (TypeError, ValueError):
            role_id = 0

        return role_id or None

    return None


def all_rank_role_ids() -> set[int]:
    role_ids: set[int] = set()

    for item in RANK_ROLES:
        if not isinstance(item, dict):
            continue

        try:
            role_id = int(item.get("role_id") or 0)
        except (TypeError, ValueError):
            role_id = 0

        if role_id:
            role_ids.add(role_id)

    return role_ids


def managed_rank_names() -> list[str]:
    names: list[str] = []

    for rank in PROMOTION_MANAGED_RANKS:
        normalized = normalize_rank(rank)

        if normalized not in names:
            names.append(normalized)

    return names


def is_managed_rank(rank: str | None) -> bool:
    return normalize_rank(rank) in managed_rank_names()


def highest_rank_from_role_ids(role_ids: set[int]) -> str | None:
    """Return the highest configured rank represented by these Discord roles."""
    matched_rank: str | None = None
    matched_index: int | None = None

    for item in RANK_ROLES:
        if not isinstance(item, dict):
            continue

        rank = normalize_rank(str(item.get("rank") or ""))

        try:
            role_id = int(item.get("role_id") or 0)
        except (TypeError, ValueError):
            role_id = 0

        if not rank or not role_id or role_id not in role_ids:
            continue

        idx = rank_index(rank)

        if idx is None:
            continue

        if matched_index is None or idx > matched_index:
            matched_rank = rank
            matched_index = idx

    return matched_rank


def next_managed_rank(current_rank: str | None) -> str | None:
    normalized = normalize_rank(current_rank)
    managed = managed_rank_names()

    # Important safety rule:
    # Do not silently treat CAG/DCAG/XO/CO/etc. as Recruit. If a rank is not
    # part of the managed promotion ladder, the automated board must skip them.
    if normalized not in managed:
        return None

    try:
        idx = managed.index(normalized)
    except ValueError:
        return None

    next_idx = idx + 1

    if next_idx >= len(managed):
        return None

    next_rank = managed[next_idx]

    if next_rank not in PROMOTION_REQUIREMENTS:
        return None

    return next_rank


def promotion_requirement_for(rank: str) -> dict[str, int]:
    requirement = PROMOTION_REQUIREMENTS.get(rank, {})

    return {
        "total_ops": int(requirement.get("total_ops", 0)),
        "unique_ops": int(requirement.get("unique_ops", 0)),
    }


def ensure_do_not_promote_table() -> None:
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS do_not_promote (
                discord_id TEXT PRIMARY KEY,
                reason TEXT,
                max_rank TEXT,
                performed_by_id TEXT,
                created_at INTEGER NOT NULL
            )
            """
        )


def ensure_user_record(
    *,
    discord_id: str,
    discord_username: str | None,
    display_name: str | None,
) -> None:
    ts = now_ts()

    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT discord_id
            FROM users
            WHERE discord_id = ?
            LIMIT 1
            """,
            (clean_text(discord_id),),
        ).fetchone()

        if row is None:
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
                """,
                (
                    clean_text(discord_id),
                    clean_text(discord_username),
                    clean_text(display_name),
                    ts,
                    ts,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE users
                SET discord_username = COALESCE(?, discord_username),
                    display_name = COALESCE(?, display_name),
                    updated_at = ?
                WHERE discord_id = ?
                """,
                (
                    clean_text(discord_username),
                    clean_text(display_name),
                    ts,
                    clean_text(discord_id),
                ),
            )


def update_user_rank(discord_id: str, new_rank: str) -> None:
    ts = now_ts()

    with get_connection() as conn:
        conn.execute(
            """
            UPDATE users
            SET rank = ?,
                updated_at = ?
            WHERE discord_id = ?
            """,
            (
                normalize_rank(new_rank),
                ts,
                clean_text(discord_id),
            ),
        )


def set_do_not_promote(
    *,
    discord_id: str,
    max_rank: str,
    performed_by_id: str | None,
    reason: str | None = None,
) -> str:
    ensure_do_not_promote_table()

    normalized_max_rank = normalize_rank(max_rank)

    if normalized_max_rank not in RANK_ORDER:
        raise ValueError(f"Unknown rank: {max_rank}")

    ts = now_ts()

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO do_not_promote (
                discord_id,
                reason,
                max_rank,
                performed_by_id,
                created_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET
                reason = excluded.reason,
                max_rank = excluded.max_rank,
                performed_by_id = excluded.performed_by_id,
                created_at = excluded.created_at
            """,
            (
                clean_text(discord_id),
                clean_text(reason),
                normalized_max_rank,
                clean_text(performed_by_id),
                ts,
            ),
        )

    return normalized_max_rank


def remove_do_not_promote(discord_id: str) -> None:
    ensure_do_not_promote_table()

    with get_connection() as conn:
        conn.execute(
            """
            DELETE FROM do_not_promote
            WHERE discord_id = ?
            """,
            (clean_text(discord_id),),
        )


def eligible_rank_choices(current: str = "") -> list[str]:
    current_lower = current.lower().strip()

    if not current_lower:
        return RANK_ORDER[:25]

    return [
        rank
        for rank in RANK_ORDER
        if current_lower in rank.lower()
    ][:25]


def load_attendance_stats() -> dict[str, dict[str, int]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                a.discord_id AS discord_id,
                COUNT(DISTINCT COALESCE(a.scheduled_op_id, a.entry_id)) AS total_ops,
                COUNT(
                    DISTINCT COALESCE(
                        a.op_template_name,
                        ot.name,
                        'event:' || COALESCE(a.scheduled_op_id, a.entry_id)
                    )
                ) AS unique_ops
            FROM attendance a
            LEFT JOIN op_events oe ON oe.event_id = a.scheduled_op_id
            LEFT JOIN op_templates ot ON ot.id = oe.op_template_id
            WHERE a.discord_id IS NOT NULL
              AND TRIM(a.discord_id) != ''
              AND a.status IN ('submitted', 'complete')
            GROUP BY a.discord_id
            """
        ).fetchall()

    return {
        str(row["discord_id"]): {
            "total_ops": int(row["total_ops"] or 0),
            "unique_ops": int(row["unique_ops"] or 0),
        }
        for row in rows
    }


def load_do_not_promote_map() -> dict[str, str]:
    ensure_do_not_promote_table()

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT discord_id, max_rank
            FROM do_not_promote
            WHERE max_rank IS NOT NULL
              AND TRIM(max_rank) != ''
            """
        ).fetchall()

    return {
        str(row["discord_id"]): normalize_rank(row["max_rank"])
        for row in rows
    }


def username_from_user_row(row: Any) -> str:
    return (
        clean_text(row["discord_username"])
        or clean_text(row["display_name"])
        or str(row["discord_id"])
    )


def promotion_allowed_by_max_rank(next_rank: str, max_rank: str | None) -> bool:
    if not max_rank:
        return True

    next_idx = rank_index(next_rank)
    max_idx = rank_index(max_rank)

    if next_idx is None or max_idx is None:
        return True

    return next_idx <= max_idx


def candidate_from_user_row(
    *,
    row: Any,
    stats_by_user: dict[str, dict[str, int]],
    blocked_by_user: dict[str, str],
) -> tuple[PromotionCandidate | None, PromotionBlock | None, str | None]:
    discord_id = str(row["discord_id"])
    current_rank = normalize_rank(row["rank"])
    next_rank = next_managed_rank(current_rank)

    if next_rank is None:
        return None, None, (
            f"Current rank {current_rank} is not eligible for automatic promotion "
            "or is already at the highest managed rank."
        )

    requirement = promotion_requirement_for(next_rank)
    stats = stats_by_user.get(discord_id, {"total_ops": 0, "unique_ops": 0})

    total_ops = int(stats["total_ops"])
    unique_ops = int(stats["unique_ops"])

    if total_ops < requirement["total_ops"] or unique_ops < requirement["unique_ops"]:
        return (
            None,
            None,
            (
                f"Not eligible for {next_rank}. "
                f"Needs {requirement['total_ops']} total ops and {requirement['unique_ops']} unique ops. "
                f"Current: {total_ops} total, {unique_ops} unique."
            ),
        )

    max_rank = blocked_by_user.get(discord_id)
    username = username_from_user_row(row)

    if not promotion_allowed_by_max_rank(next_rank, max_rank):
        return (
            None,
            PromotionBlock(
                discord_id=discord_id,
                username=username,
                current_rank=current_rank,
                next_rank=next_rank,
                total_ops=total_ops,
                unique_ops=unique_ops,
                max_rank=max_rank or "",
            ),
            f"User is capped at {max_rank} by do_not_promote.",
        )

    return (
        PromotionCandidate(
            discord_id=discord_id,
            username=username,
            current_rank=current_rank,
            next_rank=next_rank,
            total_ops=total_ops,
            unique_ops=unique_ops,
            required_total_ops=requirement["total_ops"],
            required_unique_ops=requirement["unique_ops"],
            max_rank=max_rank,
        ),
        None,
        None,
    )


def find_promotion_candidates() -> tuple[list[PromotionCandidate], list[PromotionBlock]]:
    stats_by_user = load_attendance_stats()
    blocked_by_user = load_do_not_promote_map()

    if not stats_by_user:
        return [], []

    with get_connection() as conn:
        placeholders = ",".join("?" for _ in stats_by_user)
        rows = conn.execute(
            f"""
            SELECT discord_id, discord_username, display_name, rank, status
            FROM users
            WHERE discord_id IN ({placeholders})
              AND status = 'Active'
            """,
            tuple(stats_by_user.keys()),
        ).fetchall()

    candidates: list[PromotionCandidate] = []
    blocked: list[PromotionBlock] = []

    for row in rows:
        candidate, blocked_candidate, _reason = candidate_from_user_row(
            row=row,
            stats_by_user=stats_by_user,
            blocked_by_user=blocked_by_user,
        )

        if candidate is not None:
            candidates.append(candidate)

        if blocked_candidate is not None:
            blocked.append(blocked_candidate)

    candidates.sort(
        key=lambda item: (
            rank_index(item.next_rank) if rank_index(item.next_rank) is not None else 999,
            -item.total_ops,
            item.username.lower(),
        )
    )
    blocked.sort(key=lambda item: (item.username.lower(), item.next_rank))

    return candidates, blocked


def find_promotion_candidate_for_user(discord_id: str) -> tuple[PromotionCandidate | None, str | None]:
    stats_by_user = load_attendance_stats()
    blocked_by_user = load_do_not_promote_map()

    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT discord_id, discord_username, display_name, rank, status
            FROM users
            WHERE discord_id = ?
            LIMIT 1
            """,
            (clean_text(discord_id),),
        ).fetchone()

    if row is None:
        return None, "User is not in the users table yet."

    if row["status"] != "Active":
        return None, f"User status is {row['status']}; only Active users are checked."

    candidate, _blocked, reason = candidate_from_user_row(
        row=row,
        stats_by_user=stats_by_user,
        blocked_by_user=blocked_by_user,
    )

    return candidate, reason
