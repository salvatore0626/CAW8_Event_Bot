from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from database import get_connection


@dataclass
class EWQuizAnswerRecord:
    question_id: str
    category: str | None
    question_text: str
    selected_letter: str | None
    selected_answer: str | None
    correct_letter: str | None
    correct_answer: str | None
    is_correct: bool | None
    answered_at: int | None


@dataclass
class EWQuizAttemptRecord:
    attempt_id: int
    discord_id: str | None
    discord_username: str | None
    display_name: str | None
    quiz_version: str
    status: str
    passing_score: float
    score_percent: float | None
    correct_count: int
    total_questions: int
    started_at: int | None
    expires_at: int | None
    completed_at: int | None
    updated_at: int | None
    role_awarded: bool
    answers: list[EWQuizAnswerRecord]

    @property
    def id(self) -> int:
        return self.attempt_id

    @property
    def created_at(self) -> int | None:
        return self.started_at

    def missed_answers(self) -> list[EWQuizAnswerRecord]:
        return [answer for answer in self.answers if answer.is_correct is False]


@dataclass
class QualAttemptRecord:
    id: int

    request_qual_id: int | None

    instructor_discord_id: str | None
    instructor_username: str | None

    applicant_discord_id: str | None
    applicant_username: str | None

    carrier_rating: int | None
    carrier_remarks: str | None

    case1_rating: int | None
    case1_remarks: str | None

    formation_rating: int | None
    formation_remarks: str | None

    ag_rating: int | None
    ag_remarks: str | None

    aa_rating: int | None
    aa_remarks: str | None

    tank_rating: int | None
    tank_remarks: str | None

    vibe_rating: int | None
    vibe_remarks: str | None

    passed: bool | None

    final_remarks: str | None

    created_at: int | None
    updated_at: int | None


def row_value(row: Any, *names: str, default: Any = None) -> Any:
    keys = set(row.keys())

    for name in names:
        if name in keys:
            return row[name]

    return default


def int_or_none(value: Any) -> int | None:
    if value is None:
        return None

    try:
        return int(value)
    except Exception:
        return None


def bool_or_none(value: Any) -> bool | None:
    if value is None:
        return None

    try:
        return bool(int(value))
    except Exception:
        text = str(value).strip().lower()

        if text in {"pass", "passed", "true", "yes", "y"}:
            return True

        if text in {"fail", "failed", "false", "no", "n"}:
            return False

        return None


def clean_text_or_none(value: Any) -> str | None:
    if value is None:
        return None

    text = str(value).strip()

    return text or None


def split_vibe_from_general_remarks(
    general_remarks: str | None,
) -> tuple[str | None, str | None]:
    """
    Older/flexible paperwork service may append:
    Vibes remarks: blah

    Return:
    - final/general remarks
    - vibe remarks if found
    """
    if not general_remarks:
        return None, None

    lines = str(general_remarks).splitlines()
    final_lines: list[str] = []
    vibe_lines: list[str] = []

    in_vibes = False

    for line in lines:
        stripped = line.strip()

        if stripped.lower().startswith("vibes remarks:"):
            in_vibes = True
            vibe_lines.append(stripped.split(":", 1)[1].strip())
            continue

        if in_vibes:
            if stripped:
                vibe_lines.append(stripped)
            continue

        final_lines.append(line)

    final_text = "\n".join(final_lines).strip() or None
    vibe_text = "\n".join(vibe_lines).strip() or None

    return final_text, vibe_text


