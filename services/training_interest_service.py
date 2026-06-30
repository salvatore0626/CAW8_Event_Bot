from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord

from config import TRAINING_TOPICS
from database import get_connection, ensure_user_settings_schema
from services.user_settings_service import ensure_user_and_settings


ANSI_RESET = "\u001b[0m"
ANSI_GREEN = "\u001b[32m"
ANSI_WHITE = "\u001b[37m"


def now_ts() -> int:
    return int(time.time())


@dataclass(frozen=True)
class TrainingTopic:
    key: str
    label: str


@dataclass(frozen=True)
class TrainingInterestMember:
    discord_id: str
    discord_username: str | None
    display_name: str | None
    timezone: str | None
    notify_start: str | None
    notify_end: str | None
    notify_training: bool


def configured_training_topics() -> list[TrainingTopic]:
    topics: list[TrainingTopic] = []
    seen: set[str] = set()

    for item in TRAINING_TOPICS:
        if isinstance(item, dict):
            key = str(item.get("key") or "").strip()
            label = str(item.get("label") or item.get("name") or key).strip()
        elif isinstance(item, (tuple, list)) and len(item) >= 2:
            key = str(item[0] or "").strip()
            label = str(item[1] or item[0] or "").strip()
        else:
            label = str(item or "").strip()
            key = label.casefold().replace(" ", "_")

        if not key or not label:
            continue

        normalized_key = key.casefold()

        if normalized_key in seen:
            continue

        seen.add(normalized_key)
        topics.append(TrainingTopic(key=key, label=label))

    return topics


def topic_map() -> dict[str, TrainingTopic]:
    return {
        topic.key: topic
        for topic in configured_training_topics()
    }


def topic_label(topic_key: str) -> str:
    topic = topic_map().get(str(topic_key))
    return topic.label if topic else str(topic_key)


def ensure_training_interest_schema() -> None:
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS training_interest (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id TEXT NOT NULL,
                topic_key TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,

                UNIQUE(discord_id, topic_key),
                FOREIGN KEY (discord_id) REFERENCES users(discord_id) ON DELETE CASCADE
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_training_interest_topic
            ON training_interest(topic_key)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_training_interest_discord
            ON training_interest(discord_id)
            """
        )


def ensure_member(member: discord.Member) -> None:
    ensure_user_and_settings(member)
    ensure_training_interest_schema()


def user_training_interest_keys(discord_id: str) -> set[str]:
    ensure_training_interest_schema()

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT topic_key
            FROM training_interest
            WHERE discord_id = ?
            ORDER BY topic_key ASC
            """,
            (str(discord_id),),
        ).fetchall()

    return {
        str(row["topic_key"])
        for row in rows
    }


def set_user_training_interests(discord_id: str, topic_keys: set[str]) -> None:
    ensure_training_interest_schema()

    valid_topics = {
        topic.key
        for topic in configured_training_topics()
    }
    cleaned_keys = {
        str(key)
        for key in topic_keys
        if str(key) in valid_topics
    }

    ts = now_ts()

    with get_connection() as conn:
        existing_rows = conn.execute(
            """
            SELECT topic_key
            FROM training_interest
            WHERE discord_id = ?
            """,
            (str(discord_id),),
        ).fetchall()

        existing = {
            str(row["topic_key"])
            for row in existing_rows
        }

        to_add = sorted(cleaned_keys - existing)
        to_remove = sorted(existing - cleaned_keys)

        for topic_key in to_add:
            conn.execute(
                """
                INSERT OR IGNORE INTO training_interest (
                    discord_id,
                    topic_key,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    str(discord_id),
                    topic_key,
                    ts,
                    ts,
                ),
            )

        for topic_key in to_remove:
            conn.execute(
                """
                DELETE FROM training_interest
                WHERE discord_id = ?
                  AND topic_key = ?
                """,
                (
                    str(discord_id),
                    topic_key,
                ),
            )

        for topic_key in cleaned_keys & existing:
            conn.execute(
                """
                UPDATE training_interest
                SET updated_at = ?
                WHERE discord_id = ?
                  AND topic_key = ?
                """,
                (
                    ts,
                    str(discord_id),
                    topic_key,
                ),
            )


def update_user_training_notifications(discord_id: str, enabled: bool) -> None:
    ensure_user_settings_schema()

    with get_connection() as conn:
        conn.execute(
            """
            UPDATE user_settings
            SET notify_training = ?
            WHERE discord_id = ?
            """,
            (
                1 if enabled else 0,
                str(discord_id),
            ),
        )


def get_user_training_notify(discord_id: str) -> bool:
    ensure_user_settings_schema()

    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT notify_training
            FROM user_settings
            WHERE discord_id = ?
            LIMIT 1
            """,
            (str(discord_id),),
        ).fetchone()

    if row is None:
        return False

    return bool(row["notify_training"])


