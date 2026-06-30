from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from database import get_connection

try:
    from config import EW_QUIZ_JSON_PATH
except ImportError:
    EW_QUIZ_JSON_PATH = "data/ew_quiz.json"

try:
    from config import EW_QUIZ_TIME_LIMIT_MINUTES
except ImportError:
    EW_QUIZ_TIME_LIMIT_MINUTES = 30

try:
    from config import TEST_COOLDOWN_HOURS
except ImportError:
    TEST_COOLDOWN_HOURS = 0


TABLE_NAME = "eq_quiz_attempts"

STATUS_STARTED = "Started"
STATUS_INCOMPLETE = "Incomplete"
STATUS_PASSED = "Passed"
STATUS_FAIL = "Fail"

VALID_STATUSES = {
    STATUS_STARTED,
    STATUS_INCOMPLETE,
    STATUS_PASSED,
    STATUS_FAIL,
}


class EWQuizError(Exception):
    pass


class QuizExpiredError(EWQuizError):
    pass


@dataclass(frozen=True)
class QuizVersion:
    version: str
    title: str
    passing_score: float
    randomize_questions: bool
    randomize_answers: bool
    questions: list[dict[str, Any]]


@dataclass(frozen=True)
class QuestionViewData:
    attempt_id: int
    quiz_version: str
    title: str
    status: str
    current_index: int
    total_questions: int
    question_id: str
    category: str | None
    question_text: str
    displayed_choices: list[dict[str, Any]]
    multi_select: bool
    selected_display_index: int | None
    selected_display_indexes: list[int]
    selected_letter: str | None
    selected_letters: list[str]
    selected_answer: str | None
    selected_answers: list[str]
    answered_count: int
    remaining_seconds: int
    expires_at: int
    passing_score: float



def now_ts() -> int:
    return int(time.time())


def quiz_file_path() -> Path:
    path = Path(str(EW_QUIZ_JSON_PATH))

    if path.is_absolute():
        return path

    return Path.cwd() / path


