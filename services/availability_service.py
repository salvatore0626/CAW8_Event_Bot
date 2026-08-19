from __future__ import annotations

import json
import struct
import time
import zlib
from dataclasses import dataclass
from datetime import datetime, time as datetime_time, timedelta
from typing import Any
from functools import lru_cache
from importlib import resources
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from services.user_settings_service import safe_zoneinfo
from database import get_connection, ensure_user_settings_schema

try:
    from config import AVAILABILITY_HEATMAP_TIMEZONE
except ImportError:
    try:
        from config import SCHEDULE_DEFAULT_TIMEZONE as AVAILABILITY_HEATMAP_TIMEZONE
    except ImportError:
        AVAILABILITY_HEATMAP_TIMEZONE = "America/New_York"


DAY_NAMES = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]

STATUS_PENDING = "pending"
STATUS_COMPLETE = "complete"


TIMEZONE_REGIONS = [
    "US / Canada",
    "Central America / Caribbean",
    "South America",
    "Europe",
    "Africa",
    "Middle East",
    "Asia",
    "Oceania",
    "UTC / Other",
]

_US_CANADA_CODES = {"US", "CA"}
_CENTRAL_AMERICA_CARIBBEAN_CODES = {
    "MX", "BZ", "CR", "SV", "GT", "HN", "NI", "PA",
    "AG", "AI", "AW", "BB", "BL", "BM", "BQ", "BS", "CU",
    "CW", "DM", "DO", "GD", "GP", "HT", "JM", "KN", "KY",
    "LC", "MF", "MQ", "MS", "PR", "SX", "TC", "TT", "VC", "VG", "VI",
}
_SOUTH_AMERICA_CODES = {
    "AR", "BO", "BR", "CL", "CO", "EC", "FK", "GF", "GY",
    "PY", "PE", "SR", "UY", "VE",
}
_MIDDLE_EAST_CODES = {
    "AE", "BH", "CY", "IL", "IQ", "IR", "JO", "KW", "LB",
    "OM", "PS", "QA", "SA", "SY", "TR", "YE",
}
_EUROPE_CODES = {
    "AD", "AL", "AT", "AX", "BA", "BE", "BG", "BY", "CH", "CZ",
    "DE", "DK", "EE", "ES", "FI", "FO", "FR", "GB", "GG", "GI",
    "GR", "HR", "HU", "IE", "IM", "IS", "IT", "JE", "LI", "LT",
    "LU", "LV", "MC", "MD", "ME", "MK", "MT", "NL", "NO", "PL",
    "PT", "RO", "RS", "RU", "SE", "SI", "SJ", "SK", "SM", "UA", "VA",
}
_AFRICA_CODES = {
    "AO", "BF", "BI", "BJ", "BW", "CD", "CF", "CG", "CI", "CM",
    "CV", "DJ", "DZ", "EG", "EH", "ER", "ET", "GA", "GH", "GM",
    "GN", "GQ", "GW", "KE", "KM", "LR", "LS", "LY", "MA", "MG",
    "ML", "MR", "MU", "MW", "MZ", "NA", "NE", "NG", "RE", "RW",
    "SC", "SD", "SH", "SL", "SN", "SO", "SS", "ST", "SZ", "TD",
    "TG", "TN", "TZ", "UG", "YT", "ZA", "ZM", "ZW",
}
_OCEANIA_CODES = {
    "AS", "AU", "CK", "FJ", "FM", "GU", "KI", "MH", "MP", "NC",
    "NF", "NR", "NU", "NZ", "PF", "PG", "PN", "PW", "SB", "TK",
    "TO", "TV", "UM", "VU", "WF", "WS",
}


@lru_cache(maxsize=1)
def _timezone_country_map() -> dict[str, set[str]]:
    """Load IANA zone-to-country mappings from tzdata when available."""
    mapping: dict[str, set[str]] = {}
    text = ""

    try:
        zone_tab = resources.files("tzdata.zoneinfo").joinpath("zone.tab")
        text = zone_tab.read_text(encoding="utf-8")
    except Exception:
        for candidate in (
            Path("/usr/share/zoneinfo/zone.tab"),
            Path("/usr/share/lib/zoneinfo/tab/zone_sun.tab"),
        ):
            try:
                if candidate.is_file():
                    text = candidate.read_text(encoding="utf-8")
                    break
            except OSError:
                continue

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        country_text, _coordinates, zone_name = parts[:3]
        countries = {code.strip().upper() for code in country_text.split(",") if code.strip()}
        if countries:
            mapping.setdefault(zone_name.strip(), set()).update(countries)

    return mapping


