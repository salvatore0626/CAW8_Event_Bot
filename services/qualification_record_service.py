from __future__ import annotations

import json
import re
import time
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
class ASVABAnswerRecord:
    question_id: str
    category: str | None
    question_text: str
    selected_letters: list[str]
    selected_answers: list[str]
    correct_letters: list[str]
    correct_answers: list[str]
    is_correct: bool | None
    answered_at: int | None


@dataclass
class ASVABCategoryScoreRecord:
    category: str
    correct: int
    total: int
    percent: float


@dataclass
class ASVABAttemptRecord:
    attempt_id: int
    discord_id: str | None
    discord_username: str | None
    display_name: str | None
    quiz_version: str
    status: str
    score_percent: float | None
    correct_count: int
    total_questions: int
    started_at: int | None
    expires_at: int | None
    completed_at: int | None
    updated_at: int | None
    answers: list[ASVABAnswerRecord]
    category_scores: list[ASVABCategoryScoreRecord]

    @property
    def id(self) -> int:
        return self.attempt_id

    @property
    def created_at(self) -> int | None:
        return self.started_at

    def missed_answers(self) -> list[ASVABAnswerRecord]:
        return [answer for answer in self.answers if answer.is_correct is False]


@dataclass
class FilingCabinetQualRequestRecord:
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
    status: str
    referral: str | None
    times_pinged: int

    created_at: int | None
    updated_at: int | None

    action_by_discord_id: str | None
    action_remarks: str | None


@dataclass
class UserNoteRecord:
    id: int
    note_by: str
    note_for: str
    remarks: str
    created_at: int


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


_REQUEST_ACTION_RE = re.compile(
    r"^(?:Denied by|Marked MIA by)\s+(\d+):\s*(.*)$",
    re.IGNORECASE | re.DOTALL,
)


def filing_cabinet_qual_request_from_row(row: Any) -> FilingCabinetQualRequestRecord:
    stored_remarks = clean_text_or_none(row_value(row, "remarks"))
    action_by_discord_id: str | None = None
    action_remarks = stored_remarks

    if stored_remarks:
        match = _REQUEST_ACTION_RE.match(stored_remarks)

        if match:
            action_by_discord_id = clean_text_or_none(match.group(1))
            action_remarks = clean_text_or_none(match.group(2))

    return FilingCabinetQualRequestRecord(
        id=int(row["id"]),
        discord_id=clean_text_or_none(row_value(row, "discord_id")),
        discord_username=clean_text_or_none(row_value(row, "discord_username")),
        min_requirements=clean_text_or_none(row_value(row, "min_requirements")),
        of_age=int_or_none(row_value(row, "of_age")),
        hours=float_or_none(row_value(row, "hours")),
        preferred_aircraft=clean_text_or_none(row_value(row, "preferred_aircraft")),
        timezone=clean_text_or_none(row_value(row, "timezone")),
        availability_start=clean_text_or_none(row_value(row, "availability_start")),
        availability_end=clean_text_or_none(row_value(row, "availability_end")),
        dotw=clean_text_or_none(row_value(row, "dotw")),
        remarks=stored_remarks,
        status=clean_text_or_none(row_value(row, "status")) or "unknown",
        referral=clean_text_or_none(row_value(row, "referral")),
        times_pinged=int_or_none(row_value(row, "times_pinged")) or 0,
        created_at=int_or_none(row_value(row, "created_at")),
        updated_at=int_or_none(row_value(row, "updated_at")),
        action_by_discord_id=action_by_discord_id,
        action_remarks=action_remarks,
    )


def ensure_user_notes_table() -> None:
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                note_by TEXT NOT NULL,
                note_for TEXT NOT NULL,
                remarks TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_user_notes_note_for_created_at
            ON user_notes (note_for, created_at, id)
            """
        )


def user_note_from_row(row: Any) -> UserNoteRecord:
    return UserNoteRecord(
        id=int(row["id"]),
        note_by=clean_text_or_none(row_value(row, "note_by")) or "",
        note_for=clean_text_or_none(row_value(row, "note_for")) or "",
        remarks=clean_text_or_none(row_value(row, "remarks")) or "",
        created_at=int_or_none(row_value(row, "created_at")) or 0,
    )


def get_user_notes_for_user(discord_id: str) -> list[UserNoteRecord]:
    ensure_user_notes_table()

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, note_by, note_for, remarks, created_at
            FROM user_notes
            WHERE note_for = ?
            ORDER BY created_at ASC, id ASC
            """,
            (str(discord_id),),
        ).fetchall()

    return [user_note_from_row(row) for row in rows]


