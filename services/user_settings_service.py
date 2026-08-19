from dataclasses import dataclass
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord

from database import get_connection, ensure_user_settings_schema


DEFAULT_NOTIFY_START = "09:00"
DEFAULT_NOTIFY_END = "21:00"
DEFAULT_TIMEZONE = "America/Chicago"


def is_valid_timezone(value: str | None) -> bool:
    """Return True when value is a valid installed IANA timezone name."""
    return normalize_timezone_name(value) is not None


def safe_timezone_name(value: str | None, default: str = DEFAULT_TIMEZONE) -> str:
    """Return a valid timezone name, falling back without changing stored data."""
    normalized = normalize_timezone_name(value)
    if normalized is not None:
        return normalized

    fallback = normalize_timezone_name(default)
    return fallback or DEFAULT_TIMEZONE


def safe_zoneinfo(value: str | None, default: str = DEFAULT_TIMEZONE) -> ZoneInfo:
    """Return ZoneInfo for value, using Central Time when it is missing/invalid."""
    return ZoneInfo(safe_timezone_name(value, default))


def timezone_select_options(
    configured_options: list[tuple[str, str]],
    selected: str | None,
    *,
    limit: int = 25,
) -> list[discord.SelectOption]:
    """Build quick-pick options and preserve an unlisted valid current value."""
    selected_normalized = normalize_timezone_name(selected)
    configured_values = {str(value).strip() for _, value in configured_options}
    include_current = bool(
        selected_normalized and selected_normalized not in configured_values
    )

    options: list[discord.SelectOption] = []
    if include_current and selected_normalized is not None:
        options.append(
            discord.SelectOption(
                label=f"Current: {selected_normalized}"[:100],
                value=selected_normalized,
                description="Saved timezone (not in quick-pick list)",
                default=True,
            )
        )

    seen: set[str] = set()
    for label, value in configured_options:
        normalized = normalize_timezone_name(value)
        if normalized is None or normalized in seen:
            continue
        seen.add(normalized)
        options.append(
            discord.SelectOption(
                label=str(label)[:100],
                value=normalized,
                description=normalized[:100],
                default=(selected_normalized == normalized),
            )
        )
        if len(options) >= max(1, int(limit)):
            break

    return options[: max(1, int(limit))]


@dataclass
class UserSettings:
    discord_id: str
    timezone: str | None
    notify_start: str
    notify_end: str
    notify_flightlead: bool
    notify_instructor: bool
    notify_training: bool
    notify_weekly_report: bool
    notify_operations: bool


def now_ts() -> int:
    return int(time.time())


def normalize_time_value(value: str | None, default: str) -> str:
    text = str(value or "").strip()

    if not text:
        return default

    try:
        hour_text, minute_text = text.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except (TypeError, ValueError):
        return default

    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return default

    return f"{hour:02d}:{minute:02d}"




def repair_missing_notification_window(discord_id: str, notify_start: str | None, notify_end: str | None) -> tuple[str, str]:
    fixed_start = normalize_time_value(notify_start, DEFAULT_NOTIFY_START)
    fixed_end = normalize_time_value(notify_end, DEFAULT_NOTIFY_END)

    if notify_start != fixed_start or notify_end != fixed_end:
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE user_settings
                SET notify_start = ?,
                    notify_end = ?
                WHERE discord_id = ?
                """,
                (
                    fixed_start,
                    fixed_end,
                    discord_id,
                ),
            )

    return fixed_start, fixed_end

def ensure_user_and_settings(member: discord.Member) -> None:
    """
    Makes sure the user and user_settings rows exist.
    Useful if someone uses /user settings before the startup sync caught them.
    """
    ensure_user_settings_schema()

    ts = now_ts()
    discord_id = str(member.id)

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
                discord_id,
                str(member.name),
                str(member.display_name),
                ts,
                ts,
            ),
        )

        conn.execute(
            """
            INSERT OR IGNORE INTO user_settings (
                discord_id,
                timezone,
                notify_start,
                notify_end,
                notify_flightlead,
                notify_instructor,
                notify_training,
                notify_weekly_report,
                notify_operations
            )
            VALUES (?, NULL, ?, ?, 1, 0, 0, 0, 0)
            """,
            (
                discord_id,
                DEFAULT_NOTIFY_START,
                DEFAULT_NOTIFY_END,
            ),
        )

        conn.execute(
            """
            UPDATE user_settings
            SET notify_start = COALESCE(NULLIF(notify_start, ''), ?),
                notify_end = COALESCE(NULLIF(notify_end, ''), ?)
            WHERE discord_id = ?
            """,
            (
                DEFAULT_NOTIFY_START,
                DEFAULT_NOTIFY_END,
                discord_id,
            ),
        )


def get_user_settings(member: discord.Member) -> UserSettings:
    ensure_user_and_settings(member)

    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                discord_id,
                timezone,
                notify_start,
                notify_end,
                notify_flightlead,
                notify_instructor,
                notify_training,
                notify_weekly_report,
                notify_operations
            FROM user_settings
            WHERE discord_id = ?
            """,
            (str(member.id),),
        ).fetchone()

    notify_start, notify_end = repair_missing_notification_window(
        row["discord_id"],
        row["notify_start"],
        row["notify_end"],
    )

    return UserSettings(
        discord_id=row["discord_id"],
        timezone=row["timezone"],
        notify_start=notify_start,
        notify_end=notify_end,
        notify_flightlead=bool(row["notify_flightlead"]),
        notify_instructor=bool(row["notify_instructor"]),
        notify_training=bool(row["notify_training"]),
        notify_weekly_report=bool(row["notify_weekly_report"]),
        notify_operations=bool(row["notify_operations"]),
    )


