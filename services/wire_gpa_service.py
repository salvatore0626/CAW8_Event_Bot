from __future__ import annotations

from typing import Any


DEFAULT_WIRE_GPA_POINTS = {
    "BOLTER": 0.0,
    "ONE_WIRE": 1.0,
    "TWO_WIRE": 3.0,
    "THREE_WIRE": 4.0,
    "FOUR_WIRE": 2.0,
}

try:
    from config import WIRE_GPA_POINTS as CONFIG_WIRE_GPA_POINTS
except ImportError:
    CONFIG_WIRE_GPA_POINTS = DEFAULT_WIRE_GPA_POINTS


WIRE_KEY_ALIASES = {
    "0": "BOLTER",
    "BOLTER": "BOLTER",
    "BOLTERS": "BOLTER",
    "BOLTER_ATTEMPT": "BOLTER",
    "1": "ONE_WIRE",
    "ONE": "ONE_WIRE",
    "ONE_WIRE": "ONE_WIRE",
    "1_WIRE": "ONE_WIRE",
    "WIRE_1": "ONE_WIRE",
    "2": "TWO_WIRE",
    "TWO": "TWO_WIRE",
    "TWO_WIRE": "TWO_WIRE",
    "2_WIRE": "TWO_WIRE",
    "WIRE_2": "TWO_WIRE",
    "3": "THREE_WIRE",
    "THREE": "THREE_WIRE",
    "THREE_WIRE": "THREE_WIRE",
    "3_WIRE": "THREE_WIRE",
    "WIRE_3": "THREE_WIRE",
    "4": "FOUR_WIRE",
    "FOUR": "FOUR_WIRE",
    "FOUR_WIRE": "FOUR_WIRE",
    "4_WIRE": "FOUR_WIRE",
    "WIRE_4": "FOUR_WIRE",
}


def normalize_point_key(key: Any) -> str | None:
    if isinstance(key, int):
        return WIRE_KEY_ALIASES.get(str(key))

    text = str(key or "").strip().upper()
    text = text.replace("-", "_").replace(" ", "_")
    return WIRE_KEY_ALIASES.get(text)


def configured_wire_gpa_points() -> dict[str, float]:
    points = dict(DEFAULT_WIRE_GPA_POINTS)

    if isinstance(CONFIG_WIRE_GPA_POINTS, dict):
        for raw_key, raw_value in CONFIG_WIRE_GPA_POINTS.items():
            key = normalize_point_key(raw_key)
            if key is None:
                continue

            try:
                points[key] = float(raw_value)
            except (TypeError, ValueError):
                continue

    return points


def bolter_score() -> float:
    return configured_wire_gpa_points()["BOLTER"]


def wire_score(wire: Any) -> float | None:
    key = normalize_point_key(wire)
    if key in {"ONE_WIRE", "TWO_WIRE", "THREE_WIRE", "FOUR_WIRE"}:
        return configured_wire_gpa_points()[key]
    return None


def wire_score_map() -> dict[int, float]:
    points = configured_wire_gpa_points()
    return {
        1: points["ONE_WIRE"],
        2: points["TWO_WIRE"],
        3: points["THREE_WIRE"],
        4: points["FOUR_WIRE"],
    }


def sql_float(value: float) -> str:
    """Return a finite float literal for embedding in generated SQLite SQL."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0

    if number != number or number in (float("inf"), float("-inf")):
        number = 0.0

    return f"{number:.10g}"


def sql_wire_score_case(wire_column: str = "a.wires") -> str:
    scores = wire_score_map()
    return (
        "CASE "
        f"{wire_column} "
        f"WHEN 1 THEN {sql_float(scores[1])} "
        f"WHEN 2 THEN {sql_float(scores[2])} "
        f"WHEN 3 THEN {sql_float(scores[3])} "
        f"WHEN 4 THEN {sql_float(scores[4])} "
        "ELSE 0.0 END"
    )


def sql_gpa_score_expression(
    wire_column: str = "a.wires",
    bolter_column: str = "a.bolters",
) -> str:
    return (
        f"(({sql_wire_score_case(wire_column)}) + "
        f"(COALESCE({bolter_column}, 0) * {sql_float(bolter_score())}))"
    )


def gpa_scale_footer_text(*, separator: str = " | ") -> str:
    scores = wire_score_map()
    parts = [
        f"1-wire = {scores[1]:.1f}",
        f"2-wire = {scores[2]:.1f}",
        f"3-wire = {scores[3]:.1f}",
        f"4-wire = {scores[4]:.1f}",
        f"Bolter = {bolter_score():.1f}",
    ]
    return separator.join(parts)


def gpa_scale_sentence() -> str:
    scores = wire_score_map()
    return (
        f"Wire GPA scale: 1-wire = {scores[1]:.1f}, "
        f"2-wire = {scores[2]:.1f}, "
        f"3-wire = {scores[3]:.1f}, "
        f"4-wire = {scores[4]:.1f}, "
        f"each bolter = {bolter_score():.1f}."
    )
