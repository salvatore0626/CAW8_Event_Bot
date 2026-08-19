from __future__ import annotations

import re
import io
import struct
import zlib
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import discord

from database import get_connection
from services.wire_gpa_service import (
    bolter_score,
    gpa_scale_footer_text,
    wire_score_map,
)


try:
    from config import GREENIE_ATTEMPT_HISTORY_LENGTH
except ImportError:
    GREENIE_ATTEMPT_HISTORY_LENGTH = 16

try:
    from config import GREENIE_NORMAL_OPS_ONLY
except ImportError:
    GREENIE_NORMAL_OPS_ONLY = True

DEFAULT_GREENIE_WIRE_CARRIER_AIRFRAMES = [
    "AV-42C",
    "F/A-26B",
    "F-45A",
    "EF-24G",
    "T-55",
]

try:
    from config import GREENIE_AIRFRAME_ORDER
except ImportError:
    GREENIE_AIRFRAME_ORDER = DEFAULT_GREENIE_WIRE_CARRIER_AIRFRAMES


try:
    from config import GREENIE_GPA_ROLLING_AVERAGE_RANGE
except ImportError:
    GREENIE_GPA_ROLLING_AVERAGE_RANGE = 5



WIRE_EMOJIS = {
    1: "🟥",
    2: "🟨",
    3: "🟩",
    4: "🟧",
}
BOLTER_EMOJI = "🟦"
CAG_DCAG_BOLTER_EMOJI = "💀"
SPECIAL_SPEAKING_BOLTER_EMOJI = "🗣️"


@dataclass(frozen=True)
class GreenieAirframe:
    aircraft: str
    attempts: list[str]
    gpa_attempt_count: int
    gpa: float | None


@dataclass(frozen=True)
class GreenieBoard:
    discord_id: str
    player_name: str
    airframes: list[GreenieAirframe]
    total_gpa_attempt_count: int
    total_gpa: float | None


@dataclass(frozen=True)
class GreenieTrendPoint:
    aircraft: str
    attempt_number: int
    scheduled_at: int
    score: float
    rolling_gpa: float


@dataclass(frozen=True)
class GreenieGraphPage:
    label: str
    aircraft_filter: str | None


def clean_text(value: Any) -> str | None:
    if value is None:
        return None

    value = str(value).strip()
    return value or None


def history_length() -> int:
    try:
        return max(1, min(50, int(GREENIE_ATTEMPT_HISTORY_LENGTH)))
    except (TypeError, ValueError):
        return 16


def normal_ops_only() -> bool:
    return bool(GREENIE_NORMAL_OPS_ONLY)


def normalize_aircraft_key(value: Any) -> str:
    text = clean_text(value) or ""
    text = text.upper().replace("–", "-").replace("—", "-")
    text = re.sub(r"[^A-Z0-9]+", "", text)

    aliases = {
        "FA26": "FA26B",
        "FA26B": "FA26B",
        "FA26C": "FA26B",
        "F18": "FA26B",
        "EF24": "EF24G",
        "EF24G": "EF24G",
        "F45": "F45A",
        "F45A": "F45A",
        "AV42": "AV42C",
        "AV42C": "AV42C",
        "T55": "T55",
    }

    return aliases.get(text, text)


def supported_greenie_airframe_keys() -> set[str]:
    return {
        normalize_aircraft_key(aircraft)
        for aircraft in DEFAULT_GREENIE_WIRE_CARRIER_AIRFRAMES
    }


def is_supported_greenie_airframe(value: Any) -> bool:
    return normalize_aircraft_key(value) in supported_greenie_airframe_keys()


def configured_airframe_order() -> list[str]:
    values = GREENIE_AIRFRAME_ORDER

    if not isinstance(values, (list, tuple)):
        values = DEFAULT_GREENIE_WIRE_CARRIER_AIRFRAMES

    supported_keys = supported_greenie_airframe_keys()
    output: list[str] = []
    seen: set[str] = set()

    for value in list(values) + DEFAULT_GREENIE_WIRE_CARRIER_AIRFRAMES:
        display = clean_text(value)
        key = normalize_aircraft_key(display)

        if not display or not key or key in seen or key not in supported_keys:
            continue

        seen.add(key)
        output.append(display)

    return output


