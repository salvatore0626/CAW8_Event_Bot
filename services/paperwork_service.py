from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from database import get_connection


def now_ts() -> int:
    return int(time.time())


# UI rating scale:
# 0 = N/A
# 1 = Red
# 2 = Orange
# 3 = Yellow
# 4 = Green
#
# Database note:
# Your qual_log table CHECK constraints only allow 1,2,3,4,5 or NULL.
# To avoid breaking that table, UI rating 0 / N/A is saved as NULL.
RATING_VALUES = {0, 1, 2, 3, 4}


@dataclass
class PaperworkDraft:
    applicant_discord_id: str
    applicant_username: str
    instructor_discord_id: str
    instructor_username: str

    request_qual_id: int | None = None

    ag_rating: int | None = None
    ag_remarks: str | None = None

    aa_rating: int | None = None
    aa_remarks: str | None = None

    formation_rating: int | None = None
    formation_remarks: str | None = None

    tank_rating: int | None = None
    tank_remarks: str | None = None

    case1_rating: int | None = None
    case1_remarks: str | None = None

    carrier_rating: int | None = None
    carrier_remarks: str | None = None

    # Vibes is scored on the Verdict page.
    vibe_rating: int | None = None
    vibe_remarks: str | None = None

    passed: bool | None = None
    verdict_remarks: str | None = None

    def validate(self) -> list[str]:
        errors: list[str] = []

        if self.ag_rating not in RATING_VALUES:
            errors.append("Air to Ground rating is missing.")

        if self.aa_rating not in RATING_VALUES:
            errors.append("Air to Air rating is missing.")

        if self.formation_rating not in RATING_VALUES:
            errors.append("Formation rating is missing.")

        if self.tank_rating not in RATING_VALUES:
            errors.append("Tanker rating is missing.")

        if self.case1_rating not in RATING_VALUES:
            errors.append("Case 1 rating is missing.")

        if self.carrier_rating not in RATING_VALUES:
            errors.append("Carrier rating is missing.")

        if self.vibe_rating not in {1, 2, 3, 4}:
            errors.append("Vibe rating is missing.")

        if self.passed is None:
            errors.append("Pass/Fail result is missing.")

        return errors


@dataclass
class PreviousQualAttempt:
    id: int

    ag_rating: int | None = None
    aa_rating: int | None = None

    formation_rating: int | None = None
    tank_rating: int | None = None

    case1_rating: int | None = None
    carrier_rating: int | None = None

    vibe_rating: int | None = None

    passed: bool | None = None

    created_at: int | None = None


def row_get(row: Any, name: str, default: Any = None) -> Any:
    try:
        if name in row.keys():
            return row[name]
    except Exception:
        pass

    return default


def int_or_none(value: Any) -> int | None:
    if value is None:
        return None

    try:
        return int(value)
    except Exception:
        return None


def float_or_none(value: Any) -> float | None:
    if value is None:
        return None

    try:
        return float(value)
    except Exception:
        return None


def bool_or_none(value: Any) -> bool | None:
    if value is None:
        return None

    try:
        return bool(int(value))
    except Exception:
        return None


def get_previous_qual_attempts_for_user(
    applicant_discord_id: str,
) -> list[PreviousQualAttempt]:
    """
    Pull previous qual_log attempts for the applicant.

    Used by /paperwork to:
    - display past attempt rows
    - auto-default any previously-green item to N/A
    """
    with get_connection() as conn:
        table_row = conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name = 'qual_log'
            """
        ).fetchone()

        if table_row is None:
            return []

        rows = conn.execute(
            """
            SELECT
                id,
                ag_rating,
                aa_rating,
                formation_rating,
                tank_rating,
                case1_rating,
                carrier_rating,
                vibe_rating,
                pass,
                created_at
            FROM qual_log
            WHERE applicant_discord_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (str(applicant_discord_id),),
        ).fetchall()

    attempts: list[PreviousQualAttempt] = []

    for row in rows:
        attempts.append(
            PreviousQualAttempt(
                id=int(row["id"]),
                ag_rating=int_or_none(row_get(row, "ag_rating")),
                aa_rating=int_or_none(row_get(row, "aa_rating")),
                formation_rating=int_or_none(row_get(row, "formation_rating")),
                tank_rating=int_or_none(row_get(row, "tank_rating")),
                case1_rating=int_or_none(row_get(row, "case1_rating")),
                carrier_rating=int_or_none(row_get(row, "carrier_rating")),
                vibe_rating=int_or_none(row_get(row, "vibe_rating")),
                passed=bool_or_none(row_get(row, "pass")),
                created_at=int_or_none(row_get(row, "created_at")),
            )
        )

    return attempts