def qual_attempt_from_row(row: Any) -> QualAttemptRecord:
    general_remarks = clean_text_or_none(
        row_value(row, "remarks", "final_remarks")
    )

    vibe_remarks = clean_text_or_none(
        row_value(row, "vibe_remarks", "vibes_remarks")
    )

    if vibe_remarks is None:
        general_remarks, extracted_vibe_remarks = split_vibe_from_general_remarks(
            general_remarks
        )
        vibe_remarks = extracted_vibe_remarks

    vibe_rating = int_or_none(
        row_value(row, "vibe_rating", "vibes_rating", "vibe")
    )

    return QualAttemptRecord(
        id=int(row["id"]),

        request_qual_id=int_or_none(row_value(row, "request_qual_id")),

        instructor_discord_id=clean_text_or_none(row_value(row, "instructor_discord_id")),
        instructor_username=clean_text_or_none(row_value(row, "instructor_username")),

        applicant_discord_id=clean_text_or_none(row_value(row, "applicant_discord_id")),
        applicant_username=clean_text_or_none(row_value(row, "applicant_username")),

        carrier_rating=int_or_none(row_value(row, "carrier_rating", "carrier_landing_rating")),
        carrier_remarks=clean_text_or_none(row_value(row, "carrier_remarks", "carrier_landing_remarks")),

        case1_rating=int_or_none(row_value(row, "case1_rating", "case_1_rating")),
        case1_remarks=clean_text_or_none(row_value(row, "case1_remarks", "case_1_remarks")),

        formation_rating=int_or_none(row_value(row, "formation_rating", "formation_flying_rating")),
        formation_remarks=clean_text_or_none(row_value(row, "formation_remarks", "formation_flying_remarks")),

        ag_rating=int_or_none(row_value(row, "ag_rating", "air_to_ground_range_rating")),
        ag_remarks=clean_text_or_none(row_value(row, "ag_remarks", "air_to_ground_range_remarks")),

        aa_rating=int_or_none(row_value(row, "aa_rating", "air_to_air_range_rating")),
        aa_remarks=clean_text_or_none(row_value(row, "aa_remarks", "air_to_air_range_remarks")),

        tank_rating=int_or_none(row_value(row, "tank_rating", "aerial_refueling_rating")),
        tank_remarks=clean_text_or_none(row_value(row, "tank_remarks", "aerial_refueling_remarks")),

        vibe_rating=vibe_rating,
        vibe_remarks=vibe_remarks,

        passed=bool_or_none(row_value(row, "pass", "final_result")),

        final_remarks=general_remarks,

        created_at=int_or_none(row_value(row, "created_at")),
        updated_at=int_or_none(row_value(row, "updated_at")),
    )


def get_qualification_attempts_for_user(
    applicant_discord_id: str,
) -> list[QualAttemptRecord]:
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
            SELECT *
            FROM qual_log
            WHERE applicant_discord_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (str(applicant_discord_id),),
        ).fetchall()

    return [qual_attempt_from_row(row) for row in rows]


def float_or_none(value: Any) -> float | None:
    if value is None:
        return None

    try:
        return float(value)
    except Exception:
        return None


