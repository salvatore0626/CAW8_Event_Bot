from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import discord

from database import get_connection
from services.permission_service import member_can_receive_weekly_report
from services.wire_gpa_service import bolter_score, wire_score_map

try:
    from config import SCHEDULE_DEFAULT_TIMEZONE
except ImportError:
    SCHEDULE_DEFAULT_TIMEZONE = "America/Chicago"

try:
    from config import WEEKLY_REPORT_WEEKS_BACK
except ImportError:
    try:
        from config import WEEKLY_REPORT_DAYS_BACK

        WEEKLY_REPORT_WEEKS_BACK = max(1, int(WEEKLY_REPORT_DAYS_BACK) // 7)
    except Exception:
        WEEKLY_REPORT_WEEKS_BACK = 8

try:
    from config import WEEKLY_REPORT_DAY
except ImportError:
    WEEKLY_REPORT_DAY = 3

try:
    from config import WEEKLY_REPORT_NCIS_DATABASE_PATH
except ImportError:
    WEEKLY_REPORT_NCIS_DATABASE_PATH = str(
        Path(__file__).resolve().parents[2] / "NCIS Database" / "caw8.db"
    )

try:
    from config import WEEKLY_REPORT_OUTPUT_DIR
except ImportError:
    WEEKLY_REPORT_OUTPUT_DIR = str(
        Path(__file__).resolve().parents[1] / "data" / "weekly_report"
    )


@dataclass(frozen=True)
class WeekRange:
    start_ts: int
    end_ts: int
    start_local: datetime
    end_local: datetime

    @property
    def label(self) -> str:
        return f"{self.start_local:%b} {self.start_local.day}"


@dataclass(frozen=True)
class WeeklyTrendPoint:
    start_ts: int
    end_ts: int
    label: str
    ops_completed: int
    unique_attendees: int
    quals_passed: int
    bolters: int
    average_gpa: float | None
    qual_requests: int
    qual_attempts: int
    unique_chat_users: int
    unique_vc_users: int


@dataclass(frozen=True)
class WeeklyReportAssets:
    manifest_path: Path
    server_activity_path: Path
    operation_trends_path: Path
    qualification_activity_path: Path


@dataclass(frozen=True)
class WeeklyReportSnapshot:
    period: WeekRange
    net_users: int
    users_joined: int
    users_left: int
    quals_passed: int
    quals_pending: int
    unique_chat_users: int
    message_count: int
    unique_vc_users: int
    vc_seconds: int
    vc_categories: list[tuple[str, int]]
    ops_completed: int
    ops_cancelled: int
    attendance_count: int
    unique_attendees: int
    average_gpa: float | None
    bolters: int


ANSI_REPORT_TITLE = "CAW 8 Weekly Server Report"


def clean_text(value: Any) -> str:
    return str(value or "").strip()


def configured_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(str(SCHEDULE_DEFAULT_TIMEZONE or "America/Chicago"))
    except Exception:
        return ZoneInfo("America/Chicago")


def configured_report_day() -> int:
    try:
        value = int(WEEKLY_REPORT_DAY)
    except Exception:
        value = 3
    return max(1, min(7, value))


def configured_weeks_back() -> int:
    try:
        value = int(WEEKLY_REPORT_WEEKS_BACK)
    except Exception:
        value = 8
    return max(2, min(52, value))


def output_directory() -> Path:
    path = Path(str(WEEKLY_REPORT_OUTPUT_DIR)).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def report_assets() -> WeeklyReportAssets:
    directory = output_directory()
    return WeeklyReportAssets(
        manifest_path=directory / "weekly_report.json",
        server_activity_path=directory / "server_activity.png",
        operation_trends_path=directory / "operation_trends.png",
        qualification_activity_path=directory / "qualification_activity.png",
    )


def ncis_database_path() -> Path:
    return Path(str(WEEKLY_REPORT_NCIS_DATABASE_PATH)).expanduser().resolve()


def ncis_connection() -> sqlite3.Connection | None:
    path = ncis_database_path()
    if not path.exists():
        return None

    try:
        uri = f"file:{path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def latest_week_boundary(reference: datetime | None = None) -> datetime:
    tz = configured_timezone()
    now_local = reference.astimezone(tz) if reference else datetime.now(tz)
    midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    delta_days = (midnight.isoweekday() - configured_report_day()) % 7
    return midnight - timedelta(days=delta_days)


def latest_completed_week(reference: datetime | None = None) -> WeekRange:
    end_local = latest_week_boundary(reference)
    start_local = end_local - timedelta(days=7)
    return WeekRange(
        start_ts=int(start_local.timestamp()),
        end_ts=int(end_local.timestamp()),
        start_local=start_local,
        end_local=end_local,
    )


def previous_week(period: WeekRange) -> WeekRange:
    end_local = period.start_local
    start_local = end_local - timedelta(days=7)
    return WeekRange(
        start_ts=int(start_local.timestamp()),
        end_ts=int(end_local.timestamp()),
        start_local=start_local,
        end_local=end_local,
    )


def graph_week_ranges(reference: datetime | None = None) -> list[WeekRange]:
    final_end = latest_week_boundary(reference)
    count = configured_weeks_back()
    first_start = final_end - timedelta(days=7 * count)

    ranges: list[WeekRange] = []
    for index in range(count):
        start_local = first_start + timedelta(days=7 * index)
        end_local = start_local + timedelta(days=7)
        ranges.append(
            WeekRange(
                start_ts=int(start_local.timestamp()),
                end_ts=int(end_local.timestamp()),
                start_local=start_local,
                end_local=end_local,
            )
        )
    return ranges


def format_local_date(value: datetime) -> str:
    return f"{value:%b} {value.day}, {value.year}"


def date_range_text(period: WeekRange) -> str:
    # The SQL interval is report-day 00:00 -> report-day 00:00. Human-facing
    # copy shows the seven calendar days contained inside that interval.
    inclusive_end = period.end_local - timedelta(days=1)
    return f"{format_local_date(period.start_local)} - {format_local_date(inclusive_end)}"


def compact_duration(total_seconds: int) -> str:
    total_seconds = max(0, int(total_seconds or 0))
    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def signed_number(value: int) -> str:
    return f"{value:+d}" if value else "0"


def comparison_arrow(
    current: int | float | None,
    previous: int | float | None,
    *,
    decimals: int | None = None,
) -> str:
    """Return a simple week-over-week direction arrow for the displayed value."""
    if current is None and previous is None:
        return "→"
    if current is None:
        return "↓"
    if previous is None:
        return "↑"

    current_value = float(current)
    previous_value = float(previous)
    if decimals is not None:
        current_value = round(current_value, decimals)
        previous_value = round(previous_value, decimals)

    if current_value > previous_value:
        return "↑"
    if current_value < previous_value:
        return "↓"
    return "→"


def attendee_key(discord_id: Any, user_name: Any) -> str | None:
    discord_text = clean_text(discord_id)
    if discord_text:
        return f"id:{discord_text}"
    name = clean_text(user_name).casefold()
    if name:
        return f"name:{name}"
    return None


def gpa_from_attendance_rows(rows: Iterable[sqlite3.Row]) -> tuple[float | None, int, int]:
    scores = wire_score_map()
    total_points = 0.0
    attempts = 0
    total_bolters = 0

    for row in rows:
        try:
            bolters = max(0, min(24, int(row["bolters"] or 0)))
        except Exception:
            bolters = 0
        total_bolters += bolters
        attempts += bolters
        total_points += bolters * bolter_score()

        try:
            wire = int(row["wires"])
        except Exception:
            wire = 0
        if wire in scores:
            total_points += scores[wire]
            attempts += 1

    if attempts <= 0:
        return None, 0, total_bolters
    return total_points / attempts, attempts, total_bolters


def airboss_week_values(period: WeekRange) -> dict[str, Any]:
    with get_connection() as conn:
        op_rows = conn.execute(
            """
            SELECT event_id, status
            FROM op_events
            WHERE scheduled_at >= ?
              AND scheduled_at < ?
            """,
            (period.start_ts, period.end_ts),
        ).fetchall()

        complete_ids = [
            int(row["event_id"])
            for row in op_rows
            if clean_text(row["status"]).casefold() == "complete"
        ]
        cancelled_count = sum(
            1
            for row in op_rows
            if clean_text(row["status"]).casefold() in {"cancelled", "canceled"}
        )

        attendance_rows: list[sqlite3.Row] = []
        if complete_ids:
            placeholders = ",".join("?" for _ in complete_ids)
            attendance_rows = conn.execute(
                f"""
                SELECT
                    a.entry_id,
                    a.discord_id,
                    a.user_name,
                    a.landing_type,
                    a.wires,
                    a.bolters
                FROM attendance a
                WHERE a.scheduled_op_id IN ({placeholders})
                  AND a.status IN ('submitted', 'complete')
                ORDER BY a.entry_id ASC
                """,
                tuple(complete_ids),
            ).fetchall()

        qual_requests = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM request_qual
            WHERE created_at >= ?
              AND created_at < ?
            """,
            (period.start_ts, period.end_ts),
        ).fetchone()["count"]

        qual_attempts = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM qual_log
            WHERE created_at >= ?
              AND created_at < ?
            """,
            (period.start_ts, period.end_ts),
        ).fetchone()["count"]

        qual_passes = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM qual_log
            WHERE pass = 1
              AND created_at >= ?
              AND created_at < ?
            """,
            (period.start_ts, period.end_ts),
        ).fetchone()["count"]

        pending_quals = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM request_qual
            WHERE LOWER(COALESCE(status, '')) = 'pending'
            """
        ).fetchone()["count"]

    unique_attendees = {
        key
        for row in attendance_rows
        if (key := attendee_key(row["discord_id"], row["user_name"])) is not None
    }
    gpa, gpa_attempts, bolters = gpa_from_attendance_rows(attendance_rows)

    return {
        "ops_completed": len(complete_ids),
        "ops_cancelled": int(cancelled_count),
        "attendance_count": len(attendance_rows),
        "unique_attendees": len(unique_attendees),
        "qual_requests": int(qual_requests or 0),
        "qual_attempts": int(qual_attempts or 0),
        "quals_passed": int(qual_passes or 0),
        "quals_pending": int(pending_quals or 0),
        "average_gpa": gpa,
        "gpa_attempts": gpa_attempts,
        "bolters": int(bolters),
    }