def add_user_note(
    *,
    note_by: str,
    note_for: str,
    remarks: str,
) -> UserNoteRecord:
    ensure_user_notes_table()

    clean_remarks = clean_text_or_none(remarks)

    if not clean_remarks:
        raise ValueError("A note must contain remarks.")

    created_at = int(time.time())

    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO user_notes (note_by, note_for, remarks, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (str(note_by), str(note_for), clean_remarks, created_at),
        )
        note_id = int(cursor.lastrowid)

    return UserNoteRecord(
        id=note_id,
        note_by=str(note_by),
        note_for=str(note_for),
        remarks=clean_remarks,
        created_at=created_at,
    )


def get_mia_and_denied_qual_requests_for_user(
    discord_id: str,
) -> list[FilingCabinetQualRequestRecord]:
    with get_connection() as conn:
        if not table_exists(conn, "request_qual"):
            return []

        rows = conn.execute(
            """
            SELECT *
            FROM request_qual
            WHERE discord_id = ?
              AND LOWER(status) IN ('mia', 'denied')
            ORDER BY COALESCE(updated_at, created_at) ASC, id ASC
            """,
            (str(discord_id),),
        ).fetchall()

    return [filing_cabinet_qual_request_from_row(row) for row in rows]


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


def clean_text_list(value: Any, fallback: Any = None) -> list[str]:
    source = value if value is not None else fallback

    if source is None:
        return []

    if isinstance(source, (list, tuple)):
        values = source
    else:
        values = [source]

    result: list[str] = []

    for item in values:
        text = clean_text_or_none(item)

        if text is not None:
            result.append(text)

    return result


def parse_asvab_answers(value: Any) -> list[ASVABAnswerRecord]:
    if not value:
        return []

    try:
        data = json.loads(str(value))
    except Exception:
        return []

    if not isinstance(data, dict):
        return []

    answers: list[ASVABAnswerRecord] = []

    for key, raw in data.items():
        if not isinstance(raw, dict):
            continue

        question_id = clean_text_or_none(raw.get("question_id")) or str(key)

        answers.append(
            ASVABAnswerRecord(
                question_id=question_id,
                category=clean_text_or_none(raw.get("category")),
                question_text=clean_text_or_none(raw.get("question_text")) or question_id,
                selected_letters=clean_text_list(
                    raw.get("selected_letters"),
                    raw.get("selected_letter"),
                ),
                selected_answers=clean_text_list(
                    raw.get("selected_answers"),
                    raw.get("selected_answer"),
                ),
                correct_letters=clean_text_list(
                    raw.get("correct_letters"),
                    raw.get("correct_letter"),
                ),
                correct_answers=clean_text_list(
                    raw.get("correct_answers"),
                    raw.get("correct_answer"),
                ),
                is_correct=ew_bool_or_none(raw.get("is_correct")),
                answered_at=int_or_none(raw.get("answered_at")),
            )
        )

    answers.sort(key=lambda answer: (answer.answered_at or 0, answer.question_id))
    return answers


def parse_asvab_category_scores(value: Any) -> list[ASVABCategoryScoreRecord]:
    if not value:
        return []

    try:
        data = json.loads(str(value))
    except Exception:
        return []

    if not isinstance(data, list):
        return []

    scores: list[ASVABCategoryScoreRecord] = []

    for raw in data:
        if not isinstance(raw, dict):
            continue

        category = clean_text_or_none(raw.get("category")) or "Uncategorized"
        correct = int_or_none(raw.get("correct")) or 0
        total = int_or_none(raw.get("total")) or 0
        percent = float_or_none(raw.get("percent"))

        if percent is None:
            percent = (correct / total * 100.0) if total else 0.0

        scores.append(
            ASVABCategoryScoreRecord(
                category=category,
                correct=correct,
                total=total,
                percent=float(percent),
            )
        )

    return scores