def has_previous_green(
    attempts: list[PreviousQualAttempt],
    field_name: str,
) -> bool:
    for attempt in attempts:
        if getattr(attempt, field_name, None) == 4:
            return True

    return False


def apply_previous_green_defaults(
    draft: PaperworkDraft,
    attempts: list[PreviousQualAttempt],
) -> None:
    """
    If the applicant has ever scored green on an item before, default that
    item to 0 / N/A for the new paperwork attempt.

    UI 0 saves to NULL in qual_log, so it stays database-safe with the current
    CHECK constraints.
    """
    for field_name in [
        "ag_rating",
        "aa_rating",
        "formation_rating",
        "tank_rating",
        "case1_rating",
        "carrier_rating",
    ]:
        if has_previous_green(attempts, field_name):
            setattr(draft, field_name, 0)



@dataclass
class PendingQualApplicant:
    request_id: int
    discord_id: str
    discord_username: str | None = None
    min_requirements: str | None = None
    of_age: bool | None = None
    hours: float | None = None
    created_at: int | None = None
    updated_at: int | None = None


def get_pending_qual_applicant(
    discord_id: str,
) -> PendingQualApplicant | None:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                id,
                discord_id,
                discord_username,
                min_requirements,
                of_age,
                hours,
                created_at,
                updated_at
            FROM request_qual
            WHERE discord_id = ?
              AND status = 'pending'
            ORDER BY updated_at DESC, created_at DESC, id DESC
            LIMIT 1
            """,
            (str(discord_id),),
        ).fetchone()

    if row is None:
        return None

    return PendingQualApplicant(
        request_id=int(row["id"]),
        discord_id=str(row["discord_id"]),
        discord_username=row["discord_username"],
        min_requirements=row["min_requirements"],
        of_age=bool(int(row["of_age"])) if row["of_age"] is not None else None,
        hours=float_or_none(row["hours"]),
        created_at=int_or_none(row["created_at"]),
        updated_at=int_or_none(row["updated_at"]),
    )


def search_pending_qual_applicants(
    search_text: str = "",
    limit: int = 25,
) -> list[PendingQualApplicant]:
    search_text = str(search_text or "").strip()

    params: list[Any] = []

    where = "status = 'pending'"

    if search_text:
        where += """
            AND (
                discord_username LIKE ?
                OR discord_id LIKE ?
            )
        """
        like_value = f"%{search_text}%"
        params.extend([like_value, like_value])

    params.append(int(limit))

    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT
                id,
                discord_id,
                discord_username,
                min_requirements,
                of_age,
                hours,
                created_at,
                updated_at
            FROM request_qual
            WHERE {where}
            ORDER BY updated_at DESC, created_at DESC, id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()

    return [
        PendingQualApplicant(
            request_id=int(row["id"]),
            discord_id=str(row["discord_id"]),
            discord_username=row["discord_username"],
            min_requirements=row["min_requirements"],
            of_age=bool(int(row["of_age"])) if row["of_age"] is not None else None,
            hours=float_or_none(row["hours"]),
            created_at=int_or_none(row["created_at"]),
            updated_at=int_or_none(row["updated_at"]),
        )
        for row in rows
    ]




def clean_optional_text(value: str | None) -> str | None:
    if value is None:
        return None

    value = str(value).strip()

    return value or None


def db_rating(value: int | None) -> int | None:
    """
    Converts UI ratings to database-safe ratings.

    UI 0 = N/A, stored as NULL because the current qual_log CHECK constraints
    do not allow 0.
    """
    if value is None:
        return None

    value = int(value)

    if value == 0:
        return None

    return value


def get_table_columns(table_name: str) -> set[str]:
    with get_connection() as conn:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()

    return {row["name"] for row in rows}


def find_pending_request_id_for_applicant(applicant_discord_id: str) -> int | None:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT id
            FROM request_qual
            WHERE discord_id = ?
              AND status = 'pending'
            ORDER BY updated_at DESC, created_at DESC, id DESC
            LIMIT 1
            """,
            (applicant_discord_id,),
        ).fetchone()

    if row is None:
        return None

    return int(row["id"])


def add_if_column_exists(
    data: dict[str, Any],
    columns: set[str],
    column_name: str,
    value: Any,
) -> None:
    if column_name in columns:
        data[column_name] = value