def ncis_week_values(period: WeekRange) -> dict[str, Any]:
    conn = ncis_connection()
    if conn is None:
        return {
            "net_users": 0,
            "users_joined": 0,
            "users_left": 0,
            "unique_chat_users": 0,
            "message_count": 0,
            "unique_vc_users": 0,
            "vc_seconds": 0,
            "vc_categories": [],
        }

    try:
        users_joined = 0
        users_left = 0
        if table_exists(conn, "users"):
            row = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN joined_at >= ? AND joined_at < ? THEN 1 ELSE 0 END) AS joined_count,
                    SUM(CASE WHEN left_at >= ? AND left_at < ? THEN 1 ELSE 0 END) AS left_count
                FROM users
                WHERE COALESCE(bot, 0) = 0
                """,
                (period.start_ts, period.end_ts, period.start_ts, period.end_ts),
            ).fetchone()
            users_joined = int(row["joined_count"] or 0)
            users_left = int(row["left_count"] or 0)

        chat_users = 0
        messages = 0
        if table_exists(conn, "chat_ledger"):
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS message_count,
                    COUNT(DISTINCT cl.user_id) AS unique_users
                FROM chat_ledger cl
                LEFT JOIN users u ON u.user_id = cl.user_id
                WHERE cl.timestamp >= ?
                  AND cl.timestamp < ?
                  AND LOWER(COALESCE(cl.event, 'message_create')) = 'message_create'
                  AND COALESCE(u.bot, 0) = 0
                """,
                (period.start_ts, period.end_ts),
            ).fetchone()
            messages = int(row["message_count"] or 0)
            chat_users = int(row["unique_users"] or 0)

        vc_users = 0
        vc_seconds = 0
        vc_categories: list[tuple[str, int]] = []
        if table_exists(conn, "voice_session_ledger"):
            row = conn.execute(
                """
                SELECT
                    COUNT(DISTINCT v.user_id) AS unique_users,
                    COALESCE(SUM(v.duration_seconds), 0) AS duration_seconds
                FROM voice_session_ledger v
                LEFT JOIN users u ON u.user_id = v.user_id
                WHERE v.joined_at >= ?
                  AND v.joined_at < ?
                  AND COALESCE(u.bot, 0) = 0
                """,
                (period.start_ts, period.end_ts),
            ).fetchone()
            vc_users = int(row["unique_users"] or 0)
            vc_seconds = int(row["duration_seconds"] or 0)

            if table_exists(conn, "channels"):
                category_rows = conn.execute(
                    """
                    SELECT
                        COALESCE(NULLIF(TRIM(c.category_name), ''), 'Uncategorized') AS category_name,
                        COALESCE(SUM(v.duration_seconds), 0) AS duration_seconds
                    FROM voice_session_ledger v
                    LEFT JOIN users u ON u.user_id = v.user_id
                    LEFT JOIN channels c ON c.channel_id = v.channel_id
                    WHERE v.joined_at >= ?
                      AND v.joined_at < ?
                      AND COALESCE(u.bot, 0) = 0
                    GROUP BY COALESCE(NULLIF(TRIM(c.category_name), ''), 'Uncategorized')
                    HAVING duration_seconds > 0
                    ORDER BY duration_seconds DESC, category_name COLLATE NOCASE ASC
                    """,
                    (period.start_ts, period.end_ts),
                ).fetchall()
                vc_categories = [
                    (clean_text(row["category_name"]) or "Uncategorized", int(row["duration_seconds"] or 0))
                    for row in category_rows
                ]

        return {
            "net_users": users_joined - users_left,
            "users_joined": users_joined,
            "users_left": users_left,
            "unique_chat_users": chat_users,
            "message_count": messages,
            "unique_vc_users": vc_users,
            "vc_seconds": vc_seconds,
            "vc_categories": vc_categories,
        }
    finally:
        conn.close()