def canonical_airframe_display(value: Any) -> str:
    raw = clean_text(value) or "Unknown"
    key = normalize_aircraft_key(raw)

    for configured in configured_airframe_order():
        if normalize_aircraft_key(configured) == key:
            return configured

    return raw


def safe_int(value: Any, *, minimum: int = 0, maximum: int = 24) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return 0

    return max(minimum, min(maximum, result))


def resolve_greenie_player_name(discord_id: str, fallback_name: str | None = None) -> str:
    """Use current display name when available, otherwise historical attendance."""
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                COALESCE(
                    NULLIF(u.display_name, ''),
                    NULLIF(recent_attendance.user_name, ''),
                    NULLIF(u.discord_username, ''),
                    ?
                ) AS player_name
            FROM (SELECT ? AS discord_id) requested
            LEFT JOIN users u
                ON u.discord_id = requested.discord_id
            LEFT JOIN attendance recent_attendance
                ON recent_attendance.entry_id = (
                    SELECT candidate.entry_id
                    FROM attendance candidate
                    WHERE candidate.discord_id = requested.discord_id
                      AND NULLIF(TRIM(candidate.user_name), '') IS NOT NULL
                    ORDER BY candidate.created_at DESC,
                             candidate.logged_at DESC,
                             candidate.entry_id DESC
                    LIMIT 1
                )
            """,
            (clean_text(fallback_name) or str(discord_id), str(discord_id)),
        ).fetchone()

    if row and clean_text(row["player_name"]):
        return str(row["player_name"])

    return clean_text(fallback_name) or str(discord_id)


def load_greenie_board(
    *,
    discord_id: str,
    fallback_name: str | None = None,
    bolter_emoji: str | None = None,
    caw8: bool = False,
) -> GreenieBoard:
    """Load carrier attempts and wire GPA history for one user or all CAW8 users.

    Attempt history is kept per airframe. A row with two bolters and a 3-wire
    expands to: 🟦🟦🟩. Only the latest configured number of attempts per
    airframe is displayed, but GPA uses all qualifying historical carrier
    attempts. Each bolter contributes a 0.0 score and one GPA attempt.
    """
    operation_type_filter = "AND ot.type = 'Normal'" if normal_ops_only() else ""
    scope_filter = "" if caw8 else "AND a.discord_id = ?"
    query_params: tuple[Any, ...] = () if caw8 else (str(discord_id),)
    selected_bolter_emoji = clean_text(bolter_emoji) or BOLTER_EMOJI

    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT
                a.entry_id,
                a.aircraft,
                a.wires,
                a.bolters,
                oe.event_id,
                oe.scheduled_at
            FROM attendance a
            JOIN op_events oe
                ON oe.event_id = a.scheduled_op_id
            JOIN op_templates ot
                ON ot.id = oe.op_template_id
            WHERE oe.status = 'Complete'
              {scope_filter}
              {operation_type_filter}
              AND a.status IN ('submitted', 'complete')
              AND a.landing_type IN ('Arrested', 'FTR')
              AND (
                    a.wires BETWEEN 1 AND 4
                 OR COALESCE(a.bolters, 0) > 0
              )
            ORDER BY oe.scheduled_at ASC, oe.event_id ASC, a.entry_id ASC
            """,
            query_params,
        ).fetchall()

    attempts_by_airframe: dict[str, list[str]] = defaultdict(list)
    gpa_scores_by_airframe: dict[str, list[float]] = defaultdict(list)

    for row in rows:
        if not is_supported_greenie_airframe(row["aircraft"]):
            continue

        aircraft = canonical_airframe_display(row["aircraft"])
        wire = safe_int(row["wires"], minimum=0, maximum=4)
        bolters = safe_int(row["bolters"], minimum=0, maximum=24)

        # A bolter occurs before the wire caught on the same recovery attempt.
        attempts_by_airframe[aircraft].extend([selected_bolter_emoji] * bolters)
        gpa_scores_by_airframe[aircraft].extend([bolter_score()] * bolters)

        scores = wire_score_map()
        if wire in scores:
            attempts_by_airframe[aircraft].append(WIRE_EMOJIS[wire])
            gpa_scores_by_airframe[aircraft].append(scores[wire])

    order = configured_airframe_order()
    order_keys = {
        normalize_aircraft_key(name): index
        for index, name in enumerate(order)
    }

    known_airframes = set(attempts_by_airframe) | set(gpa_scores_by_airframe)
    ordered_airframes = sorted(
        known_airframes,
        key=lambda aircraft: (
            order_keys.get(normalize_aircraft_key(aircraft), 10_000),
            aircraft.casefold(),
        ),
    )

    airframes: list[GreenieAirframe] = []

    for aircraft in ordered_airframes:
        attempts = attempts_by_airframe.get(aircraft, [])[-history_length():]
        scores = gpa_scores_by_airframe.get(aircraft, [])
        gpa = (sum(scores) / len(scores)) if scores else None

        airframes.append(
            GreenieAirframe(
                aircraft=aircraft,
                attempts=attempts,
                gpa_attempt_count=len(scores),
                gpa=gpa,
            )
        )

    all_scores = [
        score
        for scores in gpa_scores_by_airframe.values()
        for score in scores
    ]

    total_gpa = (sum(all_scores) / len(all_scores)) if all_scores else None

    return GreenieBoard(
        discord_id="CAW8" if caw8 else str(discord_id),
        player_name="CAW8" if caw8 else resolve_greenie_player_name(discord_id, fallback_name),
        airframes=airframes,
        total_gpa_attempt_count=len(all_scores),
        total_gpa=total_gpa,
    )