def create_qual_log_record(draft: PaperworkDraft) -> int:
    errors = draft.validate()

    if errors:
        raise ValueError("; ".join(errors))

    if draft.request_qual_id is None:
        draft.request_qual_id = find_pending_request_id_for_applicant(
            draft.applicant_discord_id
        )

    columns = get_table_columns("qual_log")

    if not columns:
        raise RuntimeError("qual_log table does not exist.")

    ts = now_ts()

    data: dict[str, Any] = {}

    add_if_column_exists(data, columns, "request_qual_id", draft.request_qual_id)

    add_if_column_exists(data, columns, "instructor_discord_id", draft.instructor_discord_id)
    add_if_column_exists(data, columns, "instructor_username", draft.instructor_username)

    add_if_column_exists(data, columns, "applicant_discord_id", draft.applicant_discord_id)
    add_if_column_exists(data, columns, "applicant_username", draft.applicant_username)

    # Landing
    add_if_column_exists(data, columns, "carrier_rating", db_rating(draft.carrier_rating))
    add_if_column_exists(data, columns, "carrier_remarks", clean_optional_text(draft.carrier_remarks))

    add_if_column_exists(data, columns, "case1_rating", db_rating(draft.case1_rating))
    add_if_column_exists(data, columns, "case1_remarks", clean_optional_text(draft.case1_remarks))

    # Flying
    add_if_column_exists(data, columns, "formation_rating", db_rating(draft.formation_rating))
    add_if_column_exists(data, columns, "formation_remarks", clean_optional_text(draft.formation_remarks))

    add_if_column_exists(data, columns, "tank_rating", db_rating(draft.tank_rating))
    add_if_column_exists(data, columns, "tank_remarks", clean_optional_text(draft.tank_remarks))

    # Weapons
    add_if_column_exists(data, columns, "ag_rating", db_rating(draft.ag_rating))
    add_if_column_exists(data, columns, "ag_remarks", clean_optional_text(draft.ag_remarks))

    add_if_column_exists(data, columns, "aa_rating", db_rating(draft.aa_rating))
    add_if_column_exists(data, columns, "aa_remarks", clean_optional_text(draft.aa_remarks))

    # Vibes: not currently scored in this page flow.
    add_if_column_exists(data, columns, "vibe_rating", db_rating(draft.vibe_rating))
    add_if_column_exists(data, columns, "vibe_remarks", clean_optional_text(draft.vibe_remarks))
    add_if_column_exists(data, columns, "vibes_rating", db_rating(draft.vibe_rating))
    add_if_column_exists(data, columns, "vibes_remarks", clean_optional_text(draft.vibe_remarks))
    add_if_column_exists(data, columns, "vibe", None)

    # Verdict
    add_if_column_exists(data, columns, "pass", 1 if draft.passed else 0)
    add_if_column_exists(data, columns, "remarks", clean_optional_text(draft.verdict_remarks))

    add_if_column_exists(data, columns, "created_at", ts)
    add_if_column_exists(data, columns, "updated_at", ts)

    if "applicant_discord_id" not in data:
        raise RuntimeError("qual_log table is missing applicant_discord_id.")

    if "instructor_discord_id" not in data:
        raise RuntimeError("qual_log table is missing instructor_discord_id.")

    if "created_at" not in data or "updated_at" not in data:
        raise RuntimeError("qual_log table is missing created_at or updated_at.")

    column_names = list(data.keys())
    placeholders = ", ".join("?" for _ in column_names)
    sql_columns = ", ".join(column_names)
    values = [data[column_name] for column_name in column_names]

    with get_connection() as conn:
        cur = conn.execute(
            f"""
            INSERT INTO qual_log (
                {sql_columns}
            )
            VALUES (
                {placeholders}
            )
            """,
            values,
        )

    return int(cur.lastrowid)


def mark_request_completed_if_passed(
    applicant_discord_id: str,
    request_qual_id: int | None,
) -> None:
    ts = now_ts()

    with get_connection() as conn:
        if request_qual_id is not None:
            conn.execute(
                """
                UPDATE request_qual
                SET status = 'completed',
                    updated_at = ?
                WHERE id = ?
                  AND discord_id = ?
                  AND status = 'pending'
                """,
                (ts, request_qual_id, applicant_discord_id),
            )
        else:
            conn.execute(
                """
                UPDATE request_qual
                SET status = 'completed',
                    updated_at = ?
                WHERE discord_id = ?
                  AND status = 'pending'
                """,
                (ts, applicant_discord_id),
            )
