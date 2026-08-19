from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from config import (
    ADMIN_ROLE,
    FLIGHT_LEAD_ROLE,
    INSTRUCTOR_ROLE,
    MISSION_EXECUTER_ROLE,
)

try:
    from config import EW_QUALIFIED_ROLE
except ImportError:
    EW_QUALIFIED_ROLE = 0

try:
    from config import MISSION_EXECUTER_ROLES
except ImportError:
    MISSION_EXECUTER_ROLES = []
from services.private_view_service import (
    PrivateTimeoutView,
    bind_private_view,
    bind_view_to_original_response,
)

from services.availability_service import (
    AvailabilityDay,
    DAY_NAMES,
    STATUS_COMPLETE,
    get_availability_days,
    windows_to_text,
)
from services.qualification_record_service import (
    ASVABAnswerRecord,
    ASVABAttemptRecord,
    EWQuizAnswerRecord,
    EWQuizAttemptRecord,
    FilingCabinetQualRequestRecord,
    FilingCabinetAttendanceRecord,
    FilingCabinetUserStats,
    FlightLeadReviewRecord,
    QualAttemptRecord,
    UserNoteRecord,
    get_asvab_attempts_for_user,
    get_ew_quiz_attempts_for_user,
    get_filing_cabinet_user_stats,
    get_mia_and_denied_qual_requests_for_user,
    get_qualification_attempts_for_user,
    attendance_records_for_user,
    add_user_note,
    get_user_notes_for_user,
)
from services.permission_service import (
    require_instructor_command,
    member_is_admin,
)
from services.user_settings_service import ensure_user_and_settings


ANSI_RESET = "\u001b[0m"
ANSI_RED = "\u001b[31m"
ANSI_GREEN = "\u001b[32m"
ANSI_YELLOW = "\u001b[33m"
ANSI_BOLD = "\u001b[1m"
ANSI_WHITE = "\u001b[37m"


RATING_LABELS = {
    0: "⚪ N/A",
    1: "🔴 Red",
    2: "🟠 Orange",
    3: "🟡 Yellow",
    4: "🟢 Green",

    # Legacy support from older paperwork builds.
    5: "💻 Computer",
}


RATING_MENU_EMOJI = {
    0: "⚪",
    1: "🔴",
    2: "🟠",
    3: "🟡",
    4: "🟢",

    # Legacy support from older paperwork builds.
    5: "💻",
}


QUAL_SCORE_KEY = "A/G A/A Form Tank Case1 Carrier Result"


FILTER_QUAL = "qual"
FILTER_ATTENDANCE = "attendance"
FILTER_REVIEWS = "reviews"
FILTER_NOTES = "notes"
FILTER_QUIZZES = "quizzes"

FILTER_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Qual", FILTER_QUAL),
    ("Attendance", FILTER_ATTENDANCE),
    ("Reviews", FILTER_REVIEWS),
    ("Notes", FILTER_NOTES),
    ("Quizzes", FILTER_QUIZZES),
)
FILTER_VALUES = frozenset(value for _, value in FILTER_OPTIONS)


@dataclass
class AvailabilityFormRecord:
    discord_id: str
    timezone: str | None
    days: list[AvailabilityDay]

    @property
    def updated_at(self) -> int | None:
        timestamps = [int(day.updated_at or 0) for day in self.days]
        latest = max(timestamps, default=0)
        return latest or None

    @property
    def status(self) -> str:
        if not self.days:
            return "missing"

        if all(day.status == STATUS_COMPLETE for day in self.days):
            return "complete"

        return "pending"


def has_instructor_role(member: discord.Member) -> bool:
    if member_is_admin(member):
        return True
    return any(role.id == INSTRUCTOR_ROLE for role in member.roles)




def member_has_role_id(member: discord.Member, role_id: int | str | None) -> bool:
    try:
        rid = int(role_id or 0)
    except (TypeError, ValueError):
        return False

    if rid <= 0:
        return False

    return any(int(role.id) == rid for role in member.roles)


def member_has_any_role_id(member: discord.Member, role_ids: list[int] | tuple[int, ...] | set[int]) -> bool:
    return any(member_has_role_id(member, role_id) for role_id in role_ids)


def mission_executer_role_ids() -> list[int]:
    role_ids: list[int] = []

    for value in [MISSION_EXECUTER_ROLE, *(MISSION_EXECUTER_ROLES or [])]:
        try:
            rid = int(value or 0)
        except (TypeError, ValueError):
            continue

        if rid and rid not in role_ids:
            role_ids.append(rid)

    return role_ids


def filing_cabinet_role_labels(member: discord.Member) -> list[str]:
    labels: list[str] = []

    if member_has_role_id(member, FLIGHT_LEAD_ROLE):
        labels.append("Flight Leader")

    if member_has_role_id(member, EW_QUALIFIED_ROLE):
        labels.append("EW Qualified")

    if member_has_any_role_id(member, mission_executer_role_ids()):
        labels.append("Mission Executer")

    if member_has_role_id(member, ADMIN_ROLE):
        labels.append("CIA")

    return labels


def star_rating_text(value: float | None) -> str:
    if value is None:
        return "No star ratings"

    rounded = int(round(float(value)))
    rounded = max(0, min(5, rounded))
    return "★" * rounded + "☆" * (5 - rounded) + f" {float(value):.1f}"


def fl_review_menu_stars(value: int | None) -> str:
    if value is None:
        return "☆☆☆☆☆"

    safe_value = max(0, min(5, int(value)))
    return "★" * safe_value + "☆" * (5 - safe_value)


def compact_review_text(value: str | None, *, limit: int = 180) -> str:
    text = " ".join(str(value or "").strip().split())

    if not text:
        return ""

    if len(text) <= limit:
        return text

    return text[: max(1, limit - 1)] + "…"