def asvab_attempt_from_row(row: Any) -> ASVABAttemptRecord:
    return ASVABAttemptRecord(
        attempt_id=int(row["attempt_id"]),
        discord_id=clean_text_or_none(row_value(row, "discord_id")),
        discord_username=clean_text_or_none(row_value(row, "discord_username")),
        display_name=clean_text_or_none(row_value(row, "display_name")),
        quiz_version=clean_text_or_none(row_value(row, "quiz_version")) or "Unknown",
        status=clean_text_or_none(row_value(row, "status")) or "Unknown",
        score_percent=float_or_none(row_value(row, "score_percent")),
        correct_count=int_or_none(row_value(row, "correct_count")) or 0,
        total_questions=int_or_none(row_value(row, "total_questions")) or 0,
        started_at=int_or_none(row_value(row, "started_at")),
        expires_at=int_or_none(row_value(row, "expires_at")),
        completed_at=int_or_none(row_value(row, "completed_at")),
        updated_at=int_or_none(row_value(row, "updated_at")),
        answers=parse_asvab_answers(row_value(row, "answers_json")),
        category_scores=parse_asvab_category_scores(
            row_value(row, "category_scores_json")
        ),
    )


def get_asvab_attempts_for_user(
    discord_id: str,
) -> list[ASVABAttemptRecord]:
    with get_connection() as conn:
        if not table_exists(conn, "asvab_quiz_attempts"):
            return []

        rows = conn.execute(
            """
            SELECT *
            FROM asvab_quiz_attempts
            WHERE discord_id = ?
            ORDER BY started_at ASC, attempt_id ASC
            """,
            (str(discord_id),),
        ).fetchall()

    return [asvab_attempt_from_row(row) for row in rows]


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
class FilingCabinetAttendanceRecord:
    entry_id: int
    scheduled_op_id: int | None
    op_name: str | None
    scheduled_at: int | None
    slot: str | None
    aircraft: str | None
    landing_type: str | None
    wires: int | None
    bolters: int | None
    combat_deaths: int | None
    attend_type: str | None
    status: str | None
    op_remarks: str | None
    fl_remarks: str | None
    note_remarks: str | None

    @property
    def id(self) -> int:
        return self.entry_id

    @property
    def created_at(self) -> int | None:
        return self.scheduled_at


@dataclass
class FilingCabinetUserStats:
    attends: int
    unique_ops: int
    timezone: str | None
    flight_lead_reviews: list[FlightLeadReviewRecord]
    ftr_count: int = 0
    dnf_count: int = 0
    promotion_cap: str | None = None

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



def attendance_records_for_user(discord_id: str) -> list[FilingCabinetAttendanceRecord]:
    with get_connection() as conn:
        if not table_exists(conn, "attendance"):
            return []
        rows = conn.execute(
            """
            SELECT a.entry_id, a.scheduled_op_id,
                   COALESCE(ot.name, a.op_template_name) AS op_name,
                   oe.scheduled_at, a.slot, a.aircraft, a.landing_type,
                   a.wires, a.bolters, a.combat_deaths, a.attend_type, a.status,
                   a.op_remarks, a.fl_remarks, a.note_remarks
            FROM attendance a
            LEFT JOIN op_events oe ON oe.event_id = a.scheduled_op_id
            LEFT JOIN op_templates ot ON ot.id = oe.op_template_id
            WHERE a.discord_id = ?
              AND a.status IN ('submitted', 'complete')
              AND a.landing_type IN ('FTR', 'DNF')
            ORDER BY COALESCE(oe.scheduled_at, a.created_at, a.logged_at, 0), a.entry_id
            """,
            (str(discord_id),),
        ).fetchall()
    return [FilingCabinetAttendanceRecord(
        entry_id=int(r["entry_id"]), scheduled_op_id=int_or_none(r["scheduled_op_id"]),
        op_name=clean_text_or_none(r["op_name"]), scheduled_at=int_or_none(r["scheduled_at"]),
        slot=clean_text_or_none(r["slot"]), aircraft=clean_text_or_none(r["aircraft"]),
        landing_type=clean_text_or_none(r["landing_type"]), wires=int_or_none(r["wires"]),
        bolters=int_or_none(r["bolters"]), combat_deaths=int_or_none(r["combat_deaths"]),
        attend_type=clean_text_or_none(r["attend_type"]), status=clean_text_or_none(r["status"]),
        op_remarks=clean_text_or_none(r["op_remarks"]), fl_remarks=clean_text_or_none(r["fl_remarks"]),
        note_remarks=clean_text_or_none(r["note_remarks"]),
    ) for r in rows]