def timezone_region(timezone_name: str | None) -> str:
    """Return the broad admin-report region for an IANA timezone name."""
    value = str(timezone_name or "").strip()
    if not value:
        return "UTC / Other"

    upper = value.upper()
    if upper in {"UTC", "GMT", "ETC/UTC", "ETC/GMT", "UCT", "ZULU", "UNIVERSAL"}:
        return "UTC / Other"

    countries = _timezone_country_map().get(value, set())
    if countries & _US_CANADA_CODES:
        return "US / Canada"
    if countries & _CENTRAL_AMERICA_CARIBBEAN_CODES:
        return "Central America / Caribbean"
    if countries & _SOUTH_AMERICA_CODES:
        return "South America"
    if countries & _MIDDLE_EAST_CODES:
        return "Middle East"
    if countries & _EUROPE_CODES:
        return "Europe"
    if countries & _AFRICA_CODES:
        return "Africa"
    if countries & _OCEANIA_CODES:
        return "Oceania"
    if countries:
        return "Asia"

    # Portable fallbacks for systems where zone.tab is unavailable.
    if value.startswith("Europe/"):
        return "Europe"
    if value.startswith("Africa/"):
        return "Africa"
    if value.startswith(("Australia/", "Pacific/", "Antarctica/")):
        return "Oceania"
    if value.startswith("Asia/"):
        middle_east_city_names = {
            "Aden", "Amman", "Baghdad", "Bahrain", "Beirut", "Damascus",
            "Dubai", "Famagusta", "Gaza", "Hebron", "Istanbul", "Jerusalem",
            "Kuwait", "Muscat", "Nicosia", "Qatar", "Riyadh", "Tehran",
            "Tel_Aviv",
        }
        city = value.split("/", 1)[1]
        return "Middle East" if city in middle_east_city_names else "Asia"
    if value.startswith("Indian/"):
        return "Africa"
    if value.startswith("America/"):
        return "UTC / Other"
    return "UTC / Other"




@dataclass(frozen=True)
class AvailabilityHeatmapReport:
    display_timezone: str
    selected_day: int
    completed_users: int
    skipped_users: int
    generated_at: int
    day_selected_counts: list[int]
    hourly_counts: list[list[int]]
    timezone_counts: dict[str, int]
    submitted_discord_ids: list[str]
    def day_percent(self, day: int) -> int:
        if self.completed_users <= 0:
            return 0
        return round((self.day_selected_counts[day] / self.completed_users) * 100)

    def hour_percent(self, day: int, hour: int) -> int:
        if self.completed_users <= 0:
            return 0
        return round((self.hourly_counts[day][hour] / self.completed_users) * 100)

    def top_hours(self, day: int, *, limit: int = 8, minimum_percent: int = 20) -> list[tuple[int, int, int]]:
        values: list[tuple[int, int, int]] = []

        for hour in range(24):
            count = self.hourly_counts[day][hour]
            percent = self.hour_percent(day, hour)
            if percent < minimum_percent:
                continue
            values.append((hour, count, percent))

        values.sort(key=lambda item: (-item[2], -item[1], item[0]))
        return values[:limit]

    def top_hours_any_day(self, *, limit: int = 10, minimum_percent: int = 1) -> list[tuple[int, int, int, int]]:
        values: list[tuple[int, int, int, int]] = []

        for day in range(7):
            for hour in range(24):
                count = self.hourly_counts[day][hour]
                percent = self.hour_percent(day, hour)
                if percent < minimum_percent:
                    continue
                values.append((day, hour, count, percent))

        values.sort(key=lambda item: (-item[3], -item[2], item[0], item[1]))
        return values[:limit]

    def top_days(self, *, limit: int = 5, minimum_percent: int = 1) -> list[tuple[int, int, int]]:
        values: list[tuple[int, int, int]] = []

        for day in range(7):
            count = self.day_selected_counts[day]
            percent = self.day_percent(day)
            if percent < minimum_percent:
                continue
            values.append((day, count, percent))

        values.sort(key=lambda item: (-item[2], -item[1], item[0]))
        return values[:limit]

    def timezone_ratios(self, *, limit: int = 12) -> list[tuple[str, int, int]]:
        values: list[tuple[str, int, int]] = []

        for timezone_name, count in self.timezone_counts.items():
            percent = 0 if self.completed_users <= 0 else round((count / self.completed_users) * 100)
            values.append((timezone_name, count, percent))

        values.sort(key=lambda item: (-item[1], item[0]))
        return values[:limit]


@dataclass(frozen=True)
class AvailabilityDay:
    discord_id: str
    day_of_week: int
    status: str
    windows: list[tuple[int, int]]
    updated_at: int