def ensure_schema() -> None:
    with get_connection() as conn:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id TEXT NOT NULL,
                discord_username TEXT,
                display_name TEXT,
                quiz_version TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Started',
                passing_score REAL NOT NULL,
                score_percent REAL,
                correct_count INTEGER NOT NULL DEFAULT 0,
                total_questions INTEGER NOT NULL,
                current_question_index INTEGER NOT NULL DEFAULT 0,
                question_order_json TEXT NOT NULL DEFAULT '[]',
                answer_order_json TEXT NOT NULL DEFAULT '{{}}',
                answers_json TEXT NOT NULL DEFAULT '{{}}',
                started_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                completed_at INTEGER,
                role_awarded INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL
            )
            """
        )

        conn.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_discord_status
            ON {TABLE_NAME} (discord_id, status)
            """
        )

        conn.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_expires
            ON {TABLE_NAME} (status, expires_at)
            """
        )


def load_quiz_bank() -> dict[str, Any]:
    path = quiz_file_path()

    if not path.exists():
        raise EWQuizError(f"Quiz file not found: {path}")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise EWQuizError(f"Quiz JSON is invalid: {error}") from error

    if not isinstance(data, dict):
        raise EWQuizError("Quiz JSON root must be an object.")

    if "active_version" not in data:
        raise EWQuizError("Quiz JSON is missing active_version.")

    versions = data.get("versions")

    if not isinstance(versions, dict) or not versions:
        raise EWQuizError("Quiz JSON must contain a non-empty versions object.")

    return data


def active_quiz() -> QuizVersion:
    data = load_quiz_bank()
    version_name = str(data.get("active_version") or "").strip()
    raw_versions = data.get("versions") or {}
    raw = raw_versions.get(version_name)

    if not isinstance(raw, dict):
        raise EWQuizError(f"Active quiz version not found: {version_name}")

    return parse_quiz_version(version_name, raw)


def quiz_by_version(version_name: str) -> QuizVersion:
    data = load_quiz_bank()
    raw_versions = data.get("versions") or {}
    raw = raw_versions.get(version_name)

    if not isinstance(raw, dict):
        raise EWQuizError(f"Quiz version not found in JSON: {version_name}")

    return parse_quiz_version(version_name, raw)


def parse_quiz_version(version_name: str, raw: dict[str, Any]) -> QuizVersion:
    questions = raw.get("questions")

    if not isinstance(questions, list) or not questions:
        raise EWQuizError(f"{version_name} has no questions.")

    parsed_questions: list[dict[str, Any]] = []

    for index, question in enumerate(questions):
        if not isinstance(question, dict):
            raise EWQuizError(f"{version_name} question {index + 1} must be an object.")

        q_text = str(question.get("question") or "").strip()
        choices = question.get("choices")

        if not q_text:
            raise EWQuizError(f"{version_name} question {index + 1} is missing question text.")

        if not isinstance(choices, list) or len(choices) < 2:
            raise EWQuizError(f"{version_name} question {index + 1} needs at least 2 choices.")

        if len(choices) > 25:
            raise EWQuizError(
                f"{version_name} question {index + 1} has {len(choices)} choices. Discord dropdowns support max 25."
            )

        normalized_choices = [str(choice).strip() for choice in choices]

        if any(not choice for choice in normalized_choices):
            raise EWQuizError(f"{version_name} question {index + 1} has a blank choice.")

        multi_select = question_allows_multi_select(question)
        correct_indices = correct_answer_indices(question, len(normalized_choices))

        if len(correct_indices) > 1:
            multi_select = True

        if not multi_select and len(correct_indices) != 1:
            raise EWQuizError(
                f"{version_name} question {index + 1} must have exactly one correct answer unless multi_select is true."
            )

        parsed = dict(question)
        parsed["choices"] = normalized_choices
        parsed["_multi_select"] = bool(multi_select)
        parsed["_correct_indices"] = sorted(correct_indices)
        parsed["_correct_index"] = sorted(correct_indices)[0]
        parsed_questions.append(parsed)

    try:
        passing_score = float(raw.get("passing_score", 80))
    except (TypeError, ValueError):
        passing_score = 80.0

    return QuizVersion(
        version=version_name,
        title=str(raw.get("title") or "EW Qualification Test").strip(),
        passing_score=passing_score,
        randomize_questions=bool(raw.get("randomize_questions", False)),
        randomize_answers=bool(raw.get("randomize_answers", False)),
        questions=parsed_questions,
    )

def question_allows_multi_select(question: dict[str, Any]) -> bool:
    """Return whether a question may accept multiple dropdown answers.

    Preferred JSON field:
        "multi_select": true

    Also accepts forgiving aliases:
        "multiple_answers": true
        "multi_answer": true
        "allow_multiple": true
    """
    for key in ("multi_select", "multiple_answers", "multi_answer", "allow_multiple"):
        if key in question:
            return bool(question.get(key))

    raw_answer = question.get(
        "answers",
        question.get("answer", question.get("correct_answers", question.get("correct_answer"))),
    )
    return isinstance(raw_answer, list)


def raw_correct_answer_value(question: dict[str, Any]) -> Any:
    if "answers" in question:
        return question.get("answers")

    if "correct_answers" in question:
        return question.get("correct_answers")

    if "answer" in question:
        return question.get("answer")

    return question.get("correct_answer", question.get("correct_index"))


def parse_answer_index(value: Any, question: dict[str, Any], choice_count: int) -> int:
    if isinstance(value, int):
        index = value
    else:
        text = str(value or "").strip().upper()

        if len(text) == 1 and "A" <= text <= "Z":
            index = ord(text) - ord("A")
        elif text.isdigit():
            index = int(text)
        else:
            raise EWQuizError(
                f"Question {question.get('id') or question.get('question')} has invalid answer value: {value}"
            )

    if index < 0 or index >= choice_count:
        raise EWQuizError(
            f"Question {question.get('id') or question.get('question')} answer index is out of range."
        )

    return index


def correct_answer_indices(question: dict[str, Any], choice_count: int) -> set[int]:
    raw_answer = raw_correct_answer_value(question)

    if isinstance(raw_answer, str) and "," in raw_answer:
        raw_values: list[Any] = [part.strip() for part in raw_answer.split(",") if part.strip()]
    elif isinstance(raw_answer, list):
        raw_values = raw_answer
    else:
        raw_values = [raw_answer]

    if not raw_values:
        raise EWQuizError(
            f"Question {question.get('id') or question.get('question')} must have at least one correct answer."
        )

    indices = {
        parse_answer_index(value, question, choice_count)
        for value in raw_values
    }

    if not indices:
        raise EWQuizError(
            f"Question {question.get('id') or question.get('question')} must have at least one correct answer."
        )

    return indices


def correct_answer_index(question: dict[str, Any], choice_count: int) -> int:
    """Backward-compatible helper for old single-answer callers."""
    indices = correct_answer_indices(question, choice_count)

    if len(indices) != 1:
        raise EWQuizError(
            f"Question {question.get('id') or question.get('question')} has multiple answers."
        )

    return next(iter(indices))

def question_id(question: dict[str, Any], index: int) -> str:
    return str(question.get("id") or f"q_{index + 1:03d}").strip()


def letter_for_index(index: int) -> str:
    if index < 0:
        return "?"

    letters = ""

    while True:
        index, remainder = divmod(index, 26)
        letters = chr(ord("A") + remainder) + letters

        if index == 0:
            return letters

        index -= 1


def safe_json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default

    try:
        return json.loads(value)
    except Exception:
        return default


def dumps_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def expire_started_attempts() -> int:
    ts = now_ts()

    with get_connection() as conn:
        cur = conn.execute(
            f"""
            UPDATE {TABLE_NAME}
            SET status = ?,
                completed_at = ?,
                updated_at = ?
            WHERE status = ?
              AND expires_at <= ?
            """,
            (
                STATUS_INCOMPLETE,
                ts,
                ts,
                STATUS_STARTED,
                ts,
            ),
        )

        return int(cur.rowcount or 0)


def fetch_attempt(attempt_id: int) -> dict[str, Any] | None:
    with get_connection() as conn:
        row = conn.execute(
            f"""
            SELECT *
            FROM {TABLE_NAME}
            WHERE attempt_id = ?
            LIMIT 1
            """,
            (int(attempt_id),),
        ).fetchone()

    return dict(row) if row is not None else None


def test_cooldown_seconds() -> int:
    try:
        hours = float(TEST_COOLDOWN_HOURS)
    except (TypeError, ValueError):
        hours = 0

    return max(0, int(hours * 3600))


def last_cooldown_attempt_for_user(discord_id: str) -> dict[str, Any] | None:
    """Return the newest completed/incomplete quiz attempt used for retake cooldown."""
    ensure_schema()
    expire_started_attempts()

    with get_connection() as conn:
        row = conn.execute(
            f"""
            SELECT *
            FROM {TABLE_NAME}
            WHERE discord_id = ?
              AND status IN (?, ?, ?)
            ORDER BY COALESCE(completed_at, updated_at, started_at) DESC,
                     attempt_id DESC
            LIMIT 1
            """,
            (
                str(discord_id),
                STATUS_INCOMPLETE,
                STATUS_PASSED,
                STATUS_FAIL,
            ),
        ).fetchone()

    return dict(row) if row is not None else None


def cooldown_remaining_for_user(discord_id: str) -> int:
    cooldown = test_cooldown_seconds()

    if cooldown <= 0:
        return 0

    attempt = last_cooldown_attempt_for_user(discord_id)

    if attempt is None:
        return 0

    base_time = int(
        attempt.get("completed_at")
        or attempt.get("updated_at")
        or attempt.get("started_at")
        or 0
    )

    remaining = (base_time + cooldown) - now_ts()
    return max(0, int(remaining))


def get_started_attempt_for_user(discord_id: str) -> dict[str, Any] | None:
    ensure_schema()
    expire_started_attempts()

    with get_connection() as conn:
        row = conn.execute(
            f"""
            SELECT *
            FROM {TABLE_NAME}
            WHERE discord_id = ?
              AND status = ?
            ORDER BY attempt_id DESC
            LIMIT 1
            """,
            (str(discord_id), STATUS_STARTED),
        ).fetchone()

    return dict(row) if row is not None else None


def start_new_attempt(
    *,
    discord_id: str,
    discord_username: str | None,
    display_name: str | None,
) -> dict[str, Any]:
    ensure_schema()
    expire_started_attempts()

    quiz = active_quiz()
    ts = now_ts()
    expires_at = ts + max(1, int(EW_QUIZ_TIME_LIMIT_MINUTES)) * 60

    question_order = list(range(len(quiz.questions)))

    if quiz.randomize_questions:
        random.shuffle(question_order)

    answer_order: dict[str, list[int]] = {}

    for original_question_index in question_order:
        question = quiz.questions[original_question_index]
        qid = question_id(question, original_question_index)
        order = list(range(len(question["choices"])))

        if quiz.randomize_answers:
            random.shuffle(order)

        answer_order[qid] = order

    with get_connection() as conn:
        cur = conn.execute(
            f"""
            INSERT INTO {TABLE_NAME} (
                discord_id,
                discord_username,
                display_name,
                quiz_version,
                status,
                passing_score,
                score_percent,
                correct_count,
                total_questions,
                current_question_index,
                question_order_json,
                answer_order_json,
                answers_json,
                started_at,
                expires_at,
                completed_at,
                role_awarded,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, NULL, 0, ?, 0, ?, ?, ?, ?, ?, NULL, 0, ?)
            """,
            (
                str(discord_id),
                discord_username,
                display_name,
                quiz.version,
                STATUS_STARTED,
                quiz.passing_score,
                len(quiz.questions),
                dumps_json(question_order),
                dumps_json(answer_order),
                dumps_json({}),
                ts,
                expires_at,
                ts,
            ),
        )

        attempt_id = int(cur.lastrowid)

    attempt = fetch_attempt(attempt_id)

    if attempt is None:
        raise EWQuizError("Could not create quiz attempt.")

    return attempt


def resume_or_start_attempt(
    *,
    discord_id: str,
    discord_username: str | None,
    display_name: str | None,
) -> tuple[dict[str, Any], bool]:
    existing = get_started_attempt_for_user(discord_id)

    if existing is not None:
        return existing, True

    return start_new_attempt(
        discord_id=discord_id,
        discord_username=discord_username,
        display_name=display_name,
    ), False


def attempt_is_expired(attempt: dict[str, Any]) -> bool:
    return int(attempt.get("expires_at") or 0) <= now_ts()


def mark_attempt_incomplete(attempt_id: int) -> None:
    ts = now_ts()

    with get_connection() as conn:
        conn.execute(
            f"""
            UPDATE {TABLE_NAME}
            SET status = ?,
                completed_at = COALESCE(completed_at, ?),
                updated_at = ?
            WHERE attempt_id = ?
              AND status = ?
            """,
            (
                STATUS_INCOMPLETE,
                ts,
                ts,
                int(attempt_id),
                STATUS_STARTED,
            ),
        )


def current_question_data(attempt_id: int) -> QuestionViewData:
    attempt = fetch_attempt(attempt_id)

    if attempt is None:
        raise EWQuizError("Quiz attempt not found.")

    if str(attempt.get("status")) != STATUS_STARTED:
        raise EWQuizError(f"This quiz attempt is already {attempt.get('status')}.")

    if attempt_is_expired(attempt):
        mark_attempt_incomplete(int(attempt["attempt_id"]))
        raise QuizExpiredError("The EW quiz timer expired. This attempt was marked Incomplete.")

    quiz = quiz_by_version(str(attempt["quiz_version"]))
    question_order = safe_json_loads(attempt.get("question_order_json"), [])
    answer_order = safe_json_loads(attempt.get("answer_order_json"), {})
    answers = safe_json_loads(attempt.get("answers_json"), {})

    if not isinstance(question_order, list) or not question_order:
        question_order = list(range(len(quiz.questions)))

    current_index = int(attempt.get("current_question_index") or 0)
    current_index = max(0, min(current_index, len(question_order) - 1))
    original_question_index = int(question_order[current_index])
    question = quiz.questions[original_question_index]
    qid = question_id(question, original_question_index)
    choice_order = answer_order.get(qid)

    if not isinstance(choice_order, list) or len(choice_order) != len(question["choices"]):
        choice_order = list(range(len(question["choices"])))

    displayed_choices: list[dict[str, Any]] = []

    for display_index, original_choice_index in enumerate(choice_order):
        original_choice_index = int(original_choice_index)
        displayed_choices.append(
            {
                "display_index": display_index,
                "original_index": original_choice_index,
                "letter": letter_for_index(display_index),
                "text": str(question["choices"][original_choice_index]),
            }
        )

    answer_entry = answers.get(qid, {}) if isinstance(answers, dict) else {}
    selected_display_indexes_raw = answer_entry.get("selected_display_indexes")

    if not isinstance(selected_display_indexes_raw, list):
        legacy_selected = answer_entry.get("selected_display_index")
        selected_display_indexes_raw = [] if legacy_selected is None else [legacy_selected]

    selected_display_indexes: list[int] = []
    seen_selected: set[int] = set()

    for raw_index in selected_display_indexes_raw:
        try:
            selected_index = int(raw_index)
        except (TypeError, ValueError):
            continue

        if selected_index in seen_selected:
            continue

        if 0 <= selected_index < len(displayed_choices):
            seen_selected.add(selected_index)
            selected_display_indexes.append(selected_index)

    selected_display_index = selected_display_indexes[0] if selected_display_indexes else None
    selected_letters: list[str] = []
    selected_answers: list[str] = []

    for selected_index in selected_display_indexes:
        selected_letters.append(displayed_choices[selected_index]["letter"])
        selected_answers.append(displayed_choices[selected_index]["text"])

    selected_letter = ", ".join(selected_letters) if selected_letters else None
    selected_answer = "\n".join(selected_answers) if selected_answers else None

    return QuestionViewData(
        attempt_id=int(attempt["attempt_id"]),
        quiz_version=quiz.version,
        title=quiz.title,
        status=str(attempt["status"]),
        current_index=current_index,
        total_questions=len(question_order),
        question_id=qid,
        category=str(question.get("category") or "").strip() or None,
        question_text=str(question["question"]),
        displayed_choices=displayed_choices,
        multi_select=bool(question.get("_multi_select", False)),
        selected_display_index=selected_display_index,
        selected_display_indexes=selected_display_indexes,
        selected_letter=selected_letter,
        selected_letters=selected_letters,
        selected_answer=selected_answer,
        selected_answers=selected_answers,
        answered_count=len(answers) if isinstance(answers, dict) else 0,
        remaining_seconds=max(0, int(attempt["expires_at"]) - now_ts()),
        expires_at=int(attempt["expires_at"]),
        passing_score=float(attempt["passing_score"]),
    )

def unanswered_question_indices(attempt_id: int) -> list[int]:
    """Return display-order indices for unanswered questions in this attempt."""
    attempt = fetch_attempt(attempt_id)

    if attempt is None:
        raise EWQuizError("Quiz attempt not found.")

    if str(attempt.get("status")) != STATUS_STARTED:
        raise EWQuizError(f"This quiz attempt is already {attempt.get('status')}.")

    if attempt_is_expired(attempt):
        mark_attempt_incomplete(int(attempt["attempt_id"]))
        raise QuizExpiredError("The EW quiz timer expired. This attempt was marked Incomplete.")

    quiz = quiz_by_version(str(attempt["quiz_version"]))
    question_order = safe_json_loads(attempt.get("question_order_json"), [])
    answers = safe_json_loads(attempt.get("answers_json"), {})

    if not isinstance(question_order, list) or not question_order:
        question_order = list(range(len(quiz.questions)))

    if not isinstance(answers, dict):
        answers = {}

    unanswered: list[int] = []

    for display_index, original_question_index in enumerate(question_order):
        original_question_index = int(original_question_index)
        question = quiz.questions[original_question_index]
        qid = question_id(question, original_question_index)

        if qid not in answers:
            unanswered.append(display_index)

    return unanswered


def move_to_next_unanswered_question(attempt_id: int) -> QuestionViewData:
    attempt = fetch_attempt(attempt_id)

    if attempt is None:
        raise EWQuizError("Quiz attempt not found.")

    if str(attempt.get("status")) != STATUS_STARTED:
        raise EWQuizError(f"This quiz attempt is already {attempt.get('status')}.")

    if attempt_is_expired(attempt):
        mark_attempt_incomplete(int(attempt["attempt_id"]))
        raise QuizExpiredError("The EW quiz timer expired. This attempt was marked Incomplete.")

    unanswered = unanswered_question_indices(attempt_id)

    if not unanswered:
        return current_question_data(attempt_id)

    current_index = int(attempt.get("current_question_index") or 0)
    next_index = None

    for index in unanswered:
        if index > current_index:
            next_index = index
            break

    if next_index is None:
        next_index = unanswered[0]

    set_current_question_index(attempt_id, next_index)
    return current_question_data(attempt_id)


def set_current_question_index(attempt_id: int, new_index: int) -> None:
    attempt = fetch_attempt(attempt_id)

    if attempt is None:
        raise EWQuizError("Quiz attempt not found.")

    if str(attempt.get("status")) != STATUS_STARTED:
        raise EWQuizError(f"This quiz attempt is already {attempt.get('status')}.")

    if attempt_is_expired(attempt):
        mark_attempt_incomplete(int(attempt["attempt_id"]))
        raise QuizExpiredError("The EW quiz timer expired. This attempt was marked Incomplete.")

    question_order = safe_json_loads(attempt.get("question_order_json"), [])

    if not isinstance(question_order, list) or not question_order:
        total = int(attempt.get("total_questions") or 1)
    else:
        total = len(question_order)

    new_index = max(0, min(int(new_index), max(0, total - 1)))
    ts = now_ts()

    with get_connection() as conn:
        conn.execute(
            f"""
            UPDATE {TABLE_NAME}
            SET current_question_index = ?,
                updated_at = ?
            WHERE attempt_id = ?
              AND status = ?
            """,
            (
                new_index,
                ts,
                int(attempt_id),
                STATUS_STARTED,
            ),
        )


def move_question(attempt_id: int, delta: int) -> None:
    attempt = fetch_attempt(attempt_id)

    if attempt is None:
        raise EWQuizError("Quiz attempt not found.")

    current = int(attempt.get("current_question_index") or 0)
    set_current_question_index(attempt_id, current + int(delta))


def record_answer(
    *,
    attempt_id: int,
    selected_display_index: int | None = None,
    selected_display_indexes: list[int] | None = None,
) -> QuestionViewData:
    attempt = fetch_attempt(attempt_id)

    if attempt is None:
        raise EWQuizError("Quiz attempt not found.")

    if str(attempt.get("status")) != STATUS_STARTED:
        raise EWQuizError(f"This quiz attempt is already {attempt.get('status')}.")

    if attempt_is_expired(attempt):
        mark_attempt_incomplete(int(attempt["attempt_id"]))
        raise QuizExpiredError("The EW quiz timer expired. This attempt was marked Incomplete.")

    data = current_question_data(attempt_id)

    if selected_display_indexes is None:
        selected_display_indexes = [] if selected_display_index is None else [selected_display_index]

    cleaned_display_indexes: list[int] = []
    seen_display_indexes: set[int] = set()

    for raw_index in selected_display_indexes:
        try:
            display_index = int(raw_index)
        except (TypeError, ValueError):
            raise EWQuizError("Selected answer is out of range.")

        if display_index < 0 or display_index >= len(data.displayed_choices):
            raise EWQuizError("Selected answer is out of range.")

        if display_index in seen_display_indexes:
            continue

        seen_display_indexes.add(display_index)
        cleaned_display_indexes.append(display_index)

    if not cleaned_display_indexes:
        raise EWQuizError("Select at least one answer.")

    if not data.multi_select and len(cleaned_display_indexes) != 1:
        raise EWQuizError("This question only accepts one answer.")

    selected_choices = [
        data.displayed_choices[index]
        for index in cleaned_display_indexes
    ]
    selected_original_indices = [
        int(choice["original_index"])
        for choice in selected_choices
    ]

    quiz = quiz_by_version(str(attempt["quiz_version"]))
    question_order = safe_json_loads(attempt.get("question_order_json"), [])
    original_question_index = int(question_order[data.current_index])
    question = quiz.questions[original_question_index]
    correct_original_indices = {
        int(index)
        for index in question.get("_correct_indices", [question.get("_correct_index")])
    }

    correct_display_indexes: list[int] = []

    for choice in data.displayed_choices:
        if int(choice["original_index"]) in correct_original_indices:
            correct_display_indexes.append(int(choice["display_index"]))

    correct_answer_texts = [
        str(question["choices"][index])
        for index in sorted(correct_original_indices)
    ]
    selected_answer_texts = [
        str(choice["text"])
        for choice in selected_choices
    ]
    selected_letters = [
        letter_for_index(index)
        for index in cleaned_display_indexes
    ]
    correct_letters = [
        letter_for_index(index)
        for index in correct_display_indexes
    ]

    qid = data.question_id
    ts = now_ts()
    answers = safe_json_loads(attempt.get("answers_json"), {})

    if not isinstance(answers, dict):
        answers = {}

    first_selected_index = cleaned_display_indexes[0]
    first_correct_index = correct_display_indexes[0] if correct_display_indexes else None

    answers[qid] = {
        "question_id": qid,
        "category": data.category,
        "question_text": data.question_text,
        "multi_select": bool(data.multi_select),
        "selected_display_index": int(first_selected_index),
        "selected_display_indexes": [int(index) for index in cleaned_display_indexes],
        "selected_letter": letter_for_index(first_selected_index),
        "selected_letters": selected_letters,
        "selected_answer": selected_answer_texts[0],
        "selected_answers": selected_answer_texts,
        "correct_display_index": first_correct_index,
        "correct_display_indexes": correct_display_indexes,
        "correct_letter": letter_for_index(first_correct_index) if first_correct_index is not None else None,
        "correct_letters": correct_letters,
        "correct_answer": correct_answer_texts[0] if correct_answer_texts else None,
        "correct_answers": correct_answer_texts,
        "is_correct": set(selected_original_indices) == correct_original_indices,
        "answered_at": ts,
    }

    correct_count = sum(1 for answer in answers.values() if answer.get("is_correct"))

    with get_connection() as conn:
        conn.execute(
            f"""
            UPDATE {TABLE_NAME}
            SET answers_json = ?,
                correct_count = ?,
                updated_at = ?
            WHERE attempt_id = ?
              AND status = ?
            """,
            (
                dumps_json(answers),
                int(correct_count),
                now_ts(),
                int(attempt_id),
                STATUS_STARTED,
            ),
        )

    return current_question_data(attempt_id)

def unanswered_question_count(attempt: dict[str, Any]) -> int:
    answers = safe_json_loads(attempt.get("answers_json"), {})

    if not isinstance(answers, dict):
        answers = {}

    total = int(attempt.get("total_questions") or 0)
    return max(0, total - len(answers))


def submit_attempt(attempt_id: int) -> dict[str, Any]:
    attempt = fetch_attempt(attempt_id)

    if attempt is None:
        raise EWQuizError("Quiz attempt not found.")

    if str(attempt.get("status")) != STATUS_STARTED:
        raise EWQuizError(f"This quiz attempt is already {attempt.get('status')}.")

    if attempt_is_expired(attempt):
        mark_attempt_incomplete(int(attempt["attempt_id"]))
        raise QuizExpiredError("The EW quiz timer expired. This attempt was marked Incomplete.")

    unanswered = unanswered_question_count(attempt)

    if unanswered > 0:
        raise EWQuizError(f"You still have {unanswered} unanswered question(s).")

    answers = safe_json_loads(attempt.get("answers_json"), {})

    if not isinstance(answers, dict):
        answers = {}

    total = int(attempt.get("total_questions") or 0)
    correct = sum(1 for answer in answers.values() if answer.get("is_correct"))
    score = (correct / total * 100.0) if total else 0.0
    passing_score = float(attempt.get("passing_score") or 80)
    status = STATUS_PASSED if score >= passing_score else STATUS_FAIL
    ts = now_ts()

    with get_connection() as conn:
        conn.execute(
            f"""
            UPDATE {TABLE_NAME}
            SET status = ?,
                score_percent = ?,
                correct_count = ?,
                completed_at = ?,
                updated_at = ?
            WHERE attempt_id = ?
              AND status = ?
            """,
            (
                status,
                float(score),
                int(correct),
                ts,
                ts,
                int(attempt_id),
                STATUS_STARTED,
            ),
        )

    updated = fetch_attempt(attempt_id)

    if updated is None:
        raise EWQuizError("Quiz attempt disappeared after submit.")

    return updated


def set_role_awarded(attempt_id: int, awarded: bool) -> None:
    ts = now_ts()

    with get_connection() as conn:
        conn.execute(
            f"""
            UPDATE {TABLE_NAME}
            SET role_awarded = ?,
                updated_at = ?
            WHERE attempt_id = ?
            """,
            (
                1 if awarded else 0,
                ts,
                int(attempt_id),
            ),
        )


def attempt_result_summary(attempt: dict[str, Any]) -> str:
    status = str(attempt.get("status") or "Unknown")
    correct = int(attempt.get("correct_count") or 0)
    total = int(attempt.get("total_questions") or 0)
    score = attempt.get("score_percent")

    if score is None:
        score_text = "Not scored"
    else:
        score_text = f"{float(score):.1f}%"

    return (
        f"Status: **{status}**\n"
        f"Score: **{correct}/{total}** ({score_text})\n"
        f"Version: **{attempt.get('quiz_version')}**"
    )