def build_snapshot(period: WeekRange | None = None) -> WeeklyReportSnapshot:
    period = period or latest_completed_week()
    airboss = airboss_week_values(period)
    ncis = ncis_week_values(period)

    return WeeklyReportSnapshot(
        period=period,
        net_users=ncis["net_users"],
        users_joined=ncis["users_joined"],
        users_left=ncis["users_left"],
        quals_passed=airboss["quals_passed"],
        quals_pending=airboss["quals_pending"],
        unique_chat_users=ncis["unique_chat_users"],
        message_count=ncis["message_count"],
        unique_vc_users=ncis["unique_vc_users"],
        vc_seconds=ncis["vc_seconds"],
        vc_categories=ncis["vc_categories"],
        ops_completed=airboss["ops_completed"],
        ops_cancelled=airboss["ops_cancelled"],
        attendance_count=airboss["attendance_count"],
        unique_attendees=airboss["unique_attendees"],
        average_gpa=airboss["average_gpa"],
        bolters=airboss["bolters"],
    )


def build_trend_points(reference: datetime | None = None) -> list[WeeklyTrendPoint]:
    points: list[WeeklyTrendPoint] = []
    for period in graph_week_ranges(reference):
        airboss = airboss_week_values(period)
        ncis = ncis_week_values(period)
        points.append(
            WeeklyTrendPoint(
                start_ts=period.start_ts,
                end_ts=period.end_ts,
                label=period.start_local.strftime("%b %d"),
                ops_completed=airboss["ops_completed"],
                unique_attendees=airboss["unique_attendees"],
                quals_passed=airboss["quals_passed"],
                bolters=airboss["bolters"],
                average_gpa=airboss["average_gpa"],
                qual_requests=airboss["qual_requests"],
                qual_attempts=airboss["qual_attempts"],
                unique_chat_users=ncis["unique_chat_users"],
                unique_vc_users=ncis["unique_vc_users"],
            )
        )
    return points


