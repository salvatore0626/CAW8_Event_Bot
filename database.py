import sqlite3
import time
from typing import Optional

from config import DATABASE_PATH


def now_ts() -> int:
    return int(time.time())


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn



def ensure_user_settings_schema() -> None:
    """Create or migrate user_settings to the current notification schema."""
    with get_connection() as conn:
        migrate_user_settings_schema(conn)


def migrate_user_settings_schema(conn: sqlite3.Connection) -> None:
    desired_columns = {
        "discord_id",
        "timezone",
        "notify_start",
        "notify_end",
        "notify_flightlead",
        "notify_instructor",
        "notify_training",
    }

    existing_table = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = 'user_settings'
        LIMIT 1
        """
    ).fetchone()

    if existing_table is None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_settings (
                discord_id TEXT PRIMARY KEY,
                timezone TEXT,
                notify_start TEXT NOT NULL DEFAULT '09:00',
                notify_end TEXT NOT NULL DEFAULT '21:00',
                notify_flightlead INTEGER NOT NULL DEFAULT 1,
                notify_instructor INTEGER NOT NULL DEFAULT 0,
                notify_training INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (discord_id) REFERENCES users(discord_id) ON DELETE CASCADE
            )
            """
        )
        return

    column_rows = conn.execute("PRAGMA table_info(user_settings)").fetchall()
    existing_columns = {
        row["name"]
        for row in column_rows
    }

    if desired_columns.issubset(existing_columns):
        return

    old_rows = conn.execute("SELECT * FROM user_settings").fetchall()
    old_column_names = list(existing_columns)

    conn.execute("PRAGMA foreign_keys = OFF;")
    conn.execute("ALTER TABLE user_settings RENAME TO user_settings_old")

    conn.execute(
        """
        CREATE TABLE user_settings (
            discord_id TEXT PRIMARY KEY,
            timezone TEXT,
            notify_start TEXT NOT NULL DEFAULT '09:00',
            notify_end TEXT NOT NULL DEFAULT '21:00',
            notify_flightlead INTEGER NOT NULL DEFAULT 1,
            notify_instructor INTEGER NOT NULL DEFAULT 0,
            notify_training INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (discord_id) REFERENCES users(discord_id) ON DELETE CASCADE
        )
        """
    )

    for row in old_rows:
        discord_id = row["discord_id"] if "discord_id" in old_column_names else None

        if not discord_id:
            continue

        timezone = row["timezone"] if "timezone" in old_column_names else None
        notify_start = row["notify_start"] if "notify_start" in old_column_names and row["notify_start"] else "09:00"
        notify_end = row["notify_end"] if "notify_end" in old_column_names and row["notify_end"] else "21:00"

        if "notify_flightlead" in old_column_names:
            notify_flightlead = int(row["notify_flightlead"] or 0)
        elif "notify_flight_lead" in old_column_names:
            notify_flightlead = int(row["notify_flight_lead"] or 0)
        else:
            notify_flightlead = 1

        # New defaults: instructor/training notifications default off.
        # Only preserve these if the new column already existed.
        notify_instructor = int(row["notify_instructor"] or 0) if "notify_instructor" in old_column_names else 0
        notify_training = int(row["notify_training"] or 0) if "notify_training" in old_column_names else 0

        conn.execute(
            """
            INSERT OR REPLACE INTO user_settings (
                discord_id,
                timezone,
                notify_start,
                notify_end,
                notify_flightlead,
                notify_instructor,
                notify_training
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(discord_id),
                timezone,
                str(notify_start),
                str(notify_end),
                1 if notify_flightlead else 0,
                1 if notify_instructor else 0,
                1 if notify_training else 0,
            ),
        )

    conn.execute("DROP TABLE user_settings_old")
    conn.execute("PRAGMA foreign_keys = ON;")


def init_db() -> None:
    """
    Bare minimum tables needed for startup user syncing.

    If you already created the full Air Boss schema, this will not hurt anything
    because it uses CREATE TABLE IF NOT EXISTS.
    """
    with get_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                discord_id TEXT UNIQUE,
                discord_username TEXT,
                display_name TEXT,

                rank TEXT NOT NULL DEFAULT 'Recruit',

                status TEXT NOT NULL DEFAULT 'Active',

                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,

                CHECK (status IN ('Active', 'Retired', 'MIA'))
            );

            CREATE TABLE IF NOT EXISTS user_settings (
                discord_id TEXT PRIMARY KEY,

                timezone TEXT,
                notify_start TEXT NOT NULL DEFAULT '09:00',
                notify_end TEXT NOT NULL DEFAULT '21:00',
                notify_flightlead INTEGER NOT NULL DEFAULT 1,
                notify_instructor INTEGER NOT NULL DEFAULT 0,
                notify_training INTEGER NOT NULL DEFAULT 0,

                FOREIGN KEY (discord_id) REFERENCES users(discord_id) ON DELETE CASCADE
            );


            CREATE TABLE IF NOT EXISTS training_interest (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id TEXT NOT NULL,
                topic_key TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,

                UNIQUE(discord_id, topic_key),
                FOREIGN KEY (discord_id) REFERENCES users(discord_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_training_interest_topic
            ON training_interest(topic_key);

            CREATE INDEX IF NOT EXISTS idx_training_interest_discord
            ON training_interest(discord_id);

            CREATE TABLE IF NOT EXISTS attendance (
                entry_id INTEGER PRIMARY KEY AUTOINCREMENT,

                scheduled_op_id INTEGER,
                op_template_name TEXT,
                entry_slot_index INTEGER,

                discord_id TEXT,
                user_name TEXT,

                slot TEXT,
                aircraft TEXT,

                combat_deaths INTEGER,
                landing_type TEXT,
                wires INTEGER,
                bolters INTEGER,

                attend_type TEXT,

                created_at INTEGER,
                logged_at INTEGER,
                updated_at INTEGER,

                FOREIGN KEY (discord_id) REFERENCES users(discord_id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_users_discord_id
            ON users(discord_id);

            CREATE INDEX IF NOT EXISTS idx_users_status
            ON users(status);

            CREATE INDEX IF NOT EXISTS idx_users_rank
            ON users(rank);

            CREATE INDEX IF NOT EXISTS idx_attendance_discord_id
            ON attendance(discord_id);

            CREATE INDEX IF NOT EXISTS idx_attendance_logged_at
            ON attendance(logged_at);

            CREATE TABLE IF NOT EXISTS asvab_quiz_attempts (
                attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id TEXT NOT NULL,
                discord_username TEXT,
                display_name TEXT,
                quiz_version TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Started',
                score_percent REAL,
                correct_count INTEGER NOT NULL DEFAULT 0,
                total_questions INTEGER NOT NULL,
                current_question_index INTEGER NOT NULL DEFAULT 0,
                question_order_json TEXT NOT NULL DEFAULT '[]',
                answer_order_json TEXT NOT NULL DEFAULT '{}',
                answers_json TEXT NOT NULL DEFAULT '{}',
                category_scores_json TEXT NOT NULL DEFAULT '[]',
                started_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                completed_at INTEGER,
                updated_at INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_asvab_quiz_attempts_discord_status
            ON asvab_quiz_attempts (discord_id, status);

            CREATE INDEX IF NOT EXISTS idx_asvab_quiz_attempts_expires
            ON asvab_quiz_attempts (status, expires_at);

            CREATE INDEX IF NOT EXISTS idx_asvab_quiz_attempts_completed
            ON asvab_quiz_attempts (discord_id, completed_at);

            """
        )

    ensure_user_settings_schema()