def landing_type_counts_for_user(discord_id: str) -> tuple[int, int]:
    with get_connection() as conn:
        if not table_exists(conn, "attendance"):
            return 0, 0
        row = conn.execute(
            """SELECT
                 SUM(CASE WHEN landing_type='FTR' THEN 1 ELSE 0 END) AS ftr_count,
                 SUM(CASE WHEN landing_type='DNF' THEN 1 ELSE 0 END) AS dnf_count
               FROM attendance
               WHERE discord_id=? AND status IN ('submitted','complete')""",
            (str(discord_id),),
        ).fetchone()
    return int(row["ftr_count"] or 0), int(row["dnf_count"] or 0)


def attendance_counts_for_user(discord_id: str) -> tuple[int, int]:
    with get_connection() as conn:
        if not table_exists(conn, "attendance"):
            return 0, 0

        if table_exists(conn, "op_events"):
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS attends,
                    COUNT(DISTINCT COALESCE(
                        CAST(oe.op_template_id AS TEXT),
                        NULLIF(TRIM(a.op_template_name), '')
                    )) AS unique_ops
                FROM attendance a
                LEFT JOIN op_events oe
                    ON oe.event_id = a.scheduled_op_id
                WHERE a.discord_id = ?
                  AND a.status IN ('submitted', 'complete')
                """,
                (str(discord_id),),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS attends,
                    COUNT(DISTINCT NULLIF(TRIM(op_template_name), '')) AS unique_ops
                FROM attendance
                WHERE discord_id = ?
                  AND status IN ('submitted', 'complete')
                """,
                (str(discord_id),),
            ).fetchone()

    if row is None:
        return 0, 0

    return int(row["attends"] or 0), int(row["unique_ops"] or 0)


def promotion_cap_for_user(discord_id: str) -> str | None:
    with get_connection() as conn:
        if not table_exists(conn, "do_not_promote"):
            return None

        row = conn.execute(
            """
            SELECT max_rank
            FROM do_not_promote
            WHERE discord_id = ?
              AND max_rank IS NOT NULL
              AND TRIM(max_rank) != ''
            LIMIT 1
            """,
            (str(discord_id),),
        ).fetchone()

    if row is None:
        return None

    return clean_text_or_none(row["max_rank"])


def timezone_for_user(discord_id: str) -> str | None:
    with get_connection() as conn:
        if not table_exists(conn, "user_settings"):
            return None

        row = conn.execute(
            """
            SELECT timezone
            FROM user_settings
            WHERE discord_id = ?
            """,
            (str(discord_id),),
        ).fetchone()

    if row is None:
        return None

    return clean_text_or_none(row["timezone"])


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
    timezone = timezone_for_user(discord_id)
    ftr_count, dnf_count = landing_type_counts_for_user(discord_id)
    promotion_cap = promotion_cap_for_user(discord_id)
    reviews = (
        flight_lead_reviews_for_user(discord_id)
        if include_flight_lead_reviews
        else []
    )

    return FilingCabinetUserStats(
        attends=attends,
        unique_ops=unique_ops,
        timezone=timezone,
        flight_lead_reviews=reviews,
        ftr_count=ftr_count,
        dnf_count=dnf_count,
        promotion_cap=promotion_cap,
    )