def snapshot_to_dict(snapshot: WeeklyReportSnapshot) -> dict[str, Any]:
    return {
        "period_start_ts": snapshot.period.start_ts,
        "period_end_ts": snapshot.period.end_ts,
        "period_start_local": snapshot.period.start_local.isoformat(),
        "period_end_local": snapshot.period.end_local.isoformat(),
        "net_users": snapshot.net_users,
        "users_joined": snapshot.users_joined,
        "users_left": snapshot.users_left,
        "quals_passed": snapshot.quals_passed,
        "quals_pending": snapshot.quals_pending,
        "unique_chat_users": snapshot.unique_chat_users,
        "message_count": snapshot.message_count,
        "unique_vc_users": snapshot.unique_vc_users,
        "vc_seconds": snapshot.vc_seconds,
        "vc_categories": snapshot.vc_categories,
        "ops_completed": snapshot.ops_completed,
        "ops_cancelled": snapshot.ops_cancelled,
        "attendance_count": snapshot.attendance_count,
        "unique_attendees": snapshot.unique_attendees,
        "average_gpa": snapshot.average_gpa,
        "bolters": snapshot.bolters,
    }


def snapshot_from_dict(data: dict[str, Any]) -> WeeklyReportSnapshot:
    tz = configured_timezone()
    start_local = datetime.fromisoformat(str(data["period_start_local"])).astimezone(tz)
    end_local = datetime.fromisoformat(str(data["period_end_local"])).astimezone(tz)
    period = WeekRange(
        start_ts=int(data["period_start_ts"]),
        end_ts=int(data["period_end_ts"]),
        start_local=start_local,
        end_local=end_local,
    )
    return WeeklyReportSnapshot(
        period=period,
        net_users=int(data.get("net_users", 0)),
        users_joined=int(data.get("users_joined", 0)),
        users_left=int(data.get("users_left", 0)),
        quals_passed=int(data.get("quals_passed", 0)),
        quals_pending=int(data.get("quals_pending", 0)),
        unique_chat_users=int(data.get("unique_chat_users", 0)),
        message_count=int(data.get("message_count", 0)),
        unique_vc_users=int(data.get("unique_vc_users", 0)),
        vc_seconds=int(data.get("vc_seconds", 0)),
        vc_categories=[(str(name), int(seconds)) for name, seconds in data.get("vc_categories", [])],
        ops_completed=int(data.get("ops_completed", 0)),
        ops_cancelled=int(data.get("ops_cancelled", 0)),
        attendance_count=int(data.get("attendance_count", 0)),
        unique_attendees=int(data.get("unique_attendees", 0)),
        average_gpa=(float(data["average_gpa"]) if data.get("average_gpa") is not None else None),
        bolters=int(data.get("bolters", 0)),
    )


