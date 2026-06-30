from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import discord

from config import MIN_VTOL_HOURS
from database import get_connection


def now_ts() -> int:
    return int(time.time())


def compute_min_requirements(hours: float | None, of_age: bool | None) -> str:
    """
    Stores Yes/No in request_qual.min_requirements.

    Yes only when:
    - applicant is age 13+
    - applicant has at least MIN_VTOL_HOURS
    """
    if of_age is not True:
        return "No"

    if hours is None:
        return "No"

    try:
        return "Yes" if float(hours) >= float(MIN_VTOL_HOURS) else "No"
    except Exception:
        return "No"


@dataclass
class QualificationRequestDraft:
    discord_id: str
    discord_username: str

    # Stored as Yes/No at insert time.
    min_requirements: Optional[str] = None

    of_age: Optional[bool] = None
    hours: Optional[float] = None

    preferred_aircraft: Optional[str] = None
    timezone: Optional[str] = None

    availability_start: Optional[str] = None
    availability_end: Optional[str] = None
    dotw: Optional[str] = None

    # Applicant remarks are no longer collected.
    # This column is reserved for instructor Deny/MIA remarks.
    remarks: Optional[str] = None

    referral: Optional[str] = None

    def validate_page_one(self) -> list[str]:
        errors: list[str] = []

        if self.hours is None:
            errors.append("VTOL hours are missing.")

        if self.of_age is None:
            errors.append("Age answer is missing.")

        if not self.preferred_aircraft:
            errors.append("Preferred aircraft is missing.")

        return errors

    def validate_page_two(self) -> list[str]:
        errors: list[str] = []

        if not self.timezone:
            errors.append("Timezone is missing.")

        if not self.dotw:
            errors.append("Available days are missing.")

        if not self.availability_start:
            errors.append("Availability start time is missing.")

        if not self.availability_end:
            errors.append("Availability end time is missing.")

        return errors

    def validate_page_three(self) -> list[str]:
        errors: list[str] = []

        if not self.referral:
            errors.append("Referral source is missing.")

        return errors

    def validate(self) -> list[str]:
        return (
            self.validate_page_one()
            + self.validate_page_two()
            + self.validate_page_three()
        )


@dataclass
class ExistingQualificationRequest:
    id: int
    discord_id: str
    discord_username: str | None
    status: str
    timezone: str | None
    availability_start: str | None
    availability_end: str | None
    dotw: str | None
    times_pinged: int
    created_at: int
    updated_at: int


@dataclass
class AvailabilityUpdateDraft:
    request_id: int
    discord_id: str
    timezone: str | None = None
    availability_start: str | None = None
    availability_end: str | None = None
    dotw: str | None = None

    def validate(self) -> list[str]:
        errors: list[str] = []

        if not self.timezone:
            errors.append("Timezone is missing.")

        if not self.dotw:
            errors.append("Available days are missing.")

        if not self.availability_start:
            errors.append("Availability start time is missing.")

        if not self.availability_end:
            errors.append("Availability end time is missing.")

        return errors


def _existing_request_from_row(row) -> ExistingQualificationRequest:
    return ExistingQualificationRequest(
        id=int(row["id"]),
        discord_id=str(row["discord_id"]),
        discord_username=row["discord_username"],
        status=row["status"],
        timezone=row["timezone"],
        availability_start=row["availability_start"],
        availability_end=row["availability_end"],
        dotw=row["dotw"],
        times_pinged=int(row["times_pinged"] or 0),
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
    )


def ensure_user_exists(member: discord.Member) -> None:
    ts = now_ts()

    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO users (
                discord_id,
                discord_username,
                display_name,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                str(member.id),
                str(member.name),
                str(member.display_name),
                ts,
                ts,
            ),
        )


def update_user_timezone_setting(
    *,
    conn,
    discord_id: str,
    timezone: str | None,
) -> None:
    """Persist the qualification timezone into user_settings.

    The /get qualified flow already ensures the user exists before saving the
    request. This keeps the separate user settings timezone in sync without
    changing any notification preferences.
    """
    timezone = str(timezone or "").strip()

    if not timezone:
        return

    conn.execute(
        """
        INSERT OR IGNORE INTO user_settings (
            discord_id,
            timezone
        )
        VALUES (?, ?)
        """,
        (str(discord_id), timezone),
    )

    conn.execute(
        """
        UPDATE user_settings
        SET timezone = ?
        WHERE discord_id = ?
        """,
        (timezone, str(discord_id)),
    )