def make_attempt_lines(board: GreenieBoard) -> str:
    if not board.airframes:
        return "No qualifying carrier attempts found."

    label_width = max(len(airframe.aircraft) for airframe in board.airframes)
    lines: list[str] = []

    for airframe in board.airframes:
        history = "".join(airframe.attempts) or "—"
        lines.append(f"`{airframe.aircraft:<{label_width}}` {history}")

    return "\n".join(lines)[:1024]


def make_gpa_lines(board: GreenieBoard) -> str:
    """Render career GPA values at three decimals for the Greenie Board only."""
    rows: list[tuple[str, str]] = []

    if board.total_gpa is not None:
        rows.append(("Career Total", f"{board.total_gpa:.3f}"))
    else:
        rows.append(("Career Total", "—"))

    for airframe in board.airframes:
        value = f"{airframe.gpa:.3f}" if airframe.gpa is not None else "—"
        rows.append((airframe.aircraft, value))

    label_width = max(len(label) for label, _value in rows)
    return (
        "```\n"
        + "\n".join(f"{label:<{label_width}}  {value}" for label, value in rows)
        + "\n```"
    )[:1024]



# Small fixed color palette for the generated /greenie PNG.
# These colors are intentionally distinct so multiple airframes are readable.
AIRFRAME_GRAPH_COLORS: dict[str, tuple[int, int, int]] = {
    "AV42C": (37, 99, 235),
    "FA26B": (220, 38, 38),
    "F45A": (22, 163, 74),
    "T55": (147, 51, 234),
    "EF24G": (234, 88, 12),
}

FALLBACK_GRAPH_COLORS: list[tuple[int, int, int]] = [
    (37, 99, 235),
    (220, 38, 38),
    (22, 163, 74),
    (147, 51, 234),
    (234, 88, 12),
    (14, 165, 233),
    (202, 138, 4),
    (219, 39, 119),
]

# 3x5 pixel font. Text is rendered uppercase.
FONT_3X5: dict[str, list[str]] = {
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
    "(": ["010", "100", "100", "100", "010"],
    ")": ["010", "001", "001", "001", "010"],
}


def rolling_average_range(*, multiplier: int = 1) -> int:
    try:
        base_range = max(1, min(50, int(GREENIE_GPA_ROLLING_AVERAGE_RANGE)))
    except (TypeError, ValueError):
        base_range = 5

    try:
        safe_multiplier = max(1, int(multiplier))
    except (TypeError, ValueError):
        safe_multiplier = 1

    return max(1, min(500, base_range * safe_multiplier))


def greenie_graph_window_multiplier(*, caw8: bool = False) -> int:
    return 5 if caw8 else 1


def graph_color_for_airframe(aircraft: str, index: int) -> tuple[int, int, int]:
    key = normalize_aircraft_key(aircraft)
    configured = AIRFRAME_GRAPH_COLORS.get(key)

    if configured is not None:
        return configured

    return FALLBACK_GRAPH_COLORS[index % len(FALLBACK_GRAPH_COLORS)]