def table_exists(conn, table_name: str) -> bool:
    row = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        LIMIT 1
        """,
        (str(table_name),),
    ).fetchone()

    return row is not None


def ew_bool_or_none(value: Any) -> bool | None:
    if value is None:
        return None

    if isinstance(value, bool):
        return value

    try:
        return bool(int(value))
    except Exception:
        text = str(value).strip().lower()

        if text in {"true", "yes", "y", "correct", "pass", "passed"}:
            return True

        if text in {"false", "no", "n", "wrong", "incorrect", "fail", "failed"}:
            return False

        return None


def parse_ew_answers(value: Any) -> list[EWQuizAnswerRecord]:
    if not value:
        return []

    try:
        data = json.loads(str(value))
    except Exception:
        return []

    if not isinstance(data, dict):
        return []

    answers: list[EWQuizAnswerRecord] = []

    for key, raw in data.items():
        if not isinstance(raw, dict):
            continue

        question_id = clean_text_or_none(raw.get("question_id")) or str(key)

        answers.append(
            EWQuizAnswerRecord(
                question_id=question_id,
                category=clean_text_or_none(raw.get("category")),
                question_text=clean_text_or_none(raw.get("question_text")) or question_id,
                selected_letter=clean_text_or_none(raw.get("selected_letter")),
                selected_answer=clean_text_or_none(raw.get("selected_answer")),
                correct_letter=clean_text_or_none(raw.get("correct_letter")),
                correct_answer=clean_text_or_none(raw.get("correct_answer")),
                is_correct=ew_bool_or_none(raw.get("is_correct")),
                answered_at=int_or_none(raw.get("answered_at")),
            )
        )

    answers.sort(key=lambda answer: (answer.answered_at or 0, answer.question_id))
    return answers


def ew_quiz_attempt_from_row(row: Any) -> EWQuizAttemptRecord:
    return EWQuizAttemptRecord(
        attempt_id=int(row["attempt_id"]),
        discord_id=clean_text_or_none(row_value(row, "discord_id")),
        discord_username=clean_text_or_none(row_value(row, "discord_username")),
        display_name=clean_text_or_none(row_value(row, "display_name")),
        quiz_version=clean_text_or_none(row_value(row, "quiz_version")) or "Unknown",
        status=clean_text_or_none(row_value(row, "status")) or "Unknown",
        passing_score=float_or_none(row_value(row, "passing_score")) or 0.0,
        score_percent=float_or_none(row_value(row, "score_percent")),
        correct_count=int_or_none(row_value(row, "correct_count")) or 0,
        total_questions=int_or_none(row_value(row, "total_questions")) or 0,
        started_at=int_or_none(row_value(row, "started_at")),
        expires_at=int_or_none(row_value(row, "expires_at")),
        completed_at=int_or_none(row_value(row, "completed_at")),
        updated_at=int_or_none(row_value(row, "updated_at")),
        role_awarded=bool(int_or_none(row_value(row, "role_awarded")) or 0),
        answers=parse_ew_answers(row_value(row, "answers_json")),
    )


def get_ew_quiz_attempts_for_user(
    discord_id: str,
) -> list[EWQuizAttemptRecord]:
    with get_connection() as conn:
        if not table_exists(conn, "eq_quiz_attempts"):
            return []

        rows = conn.execute(
            """
            SELECT *
            FROM eq_quiz_attempts
            WHERE discord_id = ?
            ORDER BY started_at ASC, attempt_id ASC
            """,
            (str(discord_id),),
        ).fetchall()

    return [ew_quiz_attempt_from_row(row) for row in rows]


@dataclass
class FlightLeadReviewRecord:
    # entry_id is the attendance row that left the review.
    entry_id: int

    # leader_entry_id is the target user's 1-1 attendance row for that flight.
    # This is the ID shown in the filing cabinet menu.
    leader_entry_id: int | None
    leader_slot: str | None

    scheduled_op_id: int | None
    op_name: str | None
    scheduled_at: int | None
    reviewer_discord_id: str | None
    reviewer_name: str | None
    reviewer_slot: str | None
    flight_lead_rating: int | None
    fl_remarks: str | None


@dataclass
class FilingCabinetUserStats:
    attends: int
    unique_ops: int
    flight_lead_reviews: list[FlightLeadReviewRecord]

    @property
    def flight_lead_rating_count(self) -> int:
        return len([
            review
            for review in self.flight_lead_reviews
            if review.flight_lead_rating is not None
        ])

    @property
    def flight_lead_review_count(self) -> int:
        return len(self.flight_lead_reviews)

    @property
    def flight_lead_rating_average(self) -> float | None:
        ratings = [
            int(review.flight_lead_rating)
            for review in self.flight_lead_reviews
            if review.flight_lead_rating is not None
        ]

        if not ratings:
            return None

        return sum(ratings) / len(ratings)


def slot_is_one_one(slot: Any) -> bool:
    text = clean_text_or_none(slot)

    if not text:
        return False

    return bool(re.search(r"(^|[\s_-])1-1$", text.strip(), flags=re.IGNORECASE))


def slot_flight_prefix(slot: Any) -> str | None:
    text = clean_text_or_none(slot)

    if not text:
        return None

    match = re.match(r"^(.*?)[\s_-]*1-\d+$", text.strip(), flags=re.IGNORECASE)

    if not match:
        return None

    prefix = clean_text_or_none(match.group(1))
    return prefix.casefold() if prefix else None


def attendance_counts_for_user(discord_id: str) -> tuple[int, int]:
    with get_connection() as conn:
        if not table_exists(conn, "attendance"):
            return 0, 0

        row = conn.execute(
            """
            SELECT
                COUNT(*) AS attends,
                COUNT(DISTINCT scheduled_op_id) AS unique_ops
            FROM attendance
            WHERE discord_id = ?
              AND status IN ('submitted', 'complete')
            """,
            (str(discord_id),),
        ).fetchone()

    if row is None:
        return 0, 0

    return int(row["attends"] or 0), int(row["unique_ops"] or 0)


def flight_lead_review_from_row(row: Any) -> FlightLeadReviewRecord:
    return FlightLeadReviewRecord(
        entry_id=int(row["entry_id"]),
        leader_entry_id=int_or_none(row_value(row, "leader_entry_id")),
        leader_slot=clean_text_or_none(row_value(row, "leader_slot")),
        scheduled_op_id=int_or_none(row_value(row, "scheduled_op_id")),
        op_name=clean_text_or_none(row_value(row, "op_name", "op_template_name")),
        scheduled_at=int_or_none(row_value(row, "scheduled_at")),
        reviewer_discord_id=clean_text_or_none(row_value(row, "discord_id")),
        reviewer_name=(
            clean_text_or_none(row_value(row, "display_name"))
            or clean_text_or_none(row_value(row, "discord_username"))
            or clean_text_or_none(row_value(row, "user_name"))
            or clean_text_or_none(row_value(row, "discord_id"))
        ),
        reviewer_slot=clean_text_or_none(row_value(row, "slot")),
        flight_lead_rating=int_or_none(row_value(row, "flight_lead_rating")),
        fl_remarks=clean_text_or_none(row_value(row, "fl_remarks")),
    )


def flight_lead_reviews_for_user(discord_id: str) -> list[FlightLeadReviewRecord]:
    """Return reviews left by other players in flights where this user occupied a 1-1 slot.

    This intentionally only runs when the caller has verified the target user currently
    has the Flight Lead role. Some pilots may appear in 1-1 slots before they become
    flight leads, and those should not show FL review stats unless the role exists.
    """
    with get_connection() as conn:
        if not table_exists(conn, "attendance"):
            return []

        leader_rows = conn.execute(
            """
            SELECT
                entry_id,
                scheduled_op_id,
                slot
            FROM attendance
            WHERE discord_id = ?
              AND status IN ('submitted', 'complete')
              AND slot IS NOT NULL
            ORDER BY scheduled_op_id ASC, entry_id ASC
            """,
            (str(discord_id),),
        ).fetchall()

        flight_keys: dict[tuple[int, str], tuple[int, str | None]] = {}

        for leader_row in leader_rows:
            slot = clean_text_or_none(leader_row["slot"])

            if not slot_is_one_one(slot):
                continue

            event_id = int_or_none(leader_row["scheduled_op_id"])
            prefix = slot_flight_prefix(slot)

            if event_id is None or prefix is None:
                continue

            key = (int(event_id), prefix)
            existing = flight_keys.get(key)

            # Prefer the lowest/oldest leader attendance ID if duplicate 1-1 rows exist.
            leader_entry_id = int(leader_row["entry_id"])

            if existing is None or leader_entry_id < int(existing[0]):
                flight_keys[key] = (leader_entry_id, slot)

        if not flight_keys:
            return []

        event_ids = sorted({event_id for event_id, _ in flight_keys.keys()})
        placeholders = ",".join("?" for _ in event_ids)

        rows = conn.execute(
            f"""
            SELECT
                a.entry_id,
                a.scheduled_op_id,
                COALESCE(ot.name, a.op_template_name) AS op_name,
                oe.scheduled_at,
                a.discord_id,
                a.user_name,
                a.slot,
                a.flight_lead_rating,
                a.fl_remarks,
                u.display_name,
                u.discord_username
            FROM attendance a
            LEFT JOIN users u
                ON u.discord_id = a.discord_id
            LEFT JOIN op_events oe
                ON oe.event_id = a.scheduled_op_id
            LEFT JOIN op_templates ot
                ON ot.id = oe.op_template_id
            WHERE a.scheduled_op_id IN ({placeholders})
              AND a.status IN ('submitted', 'complete')
              AND a.discord_id IS NOT NULL
              AND a.discord_id != ?
              AND (
                    a.flight_lead_rating IS NOT NULL
                 OR NULLIF(TRIM(a.fl_remarks), '') IS NOT NULL
              )
            ORDER BY COALESCE(oe.scheduled_at, 0) ASC, a.scheduled_op_id ASC, a.entry_id ASC
            """,
            [*event_ids, str(discord_id)],
        ).fetchall()

    reviews: list[FlightLeadReviewRecord] = []
    seen_entry_ids: set[int] = set()

    for row in rows:
        event_id = int_or_none(row_value(row, "scheduled_op_id"))
        prefix = slot_flight_prefix(row_value(row, "slot"))

        if event_id is None or prefix is None:
            continue

        key = (int(event_id), prefix)
        leader_info = flight_keys.get(key)

        if leader_info is None:
            continue

        entry_id = int(row["entry_id"])

        if entry_id in seen_entry_ids:
            continue

        row_dict = dict(row)
        row_dict["leader_entry_id"] = int(leader_info[0])
        row_dict["leader_slot"] = leader_info[1]

        seen_entry_ids.add(entry_id)
        reviews.append(flight_lead_review_from_row(row_dict))

    return reviews


def get_filing_cabinet_user_stats(
    discord_id: str,
    *,
    include_flight_lead_reviews: bool = False,
) -> FilingCabinetUserStats:
    attends, unique_ops = attendance_counts_for_user(discord_id)
    reviews = (
        flight_lead_reviews_for_user(discord_id)
        if include_flight_lead_reviews
        else []
    )

    return FilingCabinetUserStats(
        attends=attends,
        unique_ops=unique_ops,
        flight_lead_reviews=reviews,
    )