def format_review_timestamp(value: int | None) -> str:
    if not value:
        return "Unknown date"

    try:
        return datetime.fromtimestamp(int(value)).strftime("%m-%d-%Y")
    except Exception:
        return "Unknown date"


def build_flight_lead_review_lines(reviews: list[FlightLeadReviewRecord]) -> list[str]:
    lines: list[str] = []

    if not reviews:
        lines.append("No flight lead reviews found.")
        return lines

    for review in reviews:
        rating = (
            star_rating_text(float(review.flight_lead_rating))
            if review.flight_lead_rating is not None
            else "No rating"
        )
        op_name = review.op_name or "Unknown Op"
        reviewer = review.reviewer_name or review.reviewer_discord_id or "Unknown"
        slot = f" [{review.reviewer_slot}]" if review.reviewer_slot else ""

        lines.append(
            f"{format_review_timestamp(review.scheduled_at)} | {op_name} | {reviewer}{slot} | {rating}"
        )

        remarks = compact_review_text(review.fl_remarks)

        if remarks:
            lines.append(f"  {remarks}")

    return lines


def build_filing_cabinet_summary(
    target_user: discord.Member,
    stats: FilingCabinetUserStats,
    *,
    is_flight_lead: bool,
) -> str:
    role_labels = filing_cabinet_role_labels(target_user)
    roles_text = " - ".join(role_labels) if role_labels else "No tracked special roles"

    lines = [
        f"User: {target_user.mention}",
        roles_text,
        f"Timezone: **{stats.timezone or 'Not set'}**",
        f"Attends: **{int(stats.attends)}**   Unique ops: **{int(stats.unique_ops)}**",
        f"FTR: **{int(stats.ftr_count)}**   DNF: **{int(stats.dnf_count)}**",
    ]

    if stats.promotion_cap:
        lines.append(f"Promotion cap: **{stats.promotion_cap}**")

    if is_flight_lead:
        lines.append(
            "Flight lead rating: "
            f"**{star_rating_text(stats.flight_lead_rating_average)}** "
            f"({int(stats.flight_lead_review_count)} reviews)"
        )

    return "\n".join(lines)

def normalized_score_value(value: int | None) -> int:
    """
    Current scale:
    NULL/0 = N/A and counts as 0
    1 = Red
    2 = Orange
    3 = Yellow
    4 = Green

    Legacy:
    5 used to mean Computer, so treat it like 0 for scoring.
    """
    if value is None:
        return 0

    if int(value) == 5:
        return 0

    return max(0, min(4, int(value)))


def rating_text(value: int | None) -> str:
    if value is None:
        return "⚪ N/A"

    return RATING_LABELS.get(int(value), str(value))


def rating_menu_emoji(value: int | None) -> str:
    if value is None:
        return "⚪"

    return RATING_MENU_EMOJI.get(int(value), "⚪")


def score_sum(attempt: QualAttemptRecord) -> int:
    values = [
        attempt.ag_rating,
        attempt.aa_rating,
        attempt.formation_rating,
        attempt.tank_rating,
        attempt.case1_rating,
        attempt.carrier_rating,
    ]

    return sum(normalized_score_value(value) for value in values if value is not None)


def max_score(attempt: QualAttemptRecord) -> int:
    values = [
        attempt.ag_rating,
        attempt.aa_rating,
        attempt.formation_rating,
        attempt.tank_rating,
        attempt.case1_rating,
        attempt.carrier_rating,
    ]

    filled_count = sum(1 for value in values if value is not None)

    return filled_count * 4


def result_text(attempt: QualAttemptRecord) -> str:
    if attempt.passed is True:
        return "pass"

    if attempt.passed is False:
        return "fail"

    return "unknown"


def result_emoji(attempt: QualAttemptRecord) -> str:
    if attempt.passed is True:
        return "✅"

    if attempt.passed is False:
        return "❌"

    return "⬜"


def is_ew_quiz_record(record: Any) -> bool:
    return isinstance(record, EWQuizAttemptRecord)


def is_asvab_record(record: Any) -> bool:
    return isinstance(record, ASVABAttemptRecord)


def record_created_timestamp(record: Any) -> int:
    if isinstance(record, FilingCabinetQualRequestRecord):
        return int(record.updated_at or record.created_at or 0)

    if isinstance(record, (EWQuizAttemptRecord, ASVABAttemptRecord)):
        return int(record.started_at or record.updated_at or record.completed_at or 0)

    if isinstance(record, FilingCabinetAttendanceRecord):
        return int(record.scheduled_at or 0)

    if isinstance(record, FlightLeadReviewRecord):
        return int(record.scheduled_at or 0)

    if isinstance(record, UserNoteRecord):
        return int(record.created_at or 0)

    if isinstance(record, AvailabilityFormRecord):
        return int(record.updated_at or 0)

    return int(record.created_at or record.updated_at or 0)


def record_filter_category(record: Any) -> str | None:
    if isinstance(record, (QualAttemptRecord, FilingCabinetQualRequestRecord)):
        return FILTER_QUAL

    if isinstance(record, FilingCabinetAttendanceRecord):
        return FILTER_ATTENDANCE

    if isinstance(record, FlightLeadReviewRecord):
        return FILTER_REVIEWS

    if isinstance(record, UserNoteRecord):
        return FILTER_NOTES

    if isinstance(record, (EWQuizAttemptRecord, ASVABAttemptRecord)):
        return FILTER_QUIZZES

    # The availability form remains visible only while every filter is selected.
    return None


def filter_filing_cabinet_records(
    records: list[Any],
    active_filters: set[str] | frozenset[str],
) -> list[Any]:
    selected = set(active_filters) & set(FILTER_VALUES)
    include_uncategorized = selected == set(FILTER_VALUES)

    return [
        record
        for record in records
        if (category := record_filter_category(record)) in selected
        or (category is None and include_uncategorized)
    ]


