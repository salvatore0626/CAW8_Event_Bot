from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable, Literal

from database import get_connection


OrderMode = Literal[
    "oldest",
    "newest",
    "vtol_time",
    "pings_asc",
    "pings_desc",
    "bumped_asc",
    "bumped_desc",
]


def now_ts() -> int:
    return int(time.time())


@dataclass
class QualRequestRecord:
    id: int
    discord_id: str | None
    discord_username: str | None

    min_requirements: str | None
    of_age: int | None
    hours: float | None

    preferred_aircraft: str | None
    timezone: str | None

    availability_start: str | None
    availability_end: str | None
    dotw: str | None

    remarks: str | None
    status: str | None
    referral: str | None

    times_pinged: int
    qual_attempts: int

    created_at: int | None
    updated_at: int | None


def _row_to_record(row) -> QualRequestRecord:
    return QualRequestRecord(
        id=row["id"],
        discord_id=row["discord_id"],
        discord_username=row["discord_username"],

        min_requirements=row["min_requirements"],
        of_age=row["of_age"],
        hours=row["hours"],

        preferred_aircraft=row["preferred_aircraft"],
        timezone=row["timezone"],

        availability_start=row["availability_start"],
        availability_end=row["availability_end"],
        dotw=row["dotw"],

        remarks=row["remarks"],
        status=row["status"],
        referral=row["referral"],

        times_pinged=int(row["times_pinged"] or 0),
        qual_attempts=int(row["qual_attempts"] or 0),

        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def get_pending_qual_requests(order: OrderMode = "oldest") -> list[QualRequestRecord]:
    if order == "newest":
        order_sql = "created_at DESC, updated_at DESC, id DESC"
    elif order == "vtol_time":
        order_sql = "COALESCE(hours, 0) DESC, updated_at DESC, created_at DESC, id DESC"
    elif order == "pings_asc":
        order_sql = "COALESCE(times_pinged, 0) ASC, updated_at ASC, created_at ASC, id ASC"
    elif order == "pings_desc":
        order_sql = "COALESCE(times_pinged, 0) DESC, updated_at DESC, created_at DESC, id DESC"
    elif order == "bumped_asc":
        order_sql = "updated_at ASC, created_at ASC, id ASC"
    elif order == "bumped_desc":
        order_sql = "updated_at DESC, created_at DESC, id DESC"
    else:
        order_sql = "created_at ASC, updated_at ASC, id ASC"

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

                preferred_aircraft,
                timezone,

                availability_start,
                availability_end,
                dotw,

                remarks,
                status,
                referral,

                times_pinged,

                (
                    SELECT COUNT(*)
                    FROM qual_log
                    WHERE qual_log.applicant_discord_id = request_qual.discord_id
                ) AS qual_attempts,

                created_at,
                updated_at
            FROM request_qual
            WHERE status = 'pending'
            ORDER BY {order_sql}
            """
        ).fetchall()

    return [_row_to_record(row) for row in rows]


def increment_qual_request_ping_count(request_id: int) -> None:
    increment_qual_request_ping_counts([request_id])


def increment_qual_request_ping_counts(request_ids: Iterable[int]) -> None:
    request_ids = list(dict.fromkeys(int(request_id) for request_id in request_ids))

    if not request_ids:
        return

    ts = now_ts()

    with get_connection() as conn:
        conn.executemany(
            """
            UPDATE request_qual
            SET times_pinged = COALESCE(times_pinged, 0) + 1,
                updated_at = ?
            WHERE id = ?
              AND status = 'pending'
            """,
            [(ts, request_id) for request_id in request_ids],
        )


def deny_qual_request(
    request_id: int,
    instructor_discord_id: str,
    remarks: str,
) -> None:
    ts = now_ts()

    with get_connection() as conn:
        conn.execute(
            """
            UPDATE request_qual
            SET status = 'denied',
                remarks = ?,
                updated_at = ?
            WHERE id = ?
              AND status = 'pending'
            """,
            (
                f"Denied by {instructor_discord_id}: {remarks}",
                ts,
                request_id,
            ),
        )


def mark_qual_request_mia(
    request_id: int,
    instructor_discord_id: str,
    remarks: str,
) -> None:
    ts = now_ts()

    with get_connection() as conn:
        conn.execute(
            """
            UPDATE request_qual
            SET status = 'mia',
                remarks = ?,
                updated_at = ?
            WHERE id = ?
              AND status = 'pending'
            """,
            (
                f"Marked MIA by {instructor_discord_id}: {remarks}",
                ts,
                request_id,
            ),
        )