def format_report_message(
    snapshot: WeeklyReportSnapshot,
    previous_snapshot: WeeklyReportSnapshot | None = None,
) -> str:
    if previous_snapshot is None:
        previous_snapshot = build_snapshot(previous_week(snapshot.period))

    gpa_text = f"{snapshot.average_gpa:.2f}" if snapshot.average_gpa is not None else "N/A"
    weekday = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][configured_report_day() - 1]

    server_block = [
        f"Net Users          {signed_number(snapshot.net_users)} {comparison_arrow(snapshot.net_users, previous_snapshot.net_users)}",
        f"Completed Quals    {snapshot.quals_passed:,} {comparison_arrow(snapshot.quals_passed, previous_snapshot.quals_passed)} ({snapshot.quals_pending:,} pending)",
        f"Unique Chat Users  {snapshot.unique_chat_users:,} {comparison_arrow(snapshot.unique_chat_users, previous_snapshot.unique_chat_users)} ({snapshot.message_count:,} messages)",
        f"Unique VC Users    {snapshot.unique_vc_users:,} {comparison_arrow(snapshot.unique_vc_users, previous_snapshot.unique_vc_users)} ({compact_duration(snapshot.vc_seconds)})",
    ]

    category_block: list[str] = []
    current_categories = snapshot.vc_categories[:4]
    if current_categories:
        current_denominator = max(1, snapshot.vc_seconds or sum(seconds for _, seconds in snapshot.vc_categories))
        previous_denominator = max(
            1,
            previous_snapshot.vc_seconds
            or sum(seconds for _, seconds in previous_snapshot.vc_categories),
        )
        previous_seconds_by_category = {
            category.casefold(): seconds
            for category, seconds in previous_snapshot.vc_categories
        }
        width = min(22, max(8, max(len(category) for category, _ in current_categories)))

        for category, seconds in current_categories:
            percent = (seconds / current_denominator) * 100.0
            previous_seconds = previous_seconds_by_category.get(category.casefold(), 0)
            previous_percent = (previous_seconds / previous_denominator) * 100.0
            arrow = comparison_arrow(percent, previous_percent, decimals=0)
            category_block.append(
                f"{category[:width]:<{width}}  {percent:>3.0f}% {arrow}  {compact_duration(seconds):>8}"
            )
    else:
        category_block.append("No VC activity recorded.")

    operations_block = [
        f"Ops Run            {snapshot.ops_completed:,} {comparison_arrow(snapshot.ops_completed, previous_snapshot.ops_completed)} ({snapshot.ops_cancelled:,} canceled)",
        f"Attendances        {snapshot.attendance_count:,} {comparison_arrow(snapshot.attendance_count, previous_snapshot.attendance_count)} ({snapshot.unique_attendees:,} unique users)",
        f"Average GPA        {gpa_text} {comparison_arrow(snapshot.average_gpa, previous_snapshot.average_gpa, decimals=2)} ({snapshot.bolters:,} bolters)",
    ]

    return "\n".join(
        [
            f"## {ANSI_REPORT_TITLE}",
            f"**{date_range_text(snapshot.period)}**",
            "",
            "**SERVER**",
            "```text",
            *server_block,
            "```",
            "**VC ACTIVITY BY CATEGORY**",
            "```text",
            *category_block,
            "```",
            "**OPERATIONS**",
            "```text",
            *operations_block,
            "```",
            f"-# ↑ higher • ↓ lower • → unchanged vs previous week • {configured_weeks_back()}-week trends attached • Weeks grouped {weekday} → {weekday}",
        ]
    )