def now_ts() -> int:
    return int(time.time())


def ensure_availability_schema() -> None:
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS availability_days (
                availability_day_id INTEGER PRIMARY KEY AUTOINCREMENT,

                discord_id TEXT NOT NULL,
                day_of_week INTEGER NOT NULL,

                status TEXT NOT NULL DEFAULT 'pending',
                windows_json TEXT NOT NULL DEFAULT '[]',

                updated_at INTEGER NOT NULL,

                UNIQUE (discord_id, day_of_week),
                CHECK (day_of_week BETWEEN 0 AND 6),
                CHECK (status IN ('pending', 'complete')),

                FOREIGN KEY (discord_id) REFERENCES users(discord_id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_availability_days_discord
            ON availability_days(discord_id)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_availability_days_status
            ON availability_days(status)
            """
        )


def parse_windows_json(value: Any) -> list[tuple[int, int]]:
    try:
        raw = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []

    if not isinstance(raw, list):
        return []

    windows: list[tuple[int, int]] = []

    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue

        try:
            start = int(item[0])
            end = int(item[1])
        except (TypeError, ValueError):
            continue

        if start < 0 or start > 1439:
            continue

        if end <= start:
            end += 1440

        if end <= start or end > 2880:
            continue

        windows.append((start, end))

    return normalize_windows(windows)


def normalize_windows(windows: list[tuple[int, int]]) -> list[tuple[int, int]]:
    cleaned: list[tuple[int, int]] = []

    for start, end in windows:
        try:
            start = int(start)
            end = int(end)
        except (TypeError, ValueError):
            continue

        if start < 0 or start > 1439:
            continue

        if end <= start:
            end += 1440

        if end <= start or end > 2880:
            continue

        cleaned.append((start, end))

    cleaned.sort(key=lambda window: (window[0], window[1]))

    output: list[tuple[int, int]] = []
    for start, end in cleaned:
        if output and start <= output[-1][1]:
            # Overlapping or touching windows are merged so the heatmap does not
            # double-count a user who entered overlapping ranges for the same day.
            prev_start, prev_end = output[-1]
            output[-1] = (prev_start, max(prev_end, end))
        else:
            output.append((start, end))

    return output


def windows_to_json(windows: list[tuple[int, int]]) -> str:
    normalized = normalize_windows(windows)
    return json.dumps([[start, end] for start, end in normalized], separators=(",", ":"))


def ensure_user_availability_days(discord_id: str) -> None:
    ensure_availability_schema()
    ts = now_ts()

    with get_connection() as conn:
        for day in range(7):
            conn.execute(
                """
                INSERT OR IGNORE INTO availability_days (
                    discord_id,
                    day_of_week,
                    status,
                    windows_json,
                    updated_at
                )
                VALUES (?, ?, 'pending', '[]', ?)
                """,
                (str(discord_id), day, ts),
            )


def get_availability_days(discord_id: str) -> list[AvailabilityDay]:
    ensure_user_availability_days(str(discord_id))

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                discord_id,
                day_of_week,
                status,
                windows_json,
                updated_at
            FROM availability_days
            WHERE discord_id = ?
            ORDER BY day_of_week ASC
            """,
            (str(discord_id),),
        ).fetchall()

    return [
        AvailabilityDay(
            discord_id=str(row["discord_id"]),
            day_of_week=int(row["day_of_week"]),
            status=str(row["status"] or STATUS_PENDING),
            windows=parse_windows_json(row["windows_json"]),
            updated_at=int(row["updated_at"] or 0),
        )
        for row in rows
    ]


def reset_all_availability_to_pending() -> int:
    """Future admin command helper: keep saved windows but require reconfirmation."""
    ensure_availability_schema()
    ts = now_ts()

    with get_connection() as conn:
        cursor = conn.execute(
            """
            UPDATE availability_days
            SET status = 'pending',
                updated_at = ?
            """,
            (ts,),
        )
        return int(cursor.rowcount or 0)




def reset_user_availability(discord_id: str) -> None:
    """Clear one user's saved availability and mark all days pending."""
    ensure_user_availability_days(str(discord_id))
    ts = now_ts()

    with get_connection() as conn:
        conn.execute(
            """
            UPDATE availability_days
            SET status = 'pending',
                windows_json = '[]',
                updated_at = ?
            WHERE discord_id = ?
            """,
            (ts, str(discord_id)),
        )


def save_full_availability(
    discord_id: str,
    *,
    windows_by_day: dict[int, list[tuple[int, int]]],
) -> None:
    """Save all seven day rows as complete.

    A complete row with an empty windows list means the user confirmed they are
    unavailable that day.
    """
    ensure_user_availability_days(str(discord_id))
    ts = now_ts()

    with get_connection() as conn:
        for day in range(7):
            windows = normalize_windows(windows_by_day.get(day, []))
            conn.execute(
                """
                UPDATE availability_days
                SET status = 'complete',
                    windows_json = ?,
                    updated_at = ?
                WHERE discord_id = ?
                  AND day_of_week = ?
                """,
                (
                    windows_to_json(windows),
                    ts,
                    str(discord_id),
                    day,
                ),
            )


def completed_day_count(days: list[AvailabilityDay]) -> int:
    return sum(1 for day in days if day.status == STATUS_COMPLETE)


def is_complete(days: list[AvailabilityDay]) -> bool:
    return len(days) == 7 and completed_day_count(days) == 7


def minutes_to_time_label(minutes: int) -> str:
    minutes = int(minutes)
    day_offset = minutes // 1440
    minutes = minutes % 1440
    hour = minutes // 60
    minute = minutes % 60
    suffix = f"(+{day_offset})" if day_offset else ""
    return f"{hour:02d}:{minute:02d}{suffix}"


def window_to_text(window: tuple[int, int]) -> str:
    start, end = window
    return f"{minutes_to_time_label(start)} -> {minutes_to_time_label(end)}"


def windows_to_text(windows: list[tuple[int, int]]) -> str:
    normalized = normalize_windows(windows)

    if not normalized:
        return "Unavailable"

    return ", ".join(window_to_text(window) for window in normalized)


# ============================================================
# Availability heatmap image helpers
# ============================================================

HEATMAP_FONT_3X5: dict[str, list[str]] = {
    "0": ["111", "101", "101", "101", "111"],
    "1": ["010", "110", "010", "010", "111"],
    "2": ["111", "001", "111", "100", "111"],
    "3": ["111", "001", "111", "001", "111"],
    "4": ["101", "101", "111", "001", "001"],
    "5": ["111", "100", "111", "001", "111"],
    "6": ["111", "100", "111", "101", "111"],
    "7": ["111", "001", "010", "010", "010"],
    "8": ["111", "101", "111", "101", "111"],
    "9": ["111", "101", "111", "001", "111"],
    "A": ["010", "101", "111", "101", "101"],
    "B": ["110", "101", "110", "101", "110"],
    "C": ["111", "100", "100", "100", "111"],
    "D": ["110", "101", "101", "101", "110"],
    "E": ["111", "100", "110", "100", "111"],
    "F": ["111", "100", "110", "100", "100"],
    "G": ["111", "100", "101", "101", "111"],
    "H": ["101", "101", "111", "101", "101"],
    "I": ["111", "010", "010", "010", "111"],
    "J": ["001", "001", "001", "101", "111"],
    "K": ["101", "101", "110", "101", "101"],
    "L": ["100", "100", "100", "100", "111"],
    "M": ["101", "111", "111", "101", "101"],
    "N": ["101", "111", "111", "111", "101"],
    "O": ["111", "101", "101", "101", "111"],
    "P": ["111", "101", "111", "100", "100"],
    "Q": ["111", "101", "101", "111", "001"],
    "R": ["111", "101", "111", "110", "101"],
    "S": ["111", "100", "111", "001", "111"],
    "T": ["111", "010", "010", "010", "010"],
    "U": ["101", "101", "101", "101", "111"],
    "V": ["101", "101", "101", "101", "010"],
    "W": ["101", "101", "111", "111", "101"],
    "X": ["101", "101", "010", "101", "101"],
    "Y": ["101", "101", "010", "010", "010"],
    "Z": ["111", "001", "010", "100", "111"],
    " ": ["000", "000", "000", "000", "000"],
    ".": ["000", "000", "000", "000", "010"],
    "-": ["000", "000", "111", "000", "000"],
    "/": ["001", "001", "010", "100", "100"],
    ":": ["000", "010", "000", "010", "000"],
    "%": ["101", "001", "010", "100", "101"],
    "(": ["010", "100", "100", "100", "010"],
    ")": ["010", "001", "001", "001", "010"],
    "+": ["000", "010", "111", "010", "000"],
    "#": ["101", "111", "101", "111", "101"],
}


def _clamp_color(value: int) -> int:
    return max(0, min(255, int(value)))


def _blend_color(
    low: tuple[int, int, int],
    high: tuple[int, int, int],
    amount: float,
) -> tuple[int, int, int]:
    amount = max(0.0, min(1.0, float(amount)))
    return (
        _clamp_color(low[0] + (high[0] - low[0]) * amount),
        _clamp_color(low[1] + (high[1] - low[1]) * amount),
        _clamp_color(low[2] + (high[2] - low[2]) * amount),
    )


def _heatmap_color(percent: int | float) -> tuple[int, int, int]:
    """Availability color bands.

    0-10% stays white. Then cells step through red, yellow, green,
    and blue as availability gets stronger.
    """
    percent = max(0.0, min(100.0, float(percent)))

    if percent <= 10.0:
        return (255, 255, 255)   # white

    if percent <= 30.0:
        return (220, 38, 38)     # red

    if percent <= 50.0:
        return (245, 158, 11)    # yellow

    if percent < 75.0:
        return (34, 197, 94)     # green

    return (37, 99, 235)         # blue


def _make_blank_canvas(width: int, height: int, color: tuple[int, int, int]) -> bytearray:
    r, g, b = color
    return bytearray([r, g, b] * width * height)


def _set_pixel(
    pixels: bytearray,
    width: int,
    height: int,
    x: int,
    y: int,
    color: tuple[int, int, int],
) -> None:
    if x < 0 or y < 0 or x >= width or y >= height:
        return
    index = (y * width + x) * 3
    pixels[index:index + 3] = bytes(color)


def _draw_rect(
    pixels: bytearray,
    width: int,
    height: int,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    color: tuple[int, int, int],
) -> None:
    x_start = max(0, min(x0, x1))
    x_end = min(width - 1, max(x0, x1))
    y_start = max(0, min(y0, y1))
    y_end = min(height - 1, max(y0, y1))
    for y in range(y_start, y_end + 1):
        row = y * width * 3
        for x in range(x_start, x_end + 1):
            index = row + x * 3
            pixels[index:index + 3] = bytes(color)


def _draw_circle(
    pixels: bytearray,
    width: int,
    height: int,
    cx: int,
    cy: int,
    radius: int,
    color: tuple[int, int, int],
) -> None:
    radius = max(0, int(radius))
    radius_sq = radius * radius

    for y in range(cy - radius, cy + radius + 1):
        if y < 0 or y >= height:
            continue
        for x in range(cx - radius, cx + radius + 1):
            if x < 0 or x >= width:
                continue
            if (x - cx) * (x - cx) + (y - cy) * (y - cy) <= radius_sq:
                index = (y * width + x) * 3
                pixels[index:index + 3] = bytes(color)


def _draw_box(
    pixels: bytearray,
    width: int,
    height: int,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    outline: tuple[int, int, int],
    fill: tuple[int, int, int],
) -> None:
    _draw_rect(pixels, width, height, x0, y0, x1, y1, fill)
    _draw_rect(pixels, width, height, x0, y0, x1, y0 + 1, outline)
    _draw_rect(pixels, width, height, x0, y1 - 1, x1, y1, outline)
    _draw_rect(pixels, width, height, x0, y0, x0 + 1, y1, outline)
    _draw_rect(pixels, width, height, x1 - 1, y0, x1, y1, outline)


def _draw_text(
    pixels: bytearray,
    width: int,
    height: int,
    x: int,
    y: int,
    text: str,
    color: tuple[int, int, int],
    *,
    scale: int = 2,
) -> None:
    cursor_x = x
    for char in str(text).upper():
        glyph = HEATMAP_FONT_3X5.get(char, HEATMAP_FONT_3X5[" "])
        for row_index, row in enumerate(glyph):
            for col_index, value in enumerate(row):
                if value != "1":
                    continue
                _draw_rect(
                    pixels,
                    width,
                    height,
                    cursor_x + col_index * scale,
                    y + row_index * scale,
                    cursor_x + (col_index + 1) * scale - 1,
                    y + (row_index + 1) * scale - 1,
                    color,
                )
        cursor_x += 4 * scale


def _text_width(text: str, *, scale: int = 2) -> int:
    return len(str(text)) * 4 * scale


def _encode_png_rgb(width: int, height: int, pixels: bytearray) -> bytes:
    def chunk(chunk_type: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
        return (
            struct.pack(">I", len(data))
            + chunk_type
            + data
            + struct.pack(">I", crc)
        )

    raw_rows: list[bytes] = []
    row_bytes = width * 3
    for y in range(height):
        start = y * row_bytes
        raw_rows.append(b"\x00" + bytes(pixels[start:start + row_bytes]))
    raw = b"".join(raw_rows)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def _load_completed_availability_rows() -> list[tuple[str, str, int, list[tuple[int, int]]]]:
    ensure_availability_schema()
    ensure_user_settings_schema()

    with get_connection() as conn:
        rows = conn.execute(
            """
            WITH complete_users AS (
                SELECT discord_id
                FROM availability_days
                GROUP BY discord_id
                HAVING COUNT(*) = 7
                   AND SUM(CASE WHEN status = 'complete' THEN 1 ELSE 0 END) = 7
            )
            SELECT
                ad.discord_id,
                us.timezone,
                ad.day_of_week,
                ad.windows_json
            FROM availability_days ad
            JOIN complete_users cu
                ON cu.discord_id = ad.discord_id
            JOIN user_settings us
                ON us.discord_id = ad.discord_id
            WHERE NULLIF(TRIM(us.timezone), '') IS NOT NULL
            ORDER BY ad.discord_id ASC,
                     ad.day_of_week ASC
            """
        ).fetchall()

    return [
        (
            str(row["discord_id"]),
            str(row["timezone"]),
            int(row["day_of_week"]),
            parse_windows_json(row["windows_json"]),
        )
        for row in rows
    ]


def _reference_monday(display_tz: ZoneInfo) -> datetime:
    now = datetime.now(display_tz)
    monday_date = (now - timedelta(days=now.weekday())).date()
    return datetime.combine(monday_date, datetime_time.min, tzinfo=display_tz)


def _add_interval_hour_buckets(
    bucket_set: set[tuple[int, int]],
    *,
    start_dt: datetime,
    end_dt: datetime,
    display_tz: ZoneInfo,
) -> None:
    display_start = start_dt.astimezone(display_tz)
    display_end = end_dt.astimezone(display_tz)

    cursor = display_start.replace(minute=0, second=0, microsecond=0)
    while cursor < display_end:
        next_hour = cursor + timedelta(hours=1)
        if max(cursor, display_start) < min(next_hour, display_end):
            bucket_set.add((cursor.weekday(), cursor.hour))
        cursor = next_hour




def build_availability_heatmap_report(
    *,
    selected_day: int = 0,
    display_timezone: str | None = None,
    timezone_filter: set[str] | None = None,
    discord_id_filter: set[str] | None = None,
) -> AvailabilityHeatmapReport:
    """Summarize completed availability responses into weekday/hour buckets.

    Windows are stored in each user's local timezone. This converts each window
    into one display timezone, then counts one user at most once per hour bucket.
    """
    selected_day = max(0, min(6, int(selected_day)))
    display_tz = safe_zoneinfo(display_timezone)
    display_timezone = getattr(display_tz, "key", "America/Chicago")

    monday = _reference_monday(display_tz)
    raw_rows = _load_completed_availability_rows()
    timezone_filter = {str(value) for value in timezone_filter or set() if str(value).strip()}
    discord_id_filter = {str(value) for value in discord_id_filter or set() if str(value).strip()}

    rows_by_user: dict[str, list[tuple[str, int, list[tuple[int, int]]]]] = {}
    for discord_id, timezone_name, day_of_week, windows in raw_rows:
        rows_by_user.setdefault(discord_id, []).append((timezone_name, day_of_week, windows))

    day_selected_counts = [0 for _ in range(7)]
    hourly_counts = [[0 for _hour in range(24)] for _day in range(7)]
    timezone_counts: dict[str, int] = {}
    submitted_discord_ids: list[str] = []
    completed_users = 0
    skipped_users = 0

    for _discord_id, user_rows in rows_by_user.items():
        if discord_id_filter and str(_discord_id) not in discord_id_filter:
            continue

        if len(user_rows) != 7:
            skipped_users += 1
            continue

        timezone_name = user_rows[0][0]

        region_name = timezone_region(timezone_name)
        if timezone_filter and region_name not in timezone_filter:
            continue

        user_tz = safe_zoneinfo(timezone_name)

        completed_users += 1
        submitted_discord_ids.append(str(_discord_id))
        timezone_counts[region_name] = timezone_counts.get(region_name, 0) + 1
        user_buckets: set[tuple[int, int]] = set()

        for _timezone_name, local_day, windows in user_rows:
            if windows:
                day_selected_counts[local_day] += 1

            local_day_date = (monday + timedelta(days=local_day)).date()
            local_midnight = datetime.combine(local_day_date, datetime_time.min, tzinfo=user_tz)

            for start_minute, end_minute in windows:
                start_dt = local_midnight + timedelta(minutes=start_minute)
                end_dt = local_midnight + timedelta(minutes=end_minute)
                _add_interval_hour_buckets(
                    user_buckets,
                    start_dt=start_dt,
                    end_dt=end_dt,
                    display_tz=display_tz,
                )

        for day, hour in user_buckets:
            if 0 <= day <= 6 and 0 <= hour <= 23:
                hourly_counts[day][hour] += 1

    return AvailabilityHeatmapReport(
        display_timezone=display_timezone,
        selected_day=selected_day,
        completed_users=completed_users,
        skipped_users=skipped_users,
        generated_at=now_ts(),
        day_selected_counts=day_selected_counts,
        hourly_counts=hourly_counts,
        timezone_counts=timezone_counts,
        submitted_discord_ids=submitted_discord_ids,
    )


def render_availability_heatmap_png(report: AvailabilityHeatmapReport) -> bytes:
    width = 1100
    height = 720
    pixels = _make_blank_canvas(width, height, (255, 255, 255))

    dark = (17, 24, 39)
    text = (55, 65, 81)
    muted = (107, 114, 128)
    border = (209, 213, 219)
    panel = (249, 250, 251)
    green = (22, 163, 74)
    red = (220, 38, 38)
    pale_green = (220, 252, 231)
    dark_green = (20, 83, 45)

    selected_day = report.selected_day
    selected_day_name = DAY_NAMES[selected_day]
    selected_day_percent = report.day_percent(selected_day)
    max_hour_count = max(report.hourly_counts[selected_day] or [0])

    _draw_text(pixels, width, height, 38, 28, "AVAILABILITY HEATMAP", dark, scale=5)
    _draw_text(pixels, width, height, 42, 78, f"{selected_day_name} OVERVIEW", muted, scale=3)
    _draw_text(
        pixels,
        width,
        height,
        735,
        34,
        f"FORMS SUBMITTED: {report.completed_users}",
        text,
        scale=2,
    )
    _draw_text(
        pixels,
        width,
        height,
        735,
        58,
        f"TZ {report.display_timezone}",
        text,
        scale=2,
    )

    # Monday/hour heat strip.
    card_x0, card_y0, card_x1, card_y1 = 32, 125, 1068, 345
    _draw_box(pixels, width, height, card_x0, card_y0, card_x1, card_y1, border, panel)
    _draw_text(pixels, width, height, card_x0 + 22, card_y0 + 22, f"{selected_day_name} AVAILABILITY", dark, scale=3)
    left = card_x0 + 22
    top = card_y0 + 72
    cell_gap = 4
    cell_w = (card_x1 - card_x0 - 44 - cell_gap * 23) // 24
    cell_h = 72

    for hour in range(24):
        count = report.hourly_counts[selected_day][hour]
        percent = report.hour_percent(selected_day, hour)
        color = _heatmap_color(percent)
        x0 = left + hour * (cell_w + cell_gap)
        y0 = top
        _draw_rect(pixels, width, height, x0, y0, x0 + cell_w, y0 + cell_h, color)
        label = f"{hour:02d}"
        _draw_text(
            pixels,
            width,
            height,
            x0 + cell_w // 2 - _text_width(label, scale=1) // 2,
            y0 + cell_h + 12,
            label,
            text,
            scale=1,
        )
        if percent >= 50:
            pct_label = f"{percent}%"
            _draw_text(
                pixels,
                width,
                height,
                x0 + cell_w // 2 - _text_width(pct_label, scale=1) // 2,
                y0 + cell_h // 2 - 3,
                pct_label,
                (255, 255, 255),
                scale=1,
            )

    _draw_text(
        pixels,
        width,
        height,
        card_x1 - 255,
        card_y1 - 34,
        f"{selected_day_name} SELECTED {selected_day_percent}%",
        _heatmap_color(selected_day_percent) if selected_day_percent > 10 else muted,
        scale=2,
    )

    # Days selected section.
    days_y = 392
    _draw_text(pixels, width, height, 38, days_y, "DAYS SELECTED", dark, scale=3)
    tile_w = 142
    tile_h = 72
    tile_gap = 10
    tile_y = days_y + 36

    for day in range(7):
        x0 = 38 + day * (tile_w + tile_gap)
        pct = report.day_percent(day)
        outline = green if day == selected_day else border
        _draw_box(pixels, width, height, x0, tile_y, x0 + tile_w, tile_y + tile_h, outline, (255, 255, 255))
        _draw_text(pixels, width, height, x0 + 16, tile_y + 14, DAY_NAMES[day][:3], dark, scale=2)
        _draw_text(
            pixels,
            width,
            height,
            x0 + tile_w - _text_width(f"{pct}%", scale=3) - 14,
            tile_y + 34,
            f"{pct}%",
            _heatmap_color(pct) if pct > 10 else muted,
            scale=3,
        )

    # Top hours list.
    top_y = 552
    _draw_text(pixels, width, height, 38, top_y, f"TOP 8 {selected_day_name} HOURS", dark, scale=3)
    note = "SHOWING HOURS 20% OR HIGHER" if selected_day_percent >= 20 else "DAY IS BELOW 20% OVERALL"
    _draw_text(pixels, width, height, 38, top_y + 28, note, muted, scale=2)

    top_hours = report.top_hours(selected_day, limit=8, minimum_percent=20) if selected_day_percent >= 20 else []
    if not top_hours:
        _draw_text(pixels, width, height, 42, top_y + 70, "NO HOURS OVER 20% YET", red, scale=3)
    else:
        list_x = 38
        list_y = top_y + 62
        col_w = 510
        row_h = 30
        for index, (hour, count, percent) in enumerate(top_hours):
            col = 0 if index < 4 else 1
            row = index if index < 4 else index - 4
            x0 = list_x + col * col_w
            y0 = list_y + row * row_h
            _draw_text(pixels, width, height, x0, y0, f"#{index + 1}", green, scale=2)
            _draw_text(pixels, width, height, x0 + 46, y0, f"{hour:02d}:00", dark, scale=2)
            bar_w = max(12, int(230 * (percent / 100)))
            _draw_rect(pixels, width, height, x0 + 140, y0 + 3, x0 + 140 + bar_w, y0 + 15, green)
            _draw_text(pixels, width, height, x0 + 390, y0, f"{percent}%", green, scale=2)
            _draw_text(pixels, width, height, x0 + 452, y0, f"({count})", muted, scale=2)

    return _encode_png_rgb(width, height, pixels)


def render_availability_overview_heatmap_png(report: AvailabilityHeatmapReport) -> bytes:
    """Render the full weekly availability overview as 7 day rows x 24 hour columns."""
    width = 1180
    height = 560
    pixels = _make_blank_canvas(width, height, (255, 255, 255))

    dark = (17, 24, 39)
    text = (55, 65, 81)
    muted = (107, 114, 128)
    border = (209, 213, 219)
    panel = (249, 250, 251)
    green = (22, 163, 74)
    red = (220, 38, 38)
    pale_green = (220, 252, 231)
    dark_green = (20, 83, 45)

    max_hour_count = max((count for day_counts in report.hourly_counts for count in day_counts), default=0)

    _draw_text(pixels, width, height, 38, 28, "AVAILABILITY HEATMAP", dark, scale=5)
    _draw_text(pixels, width, height, 42, 78, "WEEKLY OVERVIEW", muted, scale=3)
    _draw_text(pixels, width, height, 790, 34, f"FORMS SUBMITTED: {report.completed_users}", text, scale=2)
    _draw_text(pixels, width, height, 790, 58, f"TZ {report.display_timezone}", text, scale=2)

    card_x0, card_y0, card_x1, card_y1 = 32, 122, 1148, 478
    _draw_box(pixels, width, height, card_x0, card_y0, card_x1, card_y1, border, panel)
    _draw_text(pixels, width, height, card_x0 + 22, card_y0 + 20, "ALL DAYS BY HOUR", dark, scale=3)

    label_w = 92
    left = card_x0 + 26 + label_w
    top = card_y0 + 68
    cell_gap = 3
    row_gap = 8
    cell_w = (card_x1 - left - 24 - cell_gap * 23) // 24
    cell_h = 28

    # Hour labels.
    for hour in range(24):
        x0 = left + hour * (cell_w + cell_gap)
        label = f"{hour:02d}"
        _draw_text(
            pixels,
            width,
            height,
            x0 + cell_w // 2 - _text_width(label, scale=1) // 2,
            top - 20,
            label,
            muted,
            scale=1,
        )

    for day in range(7):
        y0 = top + day * (cell_h + row_gap)
        short_day = DAY_NAMES[day][:3]
        pct = report.day_percent(day)
        _draw_text(pixels, width, height, card_x0 + 24, y0 + 7, short_day, dark, scale=2)
        _draw_text(
            pixels,
            width,
            height,
            card_x0 + 62,
            y0 + 7,
            f"{pct}%",
            _heatmap_color(pct) if pct > 10 else muted,
            scale=2,
        )

        for hour in range(24):
            count = report.hourly_counts[day][hour]
            percent = report.hour_percent(day, hour)
            color = _heatmap_color(percent)
            x0 = left + hour * (cell_w + cell_gap)
            _draw_rect(pixels, width, height, x0, y0, x0 + cell_w, y0 + cell_h, color)

            if percent >= 50:
                pct_label = f"{percent}%"
                _draw_text(
                    pixels,
                    width,
                    height,
                    x0 + cell_w // 2 - _text_width(pct_label, scale=1) // 2,
                    y0 + 10,
                    pct_label,
                    (255, 255, 255),
                    scale=1,
                )

    return _encode_png_rgb(width, height, pixels)

