from __future__ import annotations

import math
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

from services.qualification_record_service import (
    EWQuizAnswerRecord,
    EWQuizAttemptRecord,
    FilingCabinetUserStats,
    FlightLeadReviewRecord,
    QualAttemptRecord,
    get_ew_quiz_attempts_for_user,
    get_filing_cabinet_user_stats,
    get_qualification_attempts_for_user,
)
from services.permission_service import (
    require_instructor_command,
    member_is_admin,
)


ANSI_RESET = "\u001b[0m"
ANSI_RED = "\u001b[31m"
ANSI_GREEN = "\u001b[32m"
ANSI_YELLOW = "\u001b[33m"
ANSI_BOLD = "\u001b[1m"


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
        f"Attends: **{int(stats.attends)}**   Unique ops: **{int(stats.unique_ops)}**",
    ]

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


def record_created_timestamp(record: Any) -> int:
    if isinstance(record, EWQuizAttemptRecord):
        return int(record.started_at or record.updated_at or record.completed_at or 0)

    if isinstance(record, FlightLeadReviewRecord):
        return int(record.scheduled_at or 0)

    return int(record.created_at or record.updated_at or 0)


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

    for index, attempt in enumerate(attempts):
        selected_marker = ">" if index == selected_index else " "

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

        if isinstance(attempt, FlightLeadReviewRecord):
            color = fl_review_color(attempt.flight_lead_rating)

            lines.append(
                f"{color}{selected_marker}FL REV  "
                f"{int(attempt.leader_entry_id or attempt.entry_id):<5} "
                f"{'':<11} {fl_review_menu_stars(attempt.flight_lead_rating)}{ANSI_RESET}"
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

    if len(lines) == 1:
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
        name="Flying",
        value=(
            build_section_line("Tanking", selected.tank_rating, selected.tank_remarks)
            + "\n\n"
            + build_section_line("Formation Flying", selected.formation_rating, selected.formation_remarks)
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

    embed.add_field(
        name="Weapons",
        value=(
            build_section_line("A/A Range", selected.aa_rating, selected.aa_remarks)
            + "\n\n"
            + build_section_line("A/G Range", selected.ag_rating, selected.ag_remarks)
        ),
        inline=False,
    )

    verdict_value = f"**Final Remarks:**\n> {remarks_or_none(selected.final_remarks)}"

    if selected.vibe_rating is not None or selected.vibe_remarks:
        verdict_value = (
            build_section_line("Vibes", selected.vibe_rating, selected.vibe_remarks)
            + "\n\n"
            + verdict_value
        )

    embed.add_field(
        name="Verdict",
        value=verdict_value,
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

    if isinstance(selected, EWQuizAttemptRecord):
        return build_ew_quiz_record_embed(
            target_user,
            attempts,
            selected_index,
            stats=stats,
            is_flight_lead=is_flight_lead,
        )

    if isinstance(selected, FlightLeadReviewRecord):
        return build_flight_lead_review_record_embed(
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
        attempts: list[Any],
        selected_index: int,
        stats: FilingCabinetUserStats,
        is_flight_lead: bool,
    ):
        super().__init__()

        self.owner_id = owner_id
        self.target_user = target_user
        self.attempts = attempts
        self.selected_index = selected_index
        self.stats = stats
        self.is_flight_lead = is_flight_lead

        self.add_item(PrevAttemptButton(disabled=len(attempts) <= 1))
        self.add_item(NextAttemptButton(disabled=len(attempts) <= 1))

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

    async def refresh(self, interaction: discord.Interaction):
        view = QualificationRecordView(
            owner_id=self.owner_id,
            target_user=self.target_user,
            attempts=self.attempts,
            selected_index=self.selected_index,
            stats=self.stats,
            is_flight_lead=self.is_flight_lead,
        )

        await interaction.response.edit_message(
            embed=build_record_embed(
                self.target_user,
                self.attempts,
                self.selected_index,
                stats=self.stats,
                is_flight_lead=self.is_flight_lead,
            ),
            view=bind_private_view(view, interaction.message),
        )


class PrevAttemptButton(discord.ui.Button):
    def __init__(self, disabled: bool):
        super().__init__(
            label="Prev",
            style=discord.ButtonStyle.primary,
            disabled=disabled,
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
        description="View a user's qualification attempts and remarks.",
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

        is_flight_lead = member_has_role_id(user, FLIGHT_LEAD_ROLE)
        stats = get_filing_cabinet_user_stats(
            str(user.id),
            include_flight_lead_reviews=is_flight_lead,
        )

        qual_attempts = get_qualification_attempts_for_user(str(user.id))
        ew_attempts = get_ew_quiz_attempts_for_user(str(user.id))
        fl_review_attempts = stats.flight_lead_reviews if is_flight_lead else []
        attempts: list[Any] = [*qual_attempts, *ew_attempts, *fl_review_attempts]

        # Filing cabinet list is always chronological: oldest at the top.
        attempts.sort(key=record_created_timestamp)

        if not attempts:
            await interaction.response.send_message(
                embed=build_empty_filing_cabinet_embed(
                    user,
                    stats=stats,
                    is_flight_lead=is_flight_lead,
                ),
                ephemeral=True,
            )
            return

        # Default selected record is still the newest/latest, while the list stays oldest-to-newest.
        selected_index = len(attempts) - 1

        view = QualificationRecordView(
            owner_id=interaction.user.id,
            target_user=user,
            attempts=attempts,
            selected_index=selected_index,
            stats=stats,
            is_flight_lead=is_flight_lead,
        )

        await interaction.response.send_message(
            embed=build_record_embed(
                user,
                attempts,
                selected_index,
                stats=stats,
                is_flight_lead=is_flight_lead,
            ),
            view=view,
            ephemeral=True,
        )
        await bind_view_to_original_response(interaction, view)


async def setup(bot: commands.Bot):
    await bot.add_cog(FilingCabinetCommands(bot))