def ew_score_text(record: EWQuizAttemptRecord) -> str:
    if record.score_percent is None:
        return "Not scored"

    return f"{float(record.score_percent):.1f}%"


def ew_status_emoji(record: EWQuizAttemptRecord) -> str:
    status = record.status.lower()

    if status == "passed":
        return "✅"

    if status == "fail":
        return "❌"

    if status == "incomplete":
        return "⏱️"

    if status == "started":
        return "▶️"

    return "⬜"


def ew_status_color(record: EWQuizAttemptRecord) -> str:
    status = record.status.lower()

    if status == "passed":
        return ANSI_GREEN

    return ANSI_RED


def asvab_score_text(record: ASVABAttemptRecord) -> str:
    if record.score_percent is None:
        return "Not scored"

    return f"{float(record.score_percent):.1f}%"


def asvab_status_emoji(record: ASVABAttemptRecord) -> str:
    status = record.status.lower()

    if status == "complete":
        return "✅"

    if status == "incomplete":
        return "⏱️"

    if status == "started":
        return "▶️"

    return "⬜"


def asvab_status_color(record: ASVABAttemptRecord) -> str:
    status = record.status.lower()

    if status == "complete":
        return ANSI_GREEN

    if status == "started":
        return ANSI_YELLOW

    return ANSI_RED


def qual_request_status_color(record: FilingCabinetQualRequestRecord) -> str:
    return ANSI_YELLOW if record.status.lower() == "mia" else ANSI_RED


def qual_request_status_emoji(record: FilingCabinetQualRequestRecord) -> str:
    return "⚠️" if record.status.lower() == "mia" else "❌"


def yes_no_unknown(value: int | None) -> str:
    if value is None:
        return "Unknown"

    return "Yes" if int(value) else "No"


def qual_result_color(record: QualAttemptRecord) -> str:
    return ANSI_GREEN if record.passed is True else ANSI_RED


def fl_review_color(value: int | None) -> str:
    if value is None:
        return ANSI_RED

    stars = max(0, min(5, int(value)))

    if stars <= 1:
        return ANSI_RED

    if stars <= 3:
        return ANSI_YELLOW

    return ANSI_GREEN