def greenie_graph_pages() -> list[GreenieGraphPage]:
    """Graph pages for /greenie: total first, then configured airframes."""
    return [GreenieGraphPage(label="Total", aircraft_filter=None)] + [
        GreenieGraphPage(label=aircraft, aircraft_filter=aircraft)
        for aircraft in configured_airframe_order()
    ]


def greenie_graph_filename(graph_label: str | None) -> str:
    label = clean_text(graph_label) or "total"
    slug = re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_").lower() or "total"
    return f"greenie_gpa_{slug}.png"


def load_greenie_trend_points(
    discord_id: str,
    *,
    aircraft_filter: str | None = None,
    caw8: bool = False,
    window_multiplier: int = 1,
) -> list[GreenieTrendPoint]:
    """Build one chronological rolling-GPA series, one point per recovery attempt.

    With no aircraft_filter, this is the total trailing rolling GPA. With an
    aircraft_filter, this is that airframe's trailing rolling GPA only. If caw8
    is true, attempts from all users are combined into one CAW8-wide timeline.
    Dots remain individual attempt scores and every point gets its own
    x-position.
    """
    operation_type_filter = "AND ot.type = 'Normal'" if normal_ops_only() else ""
    scope_filter = "" if caw8 else "AND a.discord_id = ?"
    query_params: tuple[Any, ...] = () if caw8 else (str(discord_id),)
    scores = wire_score_map()
    window_size = rolling_average_range(multiplier=window_multiplier)
    filter_key = (
        normalize_aircraft_key(aircraft_filter)
        if clean_text(aircraft_filter)
        else None
    )

    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT
                a.entry_id,
                a.aircraft,
                a.wires,
                a.bolters,
                oe.event_id,
                oe.scheduled_at
            FROM attendance a
            JOIN op_events oe
                ON oe.event_id = a.scheduled_op_id
            JOIN op_templates ot
                ON ot.id = oe.op_template_id
            WHERE oe.status = 'Complete'
              {scope_filter}
              {operation_type_filter}
              AND a.status IN ('submitted', 'complete')
              AND a.landing_type IN ('Arrested', 'FTR')
              AND (
                    a.wires BETWEEN 1 AND 4
                 OR COALESCE(a.bolters, 0) > 0
              )
            ORDER BY oe.scheduled_at ASC, oe.event_id ASC, a.entry_id ASC
            """,
            query_params,
        ).fetchall()

    raw_attempts: list[tuple[int, int, str, float]] = []
    sequence = 0

    for row in rows:
        if not is_supported_greenie_airframe(row["aircraft"]):
            continue

        aircraft = canonical_airframe_display(row["aircraft"])

        if filter_key is not None and normalize_aircraft_key(aircraft) != filter_key:
            continue

        scheduled_at = safe_int(row["scheduled_at"], minimum=0, maximum=4_102_444_800)
        wire = safe_int(row["wires"], minimum=0, maximum=4)
        bolters = safe_int(row["bolters"], minimum=0, maximum=24)

        # A bolter occurs before the wire caught on the same recovery attempt.
        for _ in range(bolters):
            raw_attempts.append((scheduled_at, sequence, aircraft, bolter_score()))
            sequence += 1

        if wire in scores:
            raw_attempts.append((scheduled_at, sequence, aircraft, scores[wire]))
            sequence += 1

    raw_attempts.sort(key=lambda item: (item[0], item[1]))

    recent_scores: deque[float] = deque(maxlen=window_size)
    trend_points: list[GreenieTrendPoint] = []

    for scheduled_at, _sequence, aircraft, score in raw_attempts:
        score = max(0.0, min(4.0, float(score)))
        recent_scores.append(score)
        rolling_gpa = sum(recent_scores) / len(recent_scores)
        trend_points.append(
            GreenieTrendPoint(
                aircraft=aircraft,
                attempt_number=len(trend_points) + 1,
                scheduled_at=int(scheduled_at),
                score=score,
                rolling_gpa=max(0.0, min(4.0, rolling_gpa)),
            )
        )

    return trend_points



def make_blank_canvas(width: int, height: int, color: tuple[int, int, int]) -> bytearray:
    r, g, b = color
    return bytearray([r, g, b] * width * height)


def set_pixel(
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


def draw_rect(
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
        for x in range(x_start, x_end + 1):
            set_pixel(pixels, width, height, x, y, color)


def draw_line(
    pixels: bytearray,
    width: int,
    height: int,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    color: tuple[int, int, int],
    *,
    thickness: int = 1,
) -> None:
    dx = abs(x1 - x0)
    dy = -abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    radius = max(0, thickness // 2)

    while True:
        draw_rect(pixels, width, height, x0 - radius, y0 - radius, x0 + radius, y0 + radius, color)

        if x0 == x1 and y0 == y1:
            break

        e2 = 2 * err

        if e2 >= dy:
            err += dy
            x0 += sx

        if e2 <= dx:
            err += dx
            y0 += sy


def draw_circle(
    pixels: bytearray,
    width: int,
    height: int,
    cx: int,
    cy: int,
    radius: int,
    color: tuple[int, int, int],
) -> None:
    radius_sq = radius * radius

    for y in range(cy - radius, cy + radius + 1):
        for x in range(cx - radius, cx + radius + 1):
            if (x - cx) * (x - cx) + (y - cy) * (y - cy) <= radius_sq:
                set_pixel(pixels, width, height, x, y, color)


def draw_text(
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
        glyph = FONT_3X5.get(char, FONT_3X5[" "])

        for row_index, row in enumerate(glyph):
            for col_index, value in enumerate(row):
                if value != "1":
                    continue

                draw_rect(
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


def text_width(text: str, *, scale: int = 2) -> int:
    return len(str(text)) * 4 * scale


def encode_png_rgb(width: int, height: int, pixels: bytearray) -> bytes:
    def chunk(chunk_type: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
        return (
            struct.pack(">I", len(data))
            + chunk_type
            + data
            + struct.pack(">I", crc)
        )

    raw_rows = []
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


def short_date_label(timestamp: int) -> str:
    return datetime.fromtimestamp(int(timestamp), timezone.utc).strftime("%m/%y")


def ordered_airframes_for_trend(points: list[GreenieTrendPoint]) -> list[str]:
    order = configured_airframe_order()
    order_keys = {
        normalize_aircraft_key(name): index
        for index, name in enumerate(order)
    }

    seen: set[str] = set()
    airframes: list[str] = []

    for point in points:
        if point.aircraft in seen:
            continue

        seen.add(point.aircraft)
        airframes.append(point.aircraft)

    return sorted(
        airframes,
        key=lambda aircraft: (
            order_keys.get(normalize_aircraft_key(aircraft), 10_000),
            aircraft.casefold(),
        ),
    )


def render_greenie_trend_graph_png(
    *,
    player_name: str,
    trend_points: list[GreenieTrendPoint],
    graph_label: str | None = None,
    rolling_window_size: int | None = None,
) -> bytes | None:
    if not trend_points:
        return None

    width = 1000
    height = 520
    left = 72
    right = 210
    top = 60
    bottom = 72
    plot_left = left
    plot_right = width - right
    plot_top = top
    plot_bottom = height - bottom

    pixels = make_blank_canvas(width, height, (255, 255, 255))
    dark = (31, 41, 55)
    grid = (229, 231, 235)
    axis = (107, 114, 128)
    light_text = (75, 85, 99)
    line_color = (55, 65, 81)

    max_attempt = max(point.attempt_number for point in trend_points)

    def x_for_attempt(attempt_number: int) -> int:
        if max_attempt <= 1:
            return (plot_left + plot_right) // 2
        return int(plot_left + ((attempt_number - 1) / (max_attempt - 1)) * (plot_right - plot_left))

    def y_for_gpa(gpa: float) -> int:
        safe_gpa = max(0.0, min(4.0, float(gpa)))
        return int(plot_bottom - (safe_gpa / 4.0) * (plot_bottom - plot_top))

    def attempt_tick_values() -> list[int]:
        if max_attempt <= 10:
            return list(range(1, max_attempt + 1))

        tick_values = {
            1,
            max_attempt,
            max(1, round(max_attempt * 0.25)),
            max(1, round(max_attempt * 0.50)),
            max(1, round(max_attempt * 0.75)),
        }
        return sorted(tick_values)

    # Plot area and grid.
    draw_rect(pixels, width, height, plot_left, plot_top, plot_right, plot_bottom, (250, 250, 250))

    for tick in range(0, 5):
        y = y_for_gpa(float(tick))
        draw_line(pixels, width, height, plot_left, y, plot_right, y, grid)
        y_label = "BOLTER" if tick == 0 else str(tick)
        draw_text(
            pixels,
            width,
            height,
            plot_left - text_width(y_label, scale=2) - 14,
            y - 5,
            y_label,
            light_text,
            scale=2,
        )

    for attempt_number in attempt_tick_values():
        x = x_for_attempt(attempt_number)
        draw_line(pixels, width, height, x, plot_top, x, plot_bottom, (243, 244, 246))
        label = str(attempt_number)
        draw_text(
            pixels,
            width,
            height,
            x - text_width(label, scale=2) // 2,
            plot_bottom + 18,
            label,
            light_text,
            scale=2,
        )

    draw_line(pixels, width, height, plot_left, plot_top, plot_left, plot_bottom, axis, thickness=2)
    draw_line(pixels, width, height, plot_left, plot_bottom, plot_right, plot_bottom, axis, thickness=2)

    graph_label_text = clean_text(graph_label) or "Total"
    window_label = rolling_window_size or rolling_average_range()
    title = f"{player_name} {graph_label_text} GPA TREND"
    subtitle = f"TRAILING {window_label} ATTEMPT AVERAGE"
    draw_text(pixels, width, height, plot_left, 20, title[:36], dark, scale=3)
    draw_text(pixels, width, height, plot_left, 43, subtitle[:58], light_text, scale=2)
    draw_text(pixels, width, height, 10, plot_top - 28, "GPA", light_text, scale=2)

    line_coords = [
        (x_for_attempt(point.attempt_number), y_for_gpa(point.rolling_gpa))
        for point in trend_points
    ]

    # One neutral rolling-GPA trend line.
    for start, end in zip(line_coords, line_coords[1:]):
        draw_line(pixels, width, height, start[0], start[1], end[0], end[1], line_color, thickness=3)

    airframes = ordered_airframes_for_trend(trend_points)
    airframe_index = {
        aircraft: index
        for index, aircraft in enumerate(airframes)
    }

    # Dots are individual attempt scores colored by the airframe flown.
    for point in trend_points:
        x = x_for_attempt(point.attempt_number)
        y = y_for_gpa(point.score)
        color = graph_color_for_airframe(
            point.aircraft,
            airframe_index.get(point.aircraft, 0),
        )
        draw_circle(pixels, width, height, x, y, 5, (255, 255, 255))
        draw_circle(pixels, width, height, x, y, 4, color)

    # Legend.
    legend_x = plot_right + 30
    legend_y = plot_top + 8
    draw_text(pixels, width, height, legend_x, legend_y - 24, "AIRFRAME", dark, scale=2)

    for index, aircraft in enumerate(airframes):
        color = graph_color_for_airframe(aircraft, index)
        y = legend_y + index * 24
        draw_circle(pixels, width, height, legend_x + 10, y + 6, 5, color)
        draw_text(pixels, width, height, legend_x + 28, y, aircraft, dark, scale=2)

    latest = trend_points[-1].rolling_gpa
    current_label_top = "CURRENT ROLLING"
    current_label_bottom = f"AVERAGE {latest:.2f}"
    draw_text(
        pixels,
        width,
        height,
        legend_x,
        plot_bottom - 42,
        current_label_top,
        light_text,
        scale=2,
    )
    draw_text(
        pixels,
        width,
        height,
        legend_x,
        plot_bottom - 22,
        current_label_bottom,
        light_text,
        scale=2,
    )

    footer = "LANDING ATTEMPTS"
    draw_text(
        pixels,
        width,
        height,
        ((plot_left + plot_right) // 2) - (text_width(footer, scale=2) // 2),
        height - 28,
        footer,
        light_text,
        scale=2,
    )

    return encode_png_rgb(width, height, pixels)



def render_greenie_empty_graph_png(
    *,
    player_name: str,
    graph_label: str | None = None,
    rolling_window_size: int | None = None,
) -> bytes:
    width = 1000
    height = 520
    pixels = make_blank_canvas(width, height, (255, 255, 255))
    dark = (31, 41, 55)
    light_text = (75, 85, 99)
    grid = (229, 231, 235)

    graph_label_text = clean_text(graph_label) or "Total"
    window_label = rolling_window_size or rolling_average_range()
    title = f"{player_name} {graph_label_text} GPA TREND"
    subtitle = f"TRAILING {window_label} ATTEMPT AVERAGE"

    draw_rect(pixels, width, height, 72, 60, width - 210, height - 72, (250, 250, 250))
    draw_line(pixels, width, height, 72, 60, width - 210, 60, grid)
    draw_line(pixels, width, height, 72, height - 72, width - 210, height - 72, grid)
    draw_text(pixels, width, height, 72, 20, title[:36], dark, scale=3)
    draw_text(pixels, width, height, 72, 43, subtitle[:58], light_text, scale=2)

    no_data = "NO QUALIFYING LANDING ATTEMPTS"
    draw_text(
        pixels,
        width,
        height,
        (width // 2) - (text_width(no_data, scale=3) // 2),
        height // 2 - 12,
        no_data,
        light_text,
        scale=3,
    )

    footer = "LANDING ATTEMPTS"
    draw_text(
        pixels,
        width,
        height,
        (width // 2) - (text_width(footer, scale=2) // 2),
        height - 28,
        footer,
        light_text,
        scale=2,
    )

    return encode_png_rgb(width, height, pixels)


def build_greenie_gpa_graph_file(
    *,
    discord_id: str,
    fallback_name: str | None = None,
    aircraft_filter: str | None = None,
    graph_label: str | None = None,
    caw8: bool = False,
) -> discord.File | None:
    label = clean_text(graph_label)

    if label is None:
        label = (
            canonical_airframe_display(aircraft_filter)
            if clean_text(aircraft_filter)
            else "Total"
        )

    window_multiplier = greenie_graph_window_multiplier(caw8=caw8)
    window_size = rolling_average_range(multiplier=window_multiplier)

    trend_points = load_greenie_trend_points(
        str(discord_id),
        aircraft_filter=aircraft_filter,
        caw8=caw8,
        window_multiplier=window_multiplier,
    )

    player_name = "CAW8" if caw8 else resolve_greenie_player_name(str(discord_id), fallback_name)

    if trend_points:
        png_bytes = render_greenie_trend_graph_png(
            player_name=player_name,
            trend_points=trend_points,
            graph_label=label,
            rolling_window_size=window_size,
        )
    elif clean_text(aircraft_filter):
        png_bytes = render_greenie_empty_graph_png(
            player_name=player_name,
            graph_label=label,
            rolling_window_size=window_size,
        )
    else:
        return None

    if not png_bytes:
        return None

    return discord.File(
        fp=io.BytesIO(png_bytes),
        filename=greenie_graph_filename(label),
    )





def build_greenie_embed(
    *,
    discord_id: str,
    fallback_name: str | None = None,
    bolter_emoji: str | None = None,
    caw8: bool = False,
) -> discord.Embed:
    board = load_greenie_board(
        discord_id=str(discord_id),
        fallback_name=fallback_name,
        bolter_emoji=bolter_emoji,
        caw8=caw8,
    )

    embed = discord.Embed(
        title=("CAW8 Greenie Board" if caw8 else f"Greenie Board for {board.player_name}"),
        description=(
            f"**Last {history_length()} carrier attempts per airframe**\n"
            + ("**CAW8-wide: all users combined**\n" if caw8 else "")
            + "🟧 4-wire  •  🟩 3-wire  •  🟨 2-wire  •  "
            + "🟥 1-wire  •  🟦 Bolter"
        ),
    )

    embed.add_field(
        name="Recent Attempts",
        value=make_attempt_lines(board),
        inline=False,
    )
    embed.add_field(
        name="Career Wire GPA (4.0 is highest)",
        value=make_gpa_lines(board),
        inline=False,
    )
    graph_window = rolling_average_range(
        multiplier=greenie_graph_window_multiplier(caw8=caw8)
    )
    embed.set_footer(
        text=(
            "GPA scale: "
            + gpa_scale_footer_text()
            + " | Career total includes all qualifying attempts"
            + f" | Trend graph = trailing {graph_window} attempts"
            + (" | CAW8-wide" if caw8 else "")
            + (
                " | Completed Normal ops only"
                if normal_ops_only()
                else " | All completed op types"
            )
        )
    )

    return embed