def has_attendance_within_days(discord_id: str, days: int = 500) -> bool:
    cutoff = now_ts() - (days * 24 * 60 * 60)

    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM attendance
            WHERE discord_id = ?
              AND COALESCE(logged_at, created_at, updated_at, 0) >= ?
            LIMIT 1
            """,
            (discord_id, cutoff),
        ).fetchone()

    return row is not None


def calculate_member_status(discord_id: str, is_in_server: bool = True) -> str:
    """
    User status rules:
    - MIA = no longer in the server
    - Active = has attendance in the last 500 days
    - Retired = still in the server, but no attendance in the last 500 days
    """
    if not is_in_server:
        return "MIA"

    if has_attendance_within_days(discord_id, 500):
        return "Active"

    return "Retired"


def upsert_user(
    *,
    discord_id: str,
    discord_username: Optional[str],
    display_name: Optional[str],
    rank: str,
    status: str,
) -> None:
    ts = now_ts()

    with get_connection() as conn:
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
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET
                discord_username = excluded.discord_username,
                display_name = excluded.display_name,
                rank = excluded.rank,
                status = excluded.status,
                updated_at = excluded.updated_at
            """,
            (
                discord_id,
                discord_username,
                display_name,
                rank,
                status,
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
                notify_training
            )
            VALUES (?, NULL, '09:00', '21:00', 1, 0, 0)
            """,
            (discord_id,),
        )


def update_user_rank(
    *,
    discord_id: str,
    rank: str,
) -> None:
    ts = now_ts()

    with get_connection() as conn:
        conn.execute(
            """
            UPDATE users
            SET rank = ?,
                updated_at = ?
            WHERE discord_id = ?
            """,
            (rank, ts, discord_id),
        )


def mark_users_not_in_server_as_mia(current_member_ids: set[str]) -> int:
    """
    Any user in the database but not currently in the Discord server
    becomes MIA.
    """
    ts = now_ts()

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT discord_id
            FROM users
            WHERE discord_id IS NOT NULL
            """
        ).fetchall()

        changed = 0

        for row in rows:
            discord_id = row["discord_id"]

            if discord_id not in current_member_ids:
                conn.execute(
                    """
                    UPDATE users
                    SET status = 'MIA',
                        updated_at = ?
                    WHERE discord_id = ?
                    """,
                    (ts, discord_id),
                )
                changed += 1

    return changed