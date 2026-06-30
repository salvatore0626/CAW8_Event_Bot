from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from database import get_connection

try:
    from config import ASVAB_JSON_PATH
except ImportError:
    ASVAB_JSON_PATH = "data/asvab_quiz.json"

try:
    from config import ASVAB_NUMBER_OF_QUESTIONS
except ImportError:
    ASVAB_NUMBER_OF_QUESTIONS = 25

try:
    from config import ASVAB_TIME_LIMIT_MINUTES
except ImportError:
    ASVAB_TIME_LIMIT_MINUTES = 45


TABLE_NAME = "asvab_quiz_attempts"

STATUS_STARTED = "Started"
STATUS_INCOMPLETE = "Incomplete"
STATUS_COMPLETE = "Complete"

VALID_STATUSES = {
    STATUS_STARTED,
    STATUS_INCOMPLETE,
    STATUS_COMPLETE,
}


class ASVABQuizError(Exception):
    pass


class ASVABQuizExpiredError(ASVABQuizError):
    pass


@dataclass(frozen=True)
class ASVABQuizVersion:
    version: str
    title: str
    randomize_questions: bool
    randomize_answers: bool
    questions: list[dict[str, Any]]
    category_order: list[str]


@dataclass(frozen=True)
class ASVABQuestionViewData:
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


def now_ts() -> int:
    return int(time.time())