def normalize_timezone_name(value: str | None) -> str | None:
    """Return a canonical, valid IANA timezone name or None."""
    text = str(value or "").strip()

    if not text:
        return None

    try:
        zone = ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return None

    return zone.key


def update_timezone(discord_id: str, timezone: str) -> bool:
    """Store a timezone only when it is a valid IANA timezone name."""
    ensure_user_settings_schema()

    normalized = normalize_timezone_name(timezone)

    if normalized is None:
        return False

    with get_connection() as conn:
        conn.execute(
            """
            UPDATE user_settings
            SET timezone = ?
            WHERE discord_id = ?
            """,
            (normalized, discord_id),
        )

    return True


def update_notification_window(discord_id: str, *, notify_start: str | None = None, notify_end: str | None = None) -> None:
    ensure_user_settings_schema()

    updates: list[str] = []
    params: list[object] = []

    if notify_start is not None:
        updates.append("notify_start = ?")
        params.append(normalize_time_value(notify_start, DEFAULT_NOTIFY_START))

    if notify_end is not None:
        updates.append("notify_end = ?")
        params.append(normalize_time_value(notify_end, DEFAULT_NOTIFY_END))

    if not updates:
        return

    params.append(discord_id)

    with get_connection() as conn:
        conn.execute(
            f"""
            UPDATE user_settings
            SET {", ".join(updates)}
            WHERE discord_id = ?
            """,
            tuple(params),
        )


def update_notification_toggles(
    discord_id: str,
    *,
    notify_flightlead: bool,
    notify_instructor: bool,
    notify_training: bool,
    notify_weekly_report: bool | None = None,
    notify_operations: bool | None = None,
) -> None:
    ensure_user_settings_schema()

    updates = [
        "notify_flightlead = ?",
        "notify_instructor = ?",
        "notify_training = ?",
    ]
    params: list[object] = [
        1 if notify_flightlead else 0,
        1 if notify_instructor else 0,
        1 if notify_training else 0,
    ]

    # Weekly Server Report is admin-managed. A None value means preserve the
    # existing database value when a normal user edits their other settings.
    if notify_weekly_report is not None:
        updates.append("notify_weekly_report = ?")
        params.append(1 if notify_weekly_report else 0)

    # OP Execution Reminders are role-gated. None preserves the existing value
    # when an ineligible user edits their other notification settings.
    if notify_operations is not None:
        updates.append("notify_operations = ?")
        params.append(1 if notify_operations else 0)

    params.append(discord_id)

    with get_connection() as conn:
        conn.execute(
            f"""
            UPDATE user_settings
            SET {", ".join(updates)}
            WHERE discord_id = ?
            """,
            tuple(params),
        )


def update_notify_weekly_report(discord_id: str, enabled: bool) -> None:
    ensure_user_settings_schema()

    with get_connection() as conn:
        conn.execute(
            """
            UPDATE user_settings
            SET notify_weekly_report = ?
            WHERE discord_id = ?
            """,
            (1 if enabled else 0, discord_id),
        )



def update_notify_operations(discord_id: str, enabled: bool) -> None:
    ensure_user_settings_schema()

    with get_connection() as conn:
        conn.execute(
            """
            UPDATE user_settings
            SET notify_operations = ?
            WHERE discord_id = ?
            """,
            (1 if enabled else 0, discord_id),
        )

def update_notify_flightlead(discord_id: str, enabled: bool) -> None:
    ensure_user_settings_schema()

    with get_connection() as conn:
        conn.execute(
            """
            UPDATE user_settings
            SET notify_flightlead = ?
            WHERE discord_id = ?
            """,
            (1 if enabled else 0, discord_id),
        )


def update_notify_instructor(discord_id: str, enabled: bool) -> None:
    ensure_user_settings_schema()

    with get_connection() as conn:
        conn.execute(
            """
            UPDATE user_settings
            SET notify_instructor = ?
            WHERE discord_id = ?
            """,
            (1 if enabled else 0, discord_id),
        )


def update_notify_training(discord_id: str, enabled: bool) -> None:
    ensure_user_settings_schema()

    with get_connection() as conn:
        conn.execute(
            """
            UPDATE user_settings
            SET notify_training = ?
            WHERE discord_id = ?
            """,
            (1 if enabled else 0, discord_id),
        )


# Backward-compatible wrappers for any older imports.
def update_notify_flight_lead(discord_id: str, enabled: bool) -> None:
    update_notify_flightlead(discord_id, enabled)


def update_notify_training_alerts(discord_id: str, enabled: bool) -> None:
    update_notify_training(discord_id, enabled)


def update_notify_request_qual(discord_id: str, enabled: bool) -> None:
    update_notify_instructor(discord_id, enabled)