def build_attempt_menu(
    attempts: list[Any],
    selected_index: int,
    *,
    stats: FilingCabinetUserStats | None = None,
    is_flight_lead: bool = False,
) -> str:
    lines: list[str] = [
        "Type    ID    Result       Score / Key",
    ]

    window_size = 24
    start_index = max(0, selected_index - (window_size // 2))
    end_index = min(len(attempts), start_index + window_size)
    start_index = max(0, end_index - window_size)

    if start_index > 0:
        lines.append(f"... {start_index} newer records ...")

    for index in range(start_index, end_index):
        attempt = attempts[index]
        selected_marker = ">" if index == selected_index else " "

        if isinstance(attempt, AvailabilityFormRecord):
            color = ANSI_GREEN if attempt.status == "complete" else ANSI_YELLOW
            lines.append(
                f"{color}{selected_marker}Availability Form{ANSI_RESET}"
            )
            continue

        if isinstance(attempt, EWQuizAttemptRecord):
            color = ew_status_color(attempt)
            score = ew_score_text(attempt)

            lines.append(
                f"{color}{selected_marker}EW      {attempt.attempt_id:<5} "
                f"{attempt.status:<11} {score:<10} "
                f"{attempt.correct_count}/{attempt.total_questions} "
                f"{ew_status_emoji(attempt)}{ANSI_RESET}"
            )
            continue

        if isinstance(attempt, ASVABAttemptRecord):
            color = asvab_status_color(attempt)
            score = asvab_score_text(attempt)

            lines.append(
                f"{color}{selected_marker}ASVAB   {attempt.attempt_id:<5} "
                f"{attempt.status:<11} {score:<10} "
                f"{attempt.correct_count}/{attempt.total_questions} "
                f"{asvab_status_emoji(attempt)}{ANSI_RESET}"
            )
            continue

        if isinstance(attempt, FilingCabinetQualRequestRecord):
            color = qual_request_status_color(attempt)
            status = attempt.status.upper()

            lines.append(
                f"{color}{selected_marker}REQ     {attempt.id:<5} "
                f"{status:<11} {'':<10} "
                f"{qual_request_status_emoji(attempt)}{ANSI_RESET}"
            )
            continue

        if isinstance(attempt, FilingCabinetAttendanceRecord):
            landing = attempt.landing_type or "N/A"
            lines.append(
                f"{ANSI_WHITE}{selected_marker}ATTEND  {attempt.entry_id:<5} "
                f"{landing:<11} {attempt.op_name or 'Unknown'}{ANSI_RESET}"
            )
            continue

        if isinstance(attempt, FlightLeadReviewRecord):
            color = fl_review_color(attempt.flight_lead_rating)

            lines.append(
                f"{color}{selected_marker}FL REV  "
                f"{int(attempt.leader_entry_id or attempt.entry_id):<5} "
                f"{'':<11} {fl_review_menu_stars(attempt.flight_lead_rating)}{ANSI_RESET}"
            )
            continue

        if isinstance(attempt, UserNoteRecord):
            preview = (compact_review_text(attempt.remarks, limit=20) or "Note").replace("`", "'")
            lines.append(
                f"{ANSI_YELLOW}{selected_marker}NOTE    {attempt.id:<5} "
                f"{'':<11} {preview}{ANSI_RESET}"
            )
            continue

        result = result_text(attempt)
        color = qual_result_color(attempt)

        lines.append(
            f"{color}{selected_marker}QUAL    {attempt.id:<5} {result:<11} "
            f"{rating_menu_emoji(attempt.ag_rating)} "
            f"{rating_menu_emoji(attempt.aa_rating)} "
            f"{rating_menu_emoji(attempt.formation_rating)} "
            f"{rating_menu_emoji(attempt.tank_rating)} "
            f"{rating_menu_emoji(attempt.case1_rating)} "
            f"{rating_menu_emoji(attempt.carrier_rating)} "
            f"{result_emoji(attempt)}{ANSI_RESET}"
        )

    if end_index < len(attempts):
        lines.append(f"... {len(attempts) - end_index} older records ...")

    if len(attempts) == 0:
        lines.append("No filing cabinet records found.")

    return "```ansi\n" + "\n".join(lines)[:3800] + "\n```"


def timestamp_text(timestamp: int | None) -> str:
    if not timestamp:
        return "Unknown"

    return f"<t:{int(timestamp)}:f>"


def remarks_or_none(value: str | None) -> str:
    if not value:
        return "None"

    return value[:1000]


def build_section_line(
    label: str,
    rating: int | None,
    remarks: str | None,
) -> str:
    return (
        f"**{label}:** {rating_text(rating)}\n"
        f"> {remarks_or_none(remarks)}"
    )


def build_qual_request_record_embed(
    target_user: discord.Member,
    attempts: list[Any],
    selected_index: int,
    *,
    stats: FilingCabinetUserStats,
    is_flight_lead: bool,
) -> discord.Embed:
    selected = attempts[selected_index]
    assert isinstance(selected, FilingCabinetQualRequestRecord)

    embed = discord.Embed(
        title="Filing Cabinet",
        description=(
            f"{build_filing_cabinet_summary(target_user, stats, is_flight_lead=is_flight_lead)}\n\n"
            f"{build_attempt_menu(attempts, selected_index, stats=stats, is_flight_lead=is_flight_lead)}"
        ),
    )

    actioned_by = (
        f"<@{selected.action_by_discord_id}>"
        if selected.action_by_discord_id
        else "Unknown"
    )

    embed.add_field(
        name="Qualification Request",
        value=(
            f"**Request ID:** {selected.id}\n"
            f"**Status:** {selected.status.upper()}\n"
            f"**Submitted:** {timestamp_text(selected.created_at)}\n"
            f"**Actioned/Updated:** {timestamp_text(selected.updated_at)}\n"
            f"**Actioned By:** {actioned_by}\n"
            f"**Times Pinged:** {selected.times_pinged}"
        ),
        inline=False,
    )

    embed.add_field(
        name="Application",
        value=(
            f"**Meets Minimum Requirements:** {selected.min_requirements or 'Unknown'}\n"
            f"**Of Age:** {yes_no_unknown(selected.of_age)}\n"
            f"**VTOL Hours:** {selected.hours if selected.hours is not None else 'Unknown'}\n"
            f"**Preferred Aircraft:** {selected.preferred_aircraft or 'Not set'}\n"
            f"**Referral:** {selected.referral or 'None'}"
        ),
        inline=False,
    )

    embed.add_field(
        name="Availability",
        value=(
            f"**Timezone:** {selected.timezone or 'Not set'}\n"
            f"**Days:** {selected.dotw or 'Not set'}\n"
            f"**Window:** {selected.availability_start or 'Not set'}"
            f" – {selected.availability_end or 'Not set'}"
        ),
        inline=False,
    )

    embed.add_field(
        name="MIA / Denial Remarks",
        value=f"> {remarks_or_none(selected.action_remarks)}",
        inline=False,
    )

    embed.set_footer(
        text="REQ records are qualification requests currently marked MIA or Denied."
    )

    return embed


def build_qual_record_embed(
    target_user: discord.Member,
    attempts: list[Any],
    selected_index: int,
    *,
    stats: FilingCabinetUserStats,
    is_flight_lead: bool,
) -> discord.Embed:
    selected = attempts[selected_index]

    embed = discord.Embed(
        title="Filing Cabinet",
        description=(
            f"{build_filing_cabinet_summary(target_user, stats, is_flight_lead=is_flight_lead)}\n\n"
            f"{build_attempt_menu(attempts, selected_index, stats=stats, is_flight_lead=is_flight_lead)}"
        ),
    )

    embed.add_field(
        name="Attempt Info",
        value=(
            f"**ID:** {selected.id}\n"
            f"**Result:** {result_text(selected).upper()}\n"
            f"**Score:** {score_sum(selected)} / {max_score(selected)}\n"
            f"**Instructor:** "
            f"{f'<@{selected.instructor_discord_id}>' if selected.instructor_discord_id else (selected.instructor_username or 'Unknown')}\n"
            f"**Created:** {timestamp_text(selected.created_at)}"
        ),
        inline=False,
    )

    embed.add_field(
        name="Weapons",
        value=(
            build_section_line("A/G Range", selected.ag_rating, selected.ag_remarks)
            + "\n\n"
            + build_section_line("A/A Range", selected.aa_rating, selected.aa_remarks)
        ),
        inline=False,
    )

    embed.add_field(
        name="Flying",
        value=(
            build_section_line("Formation Flying", selected.formation_rating, selected.formation_remarks)
            + "\n\n"
            + build_section_line("Tanking", selected.tank_rating, selected.tank_remarks)
        ),
        inline=False,
    )

    embed.add_field(
        name="Landing",
        value=(
            build_section_line("Case 1", selected.case1_rating, selected.case1_remarks)
            + "\n\n"
            + build_section_line("Carrier Landing", selected.carrier_rating, selected.carrier_remarks)
        ),
        inline=False,
    )

    result_value = (
        f"**Verdict:** {result_text(selected).upper()}\n\n"
        f"**Final Remarks:**\n> {remarks_or_none(selected.final_remarks)}"
    )

    if selected.vibe_rating is not None or selected.vibe_remarks:
        result_value = (
            build_section_line("Vibes", selected.vibe_rating, selected.vibe_remarks)
            + "\n\n"
            + result_value
        )

    embed.add_field(
        name="Result",
        value=result_value,
        inline=False,
    )

    embed.set_footer(
        text="Menu columns: A/G | A/A | Form | Tank | Case1 | Carrier | Result"
    )

    return embed


def chunk_lines(lines: list[str], limit: int = 950) -> list[str]:
    chunks: list[str] = []
    current = ""

    for line in lines:
        candidate = line if not current else current + "\n" + line

        if len(candidate) > limit and current:
            chunks.append(current)
            current = line
        else:
            current = candidate

    if current:
        chunks.append(current)

    return chunks


def answer_line(letter: str | None, text: str | None) -> str:
    if not text:
        return "None"

    if letter:
        return f"{letter}. {text}"

    return text


def missed_answer_summary_line(answer: EWQuizAnswerRecord) -> str:
    question_id = answer.question_id or "unknown"
    category = answer.category or "Uncategorized"
    selected = answer.selected_letter or "?"

    return f"{question_id}: {category} - {selected}"


def asvab_missed_answer_summary_line(answer: ASVABAnswerRecord) -> str:
    question_id = answer.question_id or "unknown"
    category = answer.category or "Uncategorized"
    selected = ", ".join(answer.selected_letters) or "?"

    return f"{question_id}: {category} - {selected}"


def build_ew_quiz_record_embed(
    target_user: discord.Member,
    attempts: list[Any],
    selected_index: int,
    *,
    stats: FilingCabinetUserStats,
    is_flight_lead: bool,
) -> discord.Embed:
    selected = attempts[selected_index]
    assert isinstance(selected, EWQuizAttemptRecord)

    score = ew_score_text(selected)
    missed = selected.missed_answers()

    embed = discord.Embed(
        title="Filing Cabinet",
        description=(
            f"{build_filing_cabinet_summary(target_user, stats, is_flight_lead=is_flight_lead)}\n\n"
            f"{build_attempt_menu(attempts, selected_index, stats=stats, is_flight_lead=is_flight_lead)}"
        ),
    )

    embed.add_field(
        name="EW Quiz Attempt",
        value=(
            f"**Attempt ID:** {selected.attempt_id}\n"
            f"**Status:** {selected.status.upper()}\n"
            f"**Score:** {score}\n"
            f"**Correct:** {selected.correct_count} / {selected.total_questions}\n"
            f"**Passing Score:** {selected.passing_score:g}%\n"
            f"**Version:** {selected.quiz_version}\n"
            f"**Role Awarded:** {'Yes' if selected.role_awarded else 'No'}\n"
            f"**Started:** {timestamp_text(selected.started_at)}\n"
            f"**Completed:** {timestamp_text(selected.completed_at)}"
        ),
        inline=False,
    )

    if not missed:
        embed.add_field(
            name="Missed Questions",
            value="No missed questions logged for this attempt.",
            inline=False,
        )
    else:
        lines = [missed_answer_summary_line(answer) for answer in missed]
        chunks = chunk_lines(lines)

        for index, chunk in enumerate(chunks[:5], start=1):
            field_name = "Missed Questions" if index == 1 else f"Missed Questions {index}"
            embed.add_field(
                name=field_name,
                value=chunk[:1024],
                inline=False,
            )

        if len(chunks) > 5:
            embed.add_field(
                name="Missed Questions Continued",
                value="...and more missed questions not shown due to Discord embed limits.",
                inline=False,
            )

    embed.set_footer(
        text="EW quiz records show missed question ID, category, and selected answer letter."
    )

    return embed


def build_asvab_record_embed(
    target_user: discord.Member,
    attempts: list[Any],
    selected_index: int,
    *,
    stats: FilingCabinetUserStats,
    is_flight_lead: bool,
) -> discord.Embed:
    selected = attempts[selected_index]
    assert isinstance(selected, ASVABAttemptRecord)

    score = asvab_score_text(selected)
    missed = selected.missed_answers()

    embed = discord.Embed(
        title="Filing Cabinet",
        description=(
            f"{build_filing_cabinet_summary(target_user, stats, is_flight_lead=is_flight_lead)}\n\n"
            f"{build_attempt_menu(attempts, selected_index, stats=stats, is_flight_lead=is_flight_lead)}"
        ),
    )

    embed.add_field(
        name="ASVAB Attempt",
        value=(
            f"**Attempt ID:** {selected.attempt_id}\n"
            f"**Status:** {selected.status.upper()}\n"
            f"**Score:** {score}\n"
            f"**Correct:** {selected.correct_count} / {selected.total_questions}\n"
            f"**Version:** {selected.quiz_version}\n"
            f"**Started:** {timestamp_text(selected.started_at)}\n"
            f"**Completed:** {timestamp_text(selected.completed_at)}"
        ),
        inline=False,
    )

    if selected.category_scores:
        category_lines = [
            (
                f"**{category.category}:** {category.percent:.1f}% "
                f"({category.correct}/{category.total})"
            )
            for category in selected.category_scores
        ]
        embed.add_field(
            name="Category Scores",
            value="\n".join(category_lines)[:1024],
            inline=False,
        )
    else:
        embed.add_field(
            name="Category Scores",
            value="No category score breakdown was recorded for this attempt.",
            inline=False,
        )

    if not missed:
        embed.add_field(
            name="Missed Questions",
            value="No missed questions logged for this attempt.",
            inline=False,
        )
    else:
        lines = [asvab_missed_answer_summary_line(answer) for answer in missed]
        chunks = chunk_lines(lines)

        for index, chunk in enumerate(chunks[:5], start=1):
            field_name = "Missed Questions" if index == 1 else f"Missed Questions {index}"
            embed.add_field(
                name=field_name,
                value=chunk[:1024],
                inline=False,
            )

        if len(chunks) > 5:
            embed.add_field(
                name="Missed Questions Continued",
                value="...and more missed questions not shown due to Discord embed limits.",
                inline=False,
            )

    embed.set_footer(
        text="ASVAB records show overall score, category scores, and missed question IDs."
    )

    return embed


def availability_form_line(day: AvailabilityDay) -> str:
    day_label = f"{DAY_NAMES[day.day_of_week]:<9} - "

    if day.windows:
        return f"{ANSI_WHITE}{day_label}{ANSI_GREEN}{windows_to_text(day.windows)}{ANSI_RESET}"

    return f"{ANSI_WHITE}{day_label}{ANSI_RED}Unavailable{ANSI_RESET}"


def availability_form_block(record: AvailabilityFormRecord) -> str:
    lines = [availability_form_line(day) for day in record.days]

    if not lines:
        lines.append(f"{ANSI_RED}No availability form found.{ANSI_RESET}")

    return "```ansi\n" + "\n".join(lines)[:950] + f"\n{ANSI_RESET}```"


def build_availability_form_embed(
    target_user: discord.Member,
    attempts: list[Any],
    selected_index: int,
    *,
    stats: FilingCabinetUserStats,
    is_flight_lead: bool,
) -> discord.Embed:
    selected = attempts[selected_index]
    assert isinstance(selected, AvailabilityFormRecord)

    embed = discord.Embed(
        title="Filing Cabinet",
        description=(
            f"{build_filing_cabinet_summary(target_user, stats, is_flight_lead=is_flight_lead)}\n\n"
            f"{build_attempt_menu(attempts, selected_index, stats=stats, is_flight_lead=is_flight_lead)}"
        ),
    )

    embed.add_field(
        name="Availability Form",
        value=availability_form_block(selected),
        inline=False,
    )

    embed.set_footer(text="Availability form times are shown in the user's saved timezone.")
    return embed


def build_flight_lead_review_record_embed(
    target_user: discord.Member,
    attempts: list[Any],
    selected_index: int,
    *,
    stats: FilingCabinetUserStats,
    is_flight_lead: bool,
) -> discord.Embed:
    selected = attempts[selected_index]
    assert isinstance(selected, FlightLeadReviewRecord)

    embed = discord.Embed(
        title="Filing Cabinet",
        description=(
            f"{build_filing_cabinet_summary(target_user, stats, is_flight_lead=is_flight_lead)}\n\n"
            f"{build_attempt_menu(attempts, selected_index, stats=stats, is_flight_lead=is_flight_lead)}"
        ),
    )

    reviewer = (
        f"<@{selected.reviewer_discord_id}>"
        if selected.reviewer_discord_id
        else (selected.reviewer_name or "Unknown")
    )

    rating = (
        star_rating_text(float(selected.flight_lead_rating))
        if selected.flight_lead_rating is not None
        else "No star rating"
    )

    embed.add_field(
        name="Flight Lead Review",
        value=(
            f"**FL Attend ID:** {int(selected.leader_entry_id or selected.entry_id)}\n"
            f"**Review Entry ID:** {int(selected.entry_id)}\n"
            f"**Op:** {selected.op_name or 'Unknown'}\n"
            f"**Date:** {timestamp_text(selected.scheduled_at)}\n"
            f"**Flight Lead Slot:** {selected.leader_slot or 'Unknown'}\n"
            f"**Reviewer:** {reviewer}\n"
            f"**Reviewer Slot:** {selected.reviewer_slot or 'Unknown'}\n"
            f"**Stars:** {rating}"
        ),
        inline=False,
    )

    embed.add_field(
        name="Remarks",
        value=f"> {remarks_or_none(selected.fl_remarks)}",
        inline=False,
    )

    embed.set_footer(text="FL review rows use the Flight Lead's 1-1 attendance ID in the menu.")

    return embed


def build_attendance_record_embed(
    target_user: discord.Member, attempts: list[Any], selected_index: int, *,
    stats: FilingCabinetUserStats, is_flight_lead: bool,
) -> discord.Embed:
    selected = attempts[selected_index]
    assert isinstance(selected, FilingCabinetAttendanceRecord)
    embed = discord.Embed(
        title="Filing Cabinet",
        description=(
            f"{build_filing_cabinet_summary(target_user, stats, is_flight_lead=is_flight_lead)}\n\n"
            f"{build_attempt_menu(attempts, selected_index, stats=stats, is_flight_lead=is_flight_lead)}"
        ),
    )
    embed.add_field(
        name="Attendance Record",
        value=(
            f"**Attendance ID:** {selected.entry_id}\n"
            f"**Event:** #{selected.scheduled_op_id or '—'} {selected.op_name or 'Unknown'}\n"
            f"**Date:** {timestamp_text(selected.scheduled_at)}\n"
            f"**Slot:** {selected.slot or 'N/A'}\n"
            f"**Aircraft:** {selected.aircraft or 'N/A'}\n"
            f"**Landing:** {selected.landing_type or 'N/A'}\n"
            f"**Wire:** {selected.wires if selected.wires is not None else 'N/A'}\n"
            f"**Bolters:** {selected.bolters if selected.bolters is not None else 'N/A'}\n"
            f"**Combat Deaths:** {selected.combat_deaths if selected.combat_deaths is not None else 'N/A'}\n"
            f"**Attend Type:** {selected.attend_type or 'N/A'}\n"
            f"**Status:** {selected.status or 'N/A'}"
        ), inline=False,
    )
    remarks = [x for x in [selected.op_remarks, selected.fl_remarks, selected.note_remarks] if x]
    if remarks:
        embed.add_field(name="Remarks", value="\n".join(f"> {x}" for x in remarks)[:1024], inline=False)
    return embed


def build_user_note_record_embed(
    target_user: discord.Member,
    attempts: list[Any],
    selected_index: int,
    *,
    stats: FilingCabinetUserStats,
    is_flight_lead: bool,
) -> discord.Embed:
    selected = attempts[selected_index]
    assert isinstance(selected, UserNoteRecord)

    embed = discord.Embed(
        title="Filing Cabinet",
        description=(
            f"{build_filing_cabinet_summary(target_user, stats, is_flight_lead=is_flight_lead)}\n\n"
            f"{build_attempt_menu(attempts, selected_index, stats=stats, is_flight_lead=is_flight_lead)}"
        ),
    )
    embed.add_field(
        name="User Note",
        value=(
            f"**Note ID:** {selected.id}\n"
            f"**Added By:** <@{selected.note_by}>\n"
            f"**Created:** {timestamp_text(selected.created_at)}"
        ),
        inline=False,
    )
    embed.add_field(
        name="Remarks",
        value=f"> {remarks_or_none(selected.remarks)}",
        inline=False,
    )
    return embed


def build_empty_filing_cabinet_embed(
    target_user: discord.Member,
    *,
    stats: FilingCabinetUserStats,
    is_flight_lead: bool,
) -> discord.Embed:
    return discord.Embed(
        title="Filing Cabinet",
        description=(
            f"{build_filing_cabinet_summary(target_user, stats, is_flight_lead=is_flight_lead)}\n\n"
            f"{build_attempt_menu([], 0, stats=stats, is_flight_lead=is_flight_lead)}"
        ),
    )


def build_record_embed(
    target_user: discord.Member,
    attempts: list[Any],
    selected_index: int,
    *,
    stats: FilingCabinetUserStats,
    is_flight_lead: bool,
) -> discord.Embed:
    selected = attempts[selected_index]

    if isinstance(selected, FilingCabinetQualRequestRecord):
        return build_qual_request_record_embed(
            target_user,
            attempts,
            selected_index,
            stats=stats,
            is_flight_lead=is_flight_lead,
        )

    if isinstance(selected, EWQuizAttemptRecord):
        return build_ew_quiz_record_embed(
            target_user,
            attempts,
            selected_index,
            stats=stats,
            is_flight_lead=is_flight_lead,
        )

    if isinstance(selected, ASVABAttemptRecord):
        return build_asvab_record_embed(
            target_user,
            attempts,
            selected_index,
            stats=stats,
            is_flight_lead=is_flight_lead,
        )

    if isinstance(selected, FilingCabinetAttendanceRecord):
        return build_attendance_record_embed(
            target_user, attempts, selected_index, stats=stats, is_flight_lead=is_flight_lead
        )

    if isinstance(selected, FlightLeadReviewRecord):
        return build_flight_lead_review_record_embed(
            target_user,
            attempts,
            selected_index,
            stats=stats,
            is_flight_lead=is_flight_lead,
        )

    if isinstance(selected, UserNoteRecord):
        return build_user_note_record_embed(
            target_user,
            attempts,
            selected_index,
            stats=stats,
            is_flight_lead=is_flight_lead,
        )

    if isinstance(selected, AvailabilityFormRecord):
        return build_availability_form_embed(
            target_user,
            attempts,
            selected_index,
            stats=stats,
            is_flight_lead=is_flight_lead,
        )

    return build_qual_record_embed(
        target_user,
        attempts,
        selected_index,
        stats=stats,
        is_flight_lead=is_flight_lead,
    )


class QualificationRecordView(PrivateTimeoutView):
    def __init__(
        self,
        owner_id: int,
        target_user: discord.Member,
        all_attempts: list[Any],
        stats: FilingCabinetUserStats,
        is_flight_lead: bool,
        *,
        active_filters: set[str] | None = None,
        selected_record: Any | None = None,
    ):
        super().__init__()

        self.owner_id = owner_id
        self.target_user = target_user
        self.all_attempts = sorted(
            all_attempts,
            key=record_created_timestamp,
            reverse=True,
        )
        self.stats = stats
        self.is_flight_lead = is_flight_lead
        self.active_filters = (
            set(active_filters) & set(FILTER_VALUES)
            if active_filters is not None
            else set(FILTER_VALUES)
        )
        if not self.active_filters:
            self.active_filters = set(FILTER_VALUES)

        self.attempts: list[Any] = []
        self.selected_index = 0
        self.rebuild_attempts(preserve_record=selected_record)

        self.add_item(FilingCabinetFilterSelect(self.active_filters))
        self.add_item(PrevAttemptButton(disabled=len(self.attempts) <= 1))
        self.add_item(NextAttemptButton(disabled=len(self.attempts) <= 1))
        self.add_item(AddNoteButton())

    @property
    def selected_record(self) -> Any | None:
        if not self.attempts:
            return None

        return self.attempts[self.selected_index]

    def rebuild_attempts(self, *, preserve_record: Any | None = None) -> None:
        self.attempts = filter_filing_cabinet_records(
            self.all_attempts,
            self.active_filters,
        )

        if not self.attempts:
            self.selected_index = 0
            return

        if preserve_record is not None:
            try:
                self.selected_index = self.attempts.index(preserve_record)
                return
            except ValueError:
                pass

        self.selected_index = 0

    def build_embed(self) -> discord.Embed:
        if not self.attempts:
            return build_empty_filing_cabinet_embed(
                self.target_user,
                stats=self.stats,
                is_flight_lead=self.is_flight_lead,
            )

        return build_record_embed(
            self.target_user,
            self.attempts,
            self.selected_index,
            stats=self.stats,
            is_flight_lead=self.is_flight_lead,
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the person who opened this record can use these controls.",
                ephemeral=True,
            )
            return False

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This can only be used inside the server.",
                ephemeral=True,
            )
            return False

        if not has_instructor_role(interaction.user):
            await interaction.response.send_message(
                "You need the instructor role to use this.",
                ephemeral=True,
            )
            return False

        return True

    async def refresh(self, interaction: discord.Interaction) -> None:
        view = QualificationRecordView(
            owner_id=self.owner_id,
            target_user=self.target_user,
            all_attempts=self.all_attempts,
            stats=self.stats,
            is_flight_lead=self.is_flight_lead,
            active_filters=self.active_filters,
            selected_record=self.selected_record,
        )

        await interaction.response.edit_message(
            embed=view.build_embed(),
            view=bind_private_view(view, interaction.message),
        )


class FilingCabinetFilterSelect(discord.ui.Select):
    def __init__(self, active_filters: set[str]):
        options = [
            discord.SelectOption(
                label=label,
                value=value,
                default=value in active_filters,
            )
            for label, value in FILTER_OPTIONS
        ]

        super().__init__(
            placeholder="Filter filing cabinet records",
            min_values=1,
            max_values=len(options),
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        assert isinstance(self.view, QualificationRecordView)

        previously_selected = self.view.selected_record
        self.view.active_filters = set(self.values)
        self.view.rebuild_attempts(preserve_record=previously_selected)
        await self.view.refresh(interaction)


class AddUserNoteModal(discord.ui.Modal, title="Add Note"):
    remarks = discord.ui.TextInput(
        label="Note",
        placeholder="Enter the note for this user.",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1000,
    )

    def __init__(self, parent_view: "QualificationRecordView"):
        super().__init__()
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member) or not has_instructor_role(interaction.user):
            await interaction.response.send_message(
                "You need admin or instructor permission to add notes.",
                ephemeral=True,
            )
            return

        try:
            note = add_user_note(
                note_by=str(interaction.user.id),
                note_for=str(self.parent_view.target_user.id),
                remarks=str(self.remarks.value),
            )
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        except Exception as error:
            await interaction.response.send_message(
                f"Failed to save the note: `{type(error).__name__}: {error}`",
                ephemeral=True,
            )
            return

        self.parent_view.all_attempts.append(note)
        self.parent_view.all_attempts.sort(
            key=record_created_timestamp,
            reverse=True,
        )
        self.parent_view.active_filters.add(FILTER_NOTES)
        self.parent_view.rebuild_attempts(preserve_record=note)
        await self.parent_view.refresh(interaction)


class AddNoteButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Add note",
            style=discord.ButtonStyle.success,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, QualificationRecordView)
        await interaction.response.send_modal(AddUserNoteModal(self.view))