def quiz_file_path() -> Path:
    path = Path(str(ASVAB_JSON_PATH))

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
                score_percent REAL,
                correct_count INTEGER NOT NULL DEFAULT 0,
                total_questions INTEGER NOT NULL,
                current_question_index INTEGER NOT NULL DEFAULT 0,
                question_order_json TEXT NOT NULL DEFAULT '[]',
                answer_order_json TEXT NOT NULL DEFAULT '{{}}',
                answers_json TEXT NOT NULL DEFAULT '{{}}',
                category_scores_json TEXT NOT NULL DEFAULT '[]',
                started_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                completed_at INTEGER,
                updated_at INTEGER NOT NULL
            )
            """
        )


        columns = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({TABLE_NAME})").fetchall()
        }

        if "category_scores_json" not in columns:
            conn.execute(
                f"ALTER TABLE {TABLE_NAME} ADD COLUMN category_scores_json TEXT NOT NULL DEFAULT '[]'"
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
        raise ASVABQuizError(f"ASVAB quiz file not found: {path}")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ASVABQuizError(f"ASVAB quiz JSON is invalid: {error}") from error

    if not isinstance(data, dict):
        raise ASVABQuizError("ASVAB quiz JSON root must be an object.")

    if "active_version" not in data:
        raise ASVABQuizError("ASVAB quiz JSON is missing active_version.")

    versions = data.get("versions")

    if not isinstance(versions, dict) or not versions:
        raise ASVABQuizError("ASVAB quiz JSON must contain a non-empty versions object.")

    return data


def active_quiz() -> ASVABQuizVersion:
    data = load_quiz_bank()
    version_name = str(data.get("active_version") or "").strip()
    raw_versions = data.get("versions") or {}
    raw = raw_versions.get(version_name)

    if not isinstance(raw, dict):
        raise ASVABQuizError(f"Active ASVAB quiz version not found: {version_name}")

    return parse_quiz_version(version_name, raw)


def quiz_by_version(version_name: str) -> ASVABQuizVersion:
    data = load_quiz_bank()
    raw_versions = data.get("versions") or {}
    raw = raw_versions.get(version_name)

    if not isinstance(raw, dict):
        raise ASVABQuizError(f"ASVAB quiz version not found in JSON: {version_name}")

    return parse_quiz_version(version_name, raw)


def parse_quiz_version(version_name: str, raw: dict[str, Any]) -> ASVABQuizVersion:
    questions = raw.get("questions")

    if not isinstance(questions, list) or not questions:
        raise ASVABQuizError(f"{version_name} has no questions.")

    configured_categories = raw.get("categories")
    category_order: list[str] = []
    seen_categories: set[str] = set()

    if isinstance(configured_categories, list):
        for category in configured_categories:
            text = str(category or "").strip()
            if text and text.casefold() not in seen_categories:
                category_order.append(text)
                seen_categories.add(text.casefold())

    parsed_questions: list[dict[str, Any]] = []

    for index, question in enumerate(questions):
        if not isinstance(question, dict):
            raise ASVABQuizError(f"{version_name} question {index + 1} must be an object.")

        q_text = str(question.get("question") or "").strip()
        choices = question.get("choices")
        category = str(question.get("category") or "Uncategorized").strip() or "Uncategorized"

        if not q_text:
            raise ASVABQuizError(f"{version_name} question {index + 1} is missing question text.")

        if not isinstance(choices, list) or len(choices) < 2:
            raise ASVABQuizError(f"{version_name} question {index + 1} needs at least 2 choices.")

        if len(choices) > 25:
            raise ASVABQuizError(
                f"{version_name} question {index + 1} has {len(choices)} choices. Discord dropdowns support max 25."
            )

        normalized_choices = [str(choice).strip() for choice in choices]

        if any(not choice for choice in normalized_choices):
            raise ASVABQuizError(f"{version_name} question {index + 1} has a blank choice.")

        answer_list = normalize_answer_to_list(raw_correct_answer_value(question))
        multi_select = bool(question.get("multi_select", False) or len(answer_list) > 1)
        correct_indices = correct_answer_indices(question, len(normalized_choices))

        if len(correct_indices) > 1:
            multi_select = True

        if not multi_select and len(correct_indices) != 1:
            raise ASVABQuizError(
                f"{version_name} question {index + 1} must have exactly one correct answer unless multi_select is true."
            )

        if category.casefold() not in seen_categories:
            category_order.append(category)
            seen_categories.add(category.casefold())

        parsed = dict(question)
        parsed["category"] = category
        parsed["choices"] = normalized_choices
        parsed["answer"] = answer_list
        parsed["multi_select"] = bool(multi_select)
        parsed["_multi_select"] = bool(multi_select)
        parsed["_correct_indices"] = sorted(correct_indices)
        parsed["_correct_index"] = sorted(correct_indices)[0]
        parsed_questions.append(parsed)

    return ASVABQuizVersion(
        version=version_name,
        title=str(raw.get("title") or "ASVAB").strip(),
        randomize_questions=bool(raw.get("randomize_questions", True)),
        randomize_answers=bool(raw.get("randomize_answers", True)),
        questions=parsed_questions,
        category_order=category_order,
    )


def normalize_answer_to_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip().upper() for item in value if str(item).strip()]

    if isinstance(value, int):
        return [str(value)]

    text = str(value or "").strip().upper()

    if not text:
        return []

    if "," in text:
        return [part.strip() for part in text.split(",") if part.strip()]

    return [text]


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
            raise ASVABQuizError(
                f"Question {question.get('id') or question.get('question')} has invalid answer value: {value}"
            )

    if index < 0 or index >= choice_count:
        raise ASVABQuizError(
            f"Question {question.get('id') or question.get('question')} answer index is out of range."
        )

    return index


def correct_answer_indices(question: dict[str, Any], choice_count: int) -> set[int]:
    raw_values = normalize_answer_to_list(raw_correct_answer_value(question))

    if not raw_values:
        raise ASVABQuizError(
            f"Question {question.get('id') or question.get('question')} must have at least one correct answer."
        )

    indices = {
        parse_answer_index(value, question, choice_count)
        for value in raw_values
    }

    if not indices:
        raise ASVABQuizError(
            f"Question {question.get('id') or question.get('question')} must have at least one correct answer."
        )

    return indices


def question_id(question: dict[str, Any], index: int) -> str:
    return str(question.get("id") or f"q_{index + 1:03d}").strip()


def letter_for_index(index: int | None) -> str:
    if index is None or index < 0:
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


def configured_question_target() -> int:
    try:
        target = int(ASVAB_NUMBER_OF_QUESTIONS)
    except (TypeError, ValueError):
        target = 0

    return max(0, target)


def category_question_indices(quiz: ASVABQuizVersion) -> dict[str, list[int]]:
    groups: dict[str, list[int]] = {
        category: []
        for category in quiz.category_order
    }

    for index, question in enumerate(quiz.questions):
        category = str(question.get("category") or "Uncategorized").strip() or "Uncategorized"
        groups.setdefault(category, []).append(index)

    return {
        category: indices
        for category, indices in groups.items()
        if indices
    }


def select_question_indices_for_quiz(quiz: ASVABQuizVersion) -> list[int]:
    groups = category_question_indices(quiz)
    categories = list(groups.keys())
    total_available = sum(len(indices) for indices in groups.values())

    if total_available <= 0:
        return []

    target = configured_question_target()

    if target <= 0 or target >= total_available:
        selected = [index for indices in groups.values() for index in indices]
        if quiz.randomize_questions:
            random.shuffle(selected)
        return selected

    target = min(target, total_available)

    shuffled_groups: dict[str, list[int]] = {}

    for category in categories:
        indices = list(groups[category])
        random.shuffle(indices)
        shuffled_groups[category] = indices

    base = target // max(1, len(categories))
    remainder = target % max(1, len(categories))
    selected: list[int] = []

    for category_index, category in enumerate(categories):
        desired = base + (1 if category_index < remainder else 0)
        available = shuffled_groups[category]
        take_count = min(desired, len(available))

        for _ in range(take_count):
            selected.append(available.pop(0))

    # If some categories were short, fill the remaining slots from categories
    # that still have unused questions, one at a time to stay as balanced as possible.
    while len(selected) < target:
        added_this_round = False

        for category in categories:
            if len(selected) >= target:
                break

            available = shuffled_groups[category]
            if not available:
                continue

            selected.append(available.pop(0))
            added_this_round = True

        if not added_this_round:
            break

    if quiz.randomize_questions:
        random.shuffle(selected)

    return selected


def planned_question_count() -> int:
    quiz = active_quiz()
    return len(select_question_indices_for_quiz(quiz))


def category_plan_counts() -> dict[str, int]:
    quiz = active_quiz()
    selected_indices = select_question_indices_for_quiz(quiz)
    counts: dict[str, int] = {}

    for index in selected_indices:
        question = quiz.questions[int(index)]
        category = str(question.get("category") or "Uncategorized").strip() or "Uncategorized"
        counts[category] = counts.get(category, 0) + 1

    return counts


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
            ORDER BY started_at DESC, attempt_id DESC
            LIMIT 1
            """,
            (
                str(discord_id),
                STATUS_STARTED,
            ),
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
    question_order = select_question_indices_for_quiz(quiz)

    if not question_order:
        raise ASVABQuizError("No ASVAB questions are available.")

    ts = now_ts()
    expires_at = ts + max(1, int(ASVAB_TIME_LIMIT_MINUTES)) * 60
    answer_order: dict[str, list[int]] = {}

    for original_question_index in question_order:
        question = quiz.questions[int(original_question_index)]
        qid = question_id(question, int(original_question_index))
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
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, NULL, 0, ?, 0, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (
                str(discord_id),
                discord_username,
                display_name,
                quiz.version,
                STATUS_STARTED,
                len(question_order),
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
        raise ASVABQuizError("Could not create ASVAB attempt.")

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


def current_question_data(attempt_id: int) -> ASVABQuestionViewData:
    attempt = fetch_attempt(attempt_id)

    if attempt is None:
        raise ASVABQuizError("ASVAB attempt not found.")

    if str(attempt.get("status")) != STATUS_STARTED:
        raise ASVABQuizError(f"This ASVAB attempt is already {attempt.get('status')}.")

    if attempt_is_expired(attempt):
        mark_attempt_incomplete(int(attempt["attempt_id"]))
        raise ASVABQuizExpiredError("The ASVAB timer expired. This attempt was marked Incomplete.")

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

    return ASVABQuestionViewData(
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
    )


def unanswered_question_indices(attempt_id: int) -> list[int]:
    attempt = fetch_attempt(attempt_id)

    if attempt is None:
        raise ASVABQuizError("ASVAB attempt not found.")

    if str(attempt.get("status")) != STATUS_STARTED:
        raise ASVABQuizError(f"This ASVAB attempt is already {attempt.get('status')}.")

    if attempt_is_expired(attempt):
        mark_attempt_incomplete(int(attempt["attempt_id"]))
        raise ASVABQuizExpiredError("The ASVAB timer expired. This attempt was marked Incomplete.")

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


def move_to_next_unanswered_question(attempt_id: int) -> ASVABQuestionViewData:
    attempt = fetch_attempt(attempt_id)

    if attempt is None:
        raise ASVABQuizError("ASVAB attempt not found.")

    current = int(attempt.get("current_question_index") or 0)
    unanswered = unanswered_question_indices(attempt_id)

    if not unanswered:
        return current_question_data(attempt_id)

    later = [
        index
        for index in unanswered
        if index > current
    ]

    target = later[0] if later else unanswered[0]
    set_current_question_index(attempt_id, target)

    return current_question_data(attempt_id)


def set_current_question_index(attempt_id: int, new_index: int) -> None:
    attempt = fetch_attempt(attempt_id)

    if attempt is None:
        raise ASVABQuizError("ASVAB attempt not found.")

    if str(attempt.get("status")) != STATUS_STARTED:
        raise ASVABQuizError(f"This ASVAB attempt is already {attempt.get('status')}.")

    if attempt_is_expired(attempt):
        mark_attempt_incomplete(int(attempt["attempt_id"]))
        raise ASVABQuizExpiredError("The ASVAB timer expired. This attempt was marked Incomplete.")

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
        raise ASVABQuizError("ASVAB attempt not found.")

    current = int(attempt.get("current_question_index") or 0)
    set_current_question_index(attempt_id, current + int(delta))


def record_answer(
    *,
    attempt_id: int,
    selected_display_index: int | None = None,
    selected_display_indexes: list[int] | None = None,
) -> ASVABQuestionViewData:
    attempt = fetch_attempt(attempt_id)

    if attempt is None:
        raise ASVABQuizError("ASVAB attempt not found.")

    if str(attempt.get("status")) != STATUS_STARTED:
        raise ASVABQuizError(f"This ASVAB attempt is already {attempt.get('status')}.")

    if attempt_is_expired(attempt):
        mark_attempt_incomplete(int(attempt["attempt_id"]))
        raise ASVABQuizExpiredError("The ASVAB timer expired. This attempt was marked Incomplete.")

    data = current_question_data(attempt_id)

    if selected_display_indexes is None:
        selected_display_indexes = [] if selected_display_index is None else [selected_display_index]

    cleaned_display_indexes: list[int] = []
    seen_display_indexes: set[int] = set()

    for raw_index in selected_display_indexes:
        try:
            display_index = int(raw_index)
        except (TypeError, ValueError):
            raise ASVABQuizError("Selected answer is out of range.")

        if display_index < 0 or display_index >= len(data.displayed_choices):
            raise ASVABQuizError("Selected answer is out of range.")

        if display_index in seen_display_indexes:
            continue

        seen_display_indexes.add(display_index)
        cleaned_display_indexes.append(display_index)

    if not cleaned_display_indexes:
        raise ASVABQuizError("Select at least one answer.")

    if not data.multi_select and len(cleaned_display_indexes) != 1:
        raise ASVABQuizError("This question only accepts one answer.")

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
                ts,
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


def category_score_summary(attempt: dict[str, Any]) -> list[dict[str, Any]]:
    stored_scores = safe_json_loads(attempt.get("category_scores_json"), [])

    if isinstance(stored_scores, list) and stored_scores:
        normalized_scores: list[dict[str, Any]] = []

        for row in stored_scores:
            if not isinstance(row, dict):
                continue

            category = str(row.get("category") or "").strip()
            if not category:
                continue

            try:
                correct = int(row.get("correct") or 0)
                total = int(row.get("total") or 0)
                percent = float(row.get("percent") or 0.0)
            except (TypeError, ValueError):
                continue

            normalized_scores.append(
                {
                    "category": category,
                    "correct": correct,
                    "total": total,
                    "percent": percent,
                }
            )

        if normalized_scores:
            return normalized_scores

    return calculate_category_score_summary(attempt)


def calculate_category_score_summary(attempt: dict[str, Any]) -> list[dict[str, Any]]:
    quiz = quiz_by_version(str(attempt["quiz_version"]))
    question_order = safe_json_loads(attempt.get("question_order_json"), [])
    answers = safe_json_loads(attempt.get("answers_json"), {})

    if not isinstance(question_order, list) or not question_order:
        question_order = list(range(len(quiz.questions)))

    if not isinstance(answers, dict):
        answers = {}

    scores: dict[str, dict[str, int]] = {}

    for original_question_index in question_order:
        original_question_index = int(original_question_index)
        question = quiz.questions[original_question_index]
        qid = question_id(question, original_question_index)
        category = str(question.get("category") or "Uncategorized").strip() or "Uncategorized"

        if category not in scores:
            scores[category] = {
                "correct": 0,
                "total": 0,
            }

        scores[category]["total"] += 1

        answer = answers.get(qid, {})
        if isinstance(answer, dict) and answer.get("is_correct"):
            scores[category]["correct"] += 1

    result: list[dict[str, Any]] = []

    for category in quiz.category_order:
        if category not in scores:
            continue

        correct = int(scores[category]["correct"])
        total = int(scores[category]["total"])
        percent = (correct / total * 100.0) if total else 0.0

        result.append(
            {
                "category": category,
                "correct": correct,
                "total": total,
                "percent": percent,
            }
        )

    for category, row in scores.items():
        if any(item["category"] == category for item in result):
            continue

        correct = int(row["correct"])
        total = int(row["total"])
        percent = (correct / total * 100.0) if total else 0.0

        result.append(
            {
                "category": category,
                "correct": correct,
                "total": total,
                "percent": percent,
            }
        )

    return result

def submit_attempt(attempt_id: int) -> dict[str, Any]:
    attempt = fetch_attempt(attempt_id)

    if attempt is None:
        raise ASVABQuizError("ASVAB attempt not found.")

    if str(attempt.get("status")) != STATUS_STARTED:
        raise ASVABQuizError(f"This ASVAB attempt is already {attempt.get('status')}.")

    if attempt_is_expired(attempt):
        mark_attempt_incomplete(int(attempt["attempt_id"]))
        raise ASVABQuizExpiredError("The ASVAB timer expired. This attempt was marked Incomplete.")

    unanswered = unanswered_question_count(attempt)

    if unanswered > 0:
        raise ASVABQuizError(f"You still have {unanswered} unanswered question(s).")

    answers = safe_json_loads(attempt.get("answers_json"), {})

    if not isinstance(answers, dict):
        answers = {}

    total = int(attempt.get("total_questions") or 0)
    correct = sum(1 for answer in answers.values() if answer.get("is_correct"))
    score = (correct / total * 100.0) if total else 0.0
    ts = now_ts()

    with get_connection() as conn:
        conn.execute(
            f"""
            UPDATE {TABLE_NAME}
            SET status = ?,
                score_percent = ?,
                correct_count = ?,
                category_scores_json = ?,
                completed_at = ?,
                updated_at = ?
            WHERE attempt_id = ?
              AND status = ?
            """,
            (
                STATUS_COMPLETE,
                float(score),
                int(correct),
                dumps_json(calculate_category_score_summary(attempt)),
                ts,
                ts,
                int(attempt_id),
                STATUS_STARTED,
            ),
        )

    updated = fetch_attempt(attempt_id)

    if updated is None:
        raise ASVABQuizError("ASVAB attempt disappeared after submit.")

    return updated