def _smooth_curve(values: list[float], samples_per_segment: int = 24) -> tuple[list[float], list[float]]:
    """Return a Catmull-Rom style smooth curve while preserving real markers."""
    import numpy as np

    if len(values) <= 2:
        return list(range(len(values))), [float(v) for v in values]

    y = np.asarray(values, dtype=float)
    xs: list[float] = []
    ys: list[float] = []

    for i in range(len(y) - 1):
        p0 = y[max(0, i - 1)]
        p1 = y[i]
        p2 = y[i + 1]
        p3 = y[min(len(y) - 1, i + 2)]
        ts = np.linspace(0.0, 1.0, samples_per_segment, endpoint=False)
        for t in ts:
            value = 0.5 * (
                (2 * p1)
                + (-p0 + p2) * t
                + (2 * p0 - 5 * p1 + 4 * p2 - p3) * (t ** 2)
                + (-p0 + 3 * p1 - 3 * p2 + p3) * (t ** 3)
            )
            xs.append(i + float(t))
            ys.append(max(0.0, float(value)))

    xs.append(float(len(y) - 1))
    ys.append(max(0.0, float(y[-1])))
    return xs, ys


def _plot_smooth_line(ax, x, values, *, label: str, color: str, linewidth: float = 2.3):
    smooth_x, smooth_y = _smooth_curve([float(v) for v in values])
    line = ax.plot(smooth_x, smooth_y, label=label, color=color, linewidth=linewidth)[0]
    ax.scatter(x, values, color=color, s=25, zorder=4)
    return line


def _plot_smooth_optional_line(
    ax,
    x,
    values,
    *,
    label: str,
    color: str,
    linewidth: float = 2.3,
):
    """Plot contiguous numeric segments while leaving missing weeks blank."""
    valid_points = [(idx, float(value)) for idx, value in zip(x, values) if value is not None]
    if not valid_points:
        return ax.plot([], [], label=label, color=color, linewidth=linewidth)[0]

    segments: list[list[tuple[int, float]]] = []
    current: list[tuple[int, float]] = []
    previous_index: int | None = None
    for idx, value in valid_points:
        if previous_index is None or idx == previous_index + 1:
            current.append((idx, value))
        else:
            segments.append(current)
            current = [(idx, value)]
        previous_index = idx
    if current:
        segments.append(current)

    legend_line = None
    for segment in segments:
        seg_x = [idx for idx, _ in segment]
        seg_y = [value for _, value in segment]
        if len(segment) >= 2:
            smooth_x, smooth_y = _smooth_curve(seg_y)
            shifted_x = [seg_x[0] + value for value in smooth_x]
            line = ax.plot(
                shifted_x,
                smooth_y,
                label=label if legend_line is None else None,
                color=color,
                linewidth=linewidth,
            )[0]
        else:
            line = ax.plot(
                seg_x,
                seg_y,
                label=label if legend_line is None else None,
                color=color,
                linewidth=linewidth,
            )[0]
        if legend_line is None:
            legend_line = line
        ax.scatter(seg_x, seg_y, color=color, s=25, zorder=4)

    return legend_line


def _prepare_matplotlib():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    return plt


def _apply_dark_chart_theme(fig, axes: Iterable[Any]) -> None:
    background = "#0b1017"
    panel = "#111923"
    foreground = "#dce6f2"
    muted = "#9aabba"
    grid = "#2a3746"
    spine = "#344354"

    fig.patch.set_facecolor(background)
    for ax in axes:
        ax.set_facecolor(panel)
        ax.tick_params(axis="both", colors=muted, labelsize=9)
        ax.xaxis.label.set_color(muted)
        ax.yaxis.label.set_color(muted)
        ax.title.set_color(foreground)
        for side in ax.spines.values():
            side.set_color(spine)
        ax.grid(True, axis="y", color=grid, alpha=0.70, linewidth=0.8)


def _style_legend(legend) -> None:
    if legend is None:
        return
    frame = legend.get_frame()
    frame.set_facecolor("#111923")
    frame.set_edgecolor("#344354")
    frame.set_alpha(0.92)
    for text in legend.get_texts():
        text.set_color("#dce6f2")