def training_interest_counts() -> dict[str, int]:
    ensure_training_interest_schema()
    topics = configured_training_topics()

    counts = {
        topic.key: 0
        for topic in topics
    }

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT topic_key, COUNT(*) AS count
            FROM training_interest
            GROUP BY topic_key
            """
        ).fetchall()

    for row in rows:
        key = str(row["topic_key"])

        if key in counts:
            counts[key] = int(row["count"] or 0)

    return counts


def interested_members_for_topic(topic_key: str) -> list[TrainingInterestMember]:
    ensure_training_interest_schema()
    ensure_user_settings_schema()

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                ti.discord_id,
                u.discord_username,
                u.display_name,
                us.timezone,
                us.notify_start,
                us.notify_end,
                us.notify_training
            FROM training_interest ti
            LEFT JOIN users u
                ON u.discord_id = ti.discord_id
            LEFT JOIN user_settings us
                ON us.discord_id = ti.discord_id
            WHERE ti.topic_key = ?
            ORDER BY
                COALESCE(u.display_name, u.discord_username, ti.discord_id) COLLATE NOCASE ASC
            """,
            (str(topic_key),),
        ).fetchall()

    members: list[TrainingInterestMember] = []

    for row in rows:
        members.append(
            TrainingInterestMember(
                discord_id=str(row["discord_id"]),
                discord_username=str(row["discord_username"]) if row["discord_username"] is not None else None,
                display_name=str(row["display_name"]) if row["display_name"] is not None else None,
                timezone=str(row["timezone"]) if row["timezone"] is not None else None,
                notify_start=str(row["notify_start"]) if row["notify_start"] is not None else None,
                notify_end=str(row["notify_end"]) if row["notify_end"] is not None else None,
                notify_training=bool(row["notify_training"]),
            )
        )

    return members


def parse_notification_time(value: str | None, default: str) -> int:
    text = str(value or default).strip()

    try:
        hour_text, minute_text = text.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except (TypeError, ValueError):
        hour_text, minute_text = default.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)

    hour = max(0, min(23, hour))
    minute = max(0, min(59, minute))

    return hour * 60 + minute


def is_within_notification_window(
    *,
    timezone: str | None,
    notify_start: str | None,
    notify_end: str | None,
) -> bool:
    if not timezone:
        return False

    try:
        local_now = datetime.now(ZoneInfo(str(timezone)))
    except ZoneInfoNotFoundError:
        return False
    except Exception:
        return False

    now_minutes = local_now.hour * 60 + local_now.minute
    start_minutes = parse_notification_time(notify_start, "09:00")
    end_minutes = parse_notification_time(notify_end, "21:00")

    if start_minutes == end_minutes:
        return True

    if start_minutes < end_minutes:
        return start_minutes <= now_minutes < end_minutes

    return now_minutes >= start_minutes or now_minutes < end_minutes


def eligible_training_dm_members(topic_key: str) -> list[TrainingInterestMember]:
    members = interested_members_for_topic(topic_key)
    result: list[TrainingInterestMember] = []

    for member in members:
        if not member.notify_training:
            continue

        if not is_within_notification_window(
            timezone=member.timezone,
            notify_start=member.notify_start,
            notify_end=member.notify_end,
        ):
            continue

        result.append(member)

    return result


def signup_status_lines(selected_keys: set[str]) -> list[str]:
    lines: list[str] = []

    for topic in configured_training_topics():
        signed_up = topic.key in selected_keys
        color = ANSI_GREEN if signed_up else ANSI_WHITE
        marker = "YES" if signed_up else "NO "
        lines.append(f"{color}{topic.label}: {marker}{ANSI_RESET}")

    return lines


def roster_count_lines() -> list[str]:
    counts = training_interest_counts()
    lines: list[str] = []

    for topic in configured_training_topics():
        count = counts.get(topic.key, 0)
        color = ANSI_GREEN if count > 0 else ANSI_WHITE
        lines.append(f"{color}{topic.label}: {count}{ANSI_RESET}")

    return lines


def ansi_code_block(lines: list[str]) -> str:
    return "```ansi\n" + "\n".join(lines) + "\n```"