class PrevAttemptButton(discord.ui.Button):
    def __init__(self, disabled: bool):
        super().__init__(
            label="Prev",
            style=discord.ButtonStyle.primary,
            disabled=disabled,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, QualificationRecordView)

        self.view.selected_index = (
            self.view.selected_index - 1
        ) % len(self.view.attempts)

        await self.view.refresh(interaction)


class NextAttemptButton(discord.ui.Button):
    def __init__(self, disabled: bool):
        super().__init__(
            label="Next",
            style=discord.ButtonStyle.primary,
            disabled=disabled,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, QualificationRecordView)

        self.view.selected_index = (
            self.view.selected_index + 1
        ) % len(self.view.attempts)

        await self.view.refresh(interaction)


class FilingCabinetCommands(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="filingcabinet",
        description="View a user's filing cabinet records.",
    )
    @app_commands.describe(user="User to view qualification records for")
    @app_commands.guild_only()
    async def filingcabinet_command(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
    ):
        if not await require_instructor_command(interaction):
            return
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This command can only be used inside the server.",
                ephemeral=True,
            )
            return

        if not has_instructor_role(interaction.user):
            await interaction.response.send_message(
                "You need the instructor role to use this command.",
                ephemeral=True,
            )
            return

        ensure_user_and_settings(user)

        is_flight_lead = member_has_role_id(user, FLIGHT_LEAD_ROLE)
        stats = get_filing_cabinet_user_stats(
            str(user.id),
            include_flight_lead_reviews=is_flight_lead,
        )

        qual_attempts = get_qualification_attempts_for_user(str(user.id))
        qual_request_records = get_mia_and_denied_qual_requests_for_user(str(user.id))
        ew_attempts = get_ew_quiz_attempts_for_user(str(user.id))
        asvab_attempts = get_asvab_attempts_for_user(str(user.id))
        fl_review_attempts = stats.flight_lead_reviews if is_flight_lead else []
        attendance_attempts = attendance_records_for_user(str(user.id))
        user_note_attempts = get_user_notes_for_user(str(user.id))
        availability_record = AvailabilityFormRecord(
            discord_id=str(user.id),
            timezone=stats.timezone,
            days=get_availability_days(str(user.id)),
        )
        attempts: list[Any] = [
            *qual_attempts,
            *qual_request_records,
            *ew_attempts,
            *asvab_attempts,
            *fl_review_attempts,
            *attendance_attempts,
            *user_note_attempts,
            availability_record,
        ]

        # Every record is ordered newest-to-oldest before filters are applied.
        attempts.sort(key=record_created_timestamp, reverse=True)

        view = QualificationRecordView(
            owner_id=interaction.user.id,
            target_user=user,
            all_attempts=attempts,
            stats=stats,
            is_flight_lead=is_flight_lead,
            selected_record=attempts[0] if attempts else None,
        )

        await interaction.response.send_message(
            embed=view.build_embed(),
            view=view,
            ephemeral=True,
        )
        await bind_view_to_original_response(interaction, view)


async def setup(bot: commands.Bot):
    await bot.add_cog(FilingCabinetCommands(bot))