def generate_server_activity_image(points: list[WeeklyTrendPoint], path: Path) -> None:
    plt = _prepare_matplotlib()
    fig, ax = plt.subplots(figsize=(14, 6.5), dpi=130)

    x = list(range(len(points)))
    chat = [point.unique_chat_users for point in points]
    vc = [point.unique_vc_users for point in points]

    _apply_dark_chart_theme(fig, [ax])

    _plot_smooth_line(ax, x, chat, label="Unique Chat Users", color="#4cc9f0")
    _plot_smooth_line(ax, x, vc, label="Unique VC Users", color="#72e0a7")

    ax.set_title("Server Activity", fontsize=17, pad=14, fontweight="bold")
    ax.set_ylabel("Unique Users")
    weekday = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][configured_report_day() - 1]
    ax.set_xlabel(f"Weeks grouped {weekday} to {weekday}")
    ax.set_xticks(x)
    ax.set_xticklabels([point.label for point in points], rotation=35, ha="right")
    ax.set_ylim(bottom=0)
    legend = ax.legend(loc="upper left", frameon=True, ncol=2)
    _style_legend(legend)
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


def generate_operation_trends_image(points: list[WeeklyTrendPoint], path: Path) -> None:
    plt = _prepare_matplotlib()
    fig, ax_counts = plt.subplots(figsize=(14, 6.5), dpi=130)
    ax_gpa = ax_counts.twinx()

    _apply_dark_chart_theme(fig, [ax_counts, ax_gpa])

    x = list(range(len(points)))
    attendees = [point.unique_attendees for point in points]
    ops = [point.ops_completed for point in points]
    bolters = [point.bolters for point in points]
    gpa = [((point.average_gpa * 10.0) if point.average_gpa is not None else None) for point in points]

    line_ops = _plot_smooth_line(
        ax_counts,
        x,
        ops,
        label="Ops Completed",
        color="#72e0a7",
    )
    line_attendees = _plot_smooth_line(
        ax_counts,
        x,
        attendees,
        label="Unique Attendees",
        color="#4cc9f0",
    )
    line_bolters = _plot_smooth_line(
        ax_counts,
        x,
        bolters,
        label="Bolters",
        color="#ffd166",
    )
    line_gpa = _plot_smooth_optional_line(
        ax_gpa,
        x,
        gpa,
        label="Average GPA",
        color="#c77dff",
    )

    ax_counts.set_title("Operation Trends", fontsize=17, pad=14, fontweight="bold")
    ax_counts.set_ylabel("Weekly Counts")
    ax_gpa.set_ylabel("Average GPA", color="#c77dff")
    ax_counts.set_xlabel("Week starting")
    ax_counts.set_xticks(x)
    ax_counts.set_xticklabels([point.label for point in points], rotation=35, ha="right")
    ax_counts.set_ylim(bottom=0)
    ax_gpa.set_ylim(0, 40)
    ax_gpa.set_yticks([0, 10, 20, 30, 40])
    ax_gpa.set_yticklabels(["0", "1", "2", "3", "4"])
    ax_gpa.tick_params(axis="y", colors="#c77dff")
    ax_gpa.yaxis.label.set_color("#c77dff")
    ax_gpa.grid(False)

    legend = ax_counts.legend(
        handles=[line_ops, line_attendees, line_bolters, line_gpa],
        labels=["Ops Completed", "Unique Attendees", "Bolters", "Average GPA"],
        loc="upper left",
        frameon=True,
        ncol=2,
    )
    _style_legend(legend)
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


def generate_qualification_activity_image(points: list[WeeklyTrendPoint], path: Path) -> None:
    plt = _prepare_matplotlib()
    fig, ax = plt.subplots(figsize=(14, 6.5), dpi=130)

    _apply_dark_chart_theme(fig, [ax])

    x = list(range(len(points)))
    requests = [point.qual_requests for point in points]
    attempts = [point.qual_attempts for point in points]
    passed = [point.quals_passed for point in points]

    _plot_smooth_line(
        ax,
        x,
        requests,
        label="Qual Requests",
        color="#4cc9f0",
    )
    _plot_smooth_line(
        ax,
        x,
        attempts,
        label="Quals Attempted",
        color="#ffd166",
    )
    _plot_smooth_line(
        ax,
        x,
        passed,
        label="Quals Passed",
        color="#72e0a7",
    )

    ax.set_title("Qualification Activity", fontsize=17, pad=14, fontweight="bold")
    ax.set_ylabel("Qualifications")
    ax.set_xlabel("Week starting")
    ax.set_xticks(x)
    ax.set_xticklabels([point.label for point in points], rotation=35, ha="right")
    ax.set_ylim(bottom=0)
    legend = ax.legend(loc="upper left", frameon=True, ncol=3)
    _style_legend(legend)
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)