def get_existing_pending_or_mia_request(discord_id: str) -> ExistingQualificationRequest | None:
    """
    Finds the latest active request for the user.

    Pending requests take priority. MIA requests can be reopened by the user.
    Cancelled/denied/completed requests do not block a new request.
    """
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                id,
                discord_id,
                discord_username,
                status,
                timezone,
                availability_start,
                availability_end,
                dotw,
                times_pinged,
                created_at,
                updated_at
            FROM request_qual
            WHERE discord_id = ?
              AND status IN ('pending', 'mia')
            ORDER BY
                CASE status
                    WHEN 'pending' THEN 0
                    WHEN 'mia' THEN 1
                    ELSE 2
                END,
                updated_at DESC,
                created_at DESC,
                id DESC
            LIMIT 1
            """,
            (discord_id,),
        ).fetchone()

    if row is None:
        return None

    return _existing_request_from_row(row)


def bump_or_reopen_existing_request(request_id: int) -> ExistingQualificationRequest | None:
    """
    If pending: update updated_at.
    If MIA: set back to pending, reset times_pinged to 0, update updated_at.
    """
    ts = now_ts()

    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT status
            FROM request_qual
            WHERE id = ?
              AND status IN ('pending', 'mia')
            """,
            (request_id,),
        ).fetchone()

        if row is None:
            return None

        if row["status"] == "mia":
            conn.execute(
                """
                UPDATE request_qual
                SET status = 'pending',
                    times_pinged = 0,
                    updated_at = ?
                WHERE id = ?
                """,
                (ts, request_id),
            )
        else:
            conn.execute(
                """
                UPDATE request_qual
                SET updated_at = ?
                WHERE id = ?
                """,
                (ts, request_id),
            )

        updated = conn.execute(
            """
            SELECT
                id,
                discord_id,
                discord_username,
                status,
                timezone,
                availability_start,
                availability_end,
                dotw,
                times_pinged,
                created_at,
                updated_at
            FROM request_qual
            WHERE id = ?
            """,
            (request_id,),
        ).fetchone()

    if updated is None:
        return None

    return _existing_request_from_row(updated)


def cancel_existing_request(request_id: int, discord_id: str) -> None:
    ts = now_ts()

    with get_connection() as conn:
        conn.execute(
            """
            UPDATE request_qual
            SET status = 'cancelled',
                updated_at = ?
            WHERE id = ?
              AND discord_id = ?
              AND status IN ('pending', 'mia')
            """,
            (ts, request_id, discord_id),
        )


def update_existing_request_availability(draft: AvailabilityUpdateDraft) -> None:
    errors = draft.validate()

    if errors:
        raise ValueError("; ".join(errors))

    ts = now_ts()

    with get_connection() as conn:
        conn.execute(
            """
            UPDATE request_qual
            SET timezone = ?,
                availability_start = ?,
                availability_end = ?,
                dotw = ?,
                updated_at = ?
            WHERE id = ?
              AND discord_id = ?
              AND status IN ('pending', 'mia')
            """,
            (
                draft.timezone,
                draft.availability_start,
                draft.availability_end,
                draft.dotw,
                ts,
                draft.request_id,
                draft.discord_id,
            ),
        )

        update_user_timezone_setting(
            conn=conn,
            discord_id=draft.discord_id,
            timezone=draft.timezone,
        )


def create_request_qual_in_db(draft: QualificationRequestDraft) -> int:
    errors = draft.validate()

    if errors:
        raise ValueError("; ".join(errors))

    ts = now_ts()
    min_requirements = compute_min_requirements(draft.hours, draft.of_age)

    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO request_qual (
                discord_id,
                discord_username,

                min_requirements,
                of_age,
                hours,

                preferred_aircraft,
                timezone,

                availability_start,
                availability_end,
                dotw,

                remarks,
                status,
                referral,

                times_pinged,

                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'pending', ?, 0, ?, ?)
            """,
            (
                draft.discord_id,
                draft.discord_username,

                min_requirements,
                1 if draft.of_age else 0,
                float(draft.hours),

                draft.preferred_aircraft,
                draft.timezone,

                draft.availability_start,
                draft.availability_end,
                draft.dotw,

                draft.referral,

                ts,
                ts,
            ),
        )
        request_id = int(cur.lastrowid)

        update_user_timezone_setting(
            conn=conn,
            discord_id=draft.discord_id,
            timezone=draft.timezone,
        )

    return request_id