def generate_and_cache_weekly_report(reference: datetime | None = None) -> WeeklyReportAssets:
    assets = report_assets()
    snapshot = build_snapshot(latest_completed_week(reference))
    previous_snapshot = build_snapshot(previous_week(snapshot.period))
    points = build_trend_points(reference)

    generate_server_activity_image(points, assets.server_activity_path)
    generate_operation_trends_image(points, assets.operation_trends_path)
    generate_qualification_activity_image(points, assets.qualification_activity_path)

    manifest = {
        "generated_at": int(time.time()),
        "report_day": configured_report_day(),
        "weeks_back": configured_weeks_back(),
        "snapshot": snapshot_to_dict(snapshot),
        "previous_snapshot": snapshot_to_dict(previous_snapshot),
        "trend_points": [point.__dict__ for point in points],
    }
    assets.manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return assets


def load_cached_snapshot() -> WeeklyReportSnapshot | None:
    assets = report_assets()
    if not assets.manifest_path.exists():
        return None
    try:
        data = json.loads(assets.manifest_path.read_text(encoding="utf-8"))
        return snapshot_from_dict(data["snapshot"])
    except Exception:
        return None


def load_cached_previous_snapshot() -> WeeklyReportSnapshot | None:
    assets = report_assets()
    if assets.manifest_path.exists():
        try:
            data = json.loads(assets.manifest_path.read_text(encoding="utf-8"))
            previous_data = data.get("previous_snapshot")
            if previous_data:
                return snapshot_from_dict(previous_data)
        except Exception:
            pass

    current = load_cached_snapshot()
    if current is None:
        return None
    try:
        return build_snapshot(previous_week(current.period))
    except Exception:
        return None


def ensure_cached_report() -> tuple[WeeklyReportSnapshot, WeeklyReportAssets]:
    assets = report_assets()
    snapshot = load_cached_snapshot()
    if (
        snapshot is None
        or not assets.server_activity_path.exists()
        or not assets.operation_trends_path.exists()
        or not assets.qualification_activity_path.exists()
    ):
        assets = generate_and_cache_weekly_report()
        snapshot = load_cached_snapshot()
    if snapshot is None:
        snapshot = build_snapshot()
    return snapshot, assets


def weekly_report_subscribers() -> list[str]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT us.discord_id
            FROM user_settings us
            LEFT JOIN users u ON u.discord_id = us.discord_id
            WHERE COALESCE(us.notify_weekly_report, 0) = 1
              AND COALESCE(u.status, 'Active') = 'Active'
            ORDER BY us.discord_id ASC
            """
        ).fetchall()
    return [str(row["discord_id"]) for row in rows]


def _discord_files(assets: WeeklyReportAssets) -> list[discord.File]:
    files: list[discord.File] = []
    if assets.server_activity_path.exists():
        files.append(discord.File(assets.server_activity_path, filename="server_activity.png"))
    if assets.operation_trends_path.exists():
        files.append(discord.File(assets.operation_trends_path, filename="operation_trends.png"))
    if assets.qualification_activity_path.exists():
        files.append(
            discord.File(
                assets.qualification_activity_path,
                filename="qualification_activity.png",
            )
        )
    return files


async def send_weekly_report_dm(
    target: discord.Member,
    *,
    regenerate_if_missing: bool = True,
) -> bool:
    # The scheduled/preview report deliberately ignores the member's normal
    # notification window, but it never bypasses the Admin/Instructor role gate.
    if not member_can_receive_weekly_report(target):
        return False

    try:
        if regenerate_if_missing:
            snapshot, assets = await asyncio.to_thread(ensure_cached_report)
        else:
            snapshot = load_cached_snapshot()
            assets = report_assets()
            if snapshot is None:
                snapshot, assets = await asyncio.to_thread(ensure_cached_report)

        previous_snapshot = load_cached_previous_snapshot()
        await target.send(
            content=format_report_message(snapshot, previous_snapshot),
            files=_discord_files(assets),
        )
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False
    except Exception:
        return False


def report_state_path() -> Path:
    return output_directory() / "weekly_report_state.json"


def load_report_state() -> dict[str, Any]:
    path = report_state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_report_state(state: dict[str, Any]) -> None:
    report_state_path().write_text(json.dumps(state, indent=2), encoding="utf-8")


def current_boundary_key(reference: datetime | None = None) -> str:
    return latest_week_boundary(reference).strftime("%Y-%m-%d")
