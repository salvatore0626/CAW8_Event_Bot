from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from config import INSTRUCTOR_ROLE, MIN_VTOL_HOURS
from services.permission_service import (
    require_instructor_command,
    member_is_admin,
)

try:
    from config import MISSION_QUALIFIED_ROLE
except ImportError:
    MISSION_QUALIFIED_ROLE = 0

from services.paperwork_service import (
    PaperworkDraft,
    PendingQualApplicant,
    PreviousQualAttempt,
    apply_previous_green_defaults,
    clean_optional_text,
    create_qual_log_record,
    get_pending_qual_applicant,
    get_previous_qual_attempts_for_user,
    mark_request_completed_if_passed,
    search_pending_qual_applicants,
)


active_paperwork_drafts: dict[tuple[int, int], PaperworkDraft] = {}
active_previous_attempts: dict[tuple[int, int], list[PreviousQualAttempt]] = {}


RATING_OPTIONS = [
    discord.SelectOption(label="N/A", value="0", emoji="⚪"),
    discord.SelectOption(label="Red", value="1", emoji="🔴"),
    discord.SelectOption(label="Orange", value="2", emoji="🟠"),
    discord.SelectOption(label="Yellow", value="3", emoji="🟡"),
    discord.SelectOption(label="Green", value="4", emoji="🟢"),
]


VIBE_RATING_OPTIONS = [
    discord.SelectOption(label="Red", value="1", emoji="🔴"),
    discord.SelectOption(label="Orange", value="2", emoji="🟠"),
    discord.SelectOption(label="Yellow", value="3", emoji="🟡"),
    discord.SelectOption(label="Green", value="4", emoji="🟢"),
]


RESULT_OPTIONS = [
    discord.SelectOption(label="Pass", value="pass", emoji="✅"),
    discord.SelectOption(label="Fail", value="fail", emoji="❌"),
]


RATING_LABELS = {
    None: "Not set",
    0: "⚪ N/A",
    1: "🔴 Red",
    2: "🟠 Orange",
    3: "🟡 Yellow",
    4: "🟢 Green",
}


def has_instructor_role(member: discord.Member) -> bool:
    if member_is_admin(member):
        return True
    return any(role.id == INSTRUCTOR_ROLE for role in member.roles)


def draft_key(instructor_id: int, applicant_id: int) -> tuple[int, int]:
    return (instructor_id, applicant_id)


def rating_text(value: int | None) -> str:
    return RATING_LABELS.get(value, str(value))


def result_text(value: bool | None) -> str:
    if value is None:
        return "Not set"

    return "✅ Pass" if value else "❌ Fail"



def format_application_hours(value: float | None) -> str:
    if value is None:
        return "Not set"

    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return str(value)

    if numeric.is_integer():
        return str(int(numeric))

    return str(numeric)


def application_warning_lines(
    applicant: PendingQualApplicant,
) -> list[str]:
    warnings: list[str] = []

    if applicant.of_age is not True:
        warnings.append("Age: Under the age for Discord TOS")

    try:
        applicant_hours = float(applicant.hours) if applicant.hours is not None else None
        required_hours = float(MIN_VTOL_HOURS)
    except (TypeError, ValueError):
        applicant_hours = None
        required_hours = float(MIN_VTOL_HOURS or 0)

    if applicant_hours is None or applicant_hours < required_hours:
        warnings.append(
            "Below minimum hours required for CAW8 Mission Qualification"
        )

    return warnings


def build_application_warning_embed(
    *,
    member: discord.Member,
    applicant: PendingQualApplicant,
) -> discord.Embed:
    warnings = application_warning_lines(applicant)
    warning_text = "\n".join(warnings) if warnings else "No warning."

    embed = discord.Embed(
        title="Application Warning",
        description=(
            f"User **{member.display_name}** does not meet the following requirements:\n\n"
            f"```text\n{warning_text}\n```\n"
            f"Age 13+: **{'Yes' if applicant.of_age is True else 'No'}**\n"
            f"VTOL Hours: **{format_application_hours(applicant.hours)} / {format_application_hours(MIN_VTOL_HOURS)} required**"
        ),
        color=discord.Color.orange(),
    )

    return embed


def set_rating(draft: PaperworkDraft, field_name: str, rating: int) -> None:
    setattr(draft, field_name, rating)


def page_title(page: int) -> str:
    titles = {
        1: "Weapon Deployment",
        2: "Flying",
        3: "Landing",
        4: "Verdict",
        5: "Review",
    }

    return titles.get(page, "Paperwork")


def remarks_status(value: str | None) -> str:
    if not value:
        return "None"

    return value[:900]


def rating_emoji(value: int | None) -> str:
    return {
        None: "⬜",
        0: "⚪",
        1: "🔴",
        2: "🟠",
        3: "🟡",
        4: "🟢",
    }.get(value, "⬜")


def build_rating_row(draft: PaperworkDraft) -> str:
    return (
        f"{rating_emoji(draft.ag_rating)} "
        f"{rating_emoji(draft.aa_rating)} "
        f"{rating_emoji(draft.formation_rating)} "
        f"{rating_emoji(draft.tank_rating)} "
        f"{rating_emoji(draft.case1_rating)} "
        f"{rating_emoji(draft.carrier_rating)} "
        f"{rating_emoji(draft.vibe_rating)} "
        f"{'✅' if draft.passed is True else '❌' if draft.passed is False else '⬜'}"
    )



def previous_result_word(attempt: PreviousQualAttempt) -> str:
    if attempt.passed is True:
        return "pass"

    if attempt.passed is False:
        return "fail"

    return "unknown"


def previous_result_emoji(attempt: PreviousQualAttempt) -> str:
    if attempt.passed is True:
        return "✅"

    if attempt.passed is False:
        return "❌"

    return "⬜"


def build_previous_attempts_block(
    attempts: list[PreviousQualAttempt],
) -> str:
    if not attempts:
        return "None"

    lines = [
        "Key: A/G A/A Form Tank Case1 Carrier Vibe Result",
    ]

    for index, attempt in enumerate(attempts, start=1):
        lines.append(
            f"{index:<2} {previous_result_word(attempt):<7} "
            f"{rating_emoji(attempt.ag_rating)} "
            f"{rating_emoji(attempt.aa_rating)} "
            f"{rating_emoji(attempt.formation_rating)} "
            f"{rating_emoji(attempt.tank_rating)} "
            f"{rating_emoji(attempt.case1_rating)} "
            f"{rating_emoji(attempt.carrier_rating)} "
            f"{rating_emoji(attempt.vibe_rating)} "
            f"{previous_result_emoji(attempt)}"
        )

    block = "```text\n" + "\n".join(lines) + "\n```"

    # Keep embeds from getting too large. Show the most recent rows if needed.
    if len(block) > 950:
        trimmed = [lines[0], "..."]
        trimmed.extend(lines[-8:])
        block = "```text\n" + "\n".join(trimmed) + "\n```"

    return block


def get_attempts_for_draft(
    owner_id: int,
    draft: PaperworkDraft,
) -> list[PreviousQualAttempt]:
    return active_previous_attempts.get(
        draft_key(owner_id, int(draft.applicant_discord_id)),
        [],
    )


def build_review_remarks(draft: PaperworkDraft) -> str:
    lines: list[str] = []

    remark_rows = [
        ("A/G", draft.ag_remarks),
        ("A/A", draft.aa_remarks),
        ("Formation", draft.formation_remarks),
        ("Tanker", draft.tank_remarks),
        ("Case 1", draft.case1_remarks),
        ("Carrier", draft.carrier_remarks),
        ("Vibe", draft.vibe_remarks),
        ("Verdict", draft.verdict_remarks),
    ]

    for label, remarks in remark_rows:
        if remarks:
            lines.append(f"**{label}:** {remarks}")

    if not lines:
        return "None"

    return "\n\n".join(lines)[:1024]


def build_page_embed(draft: PaperworkDraft, page: int, previous_attempts: list[PreviousQualAttempt] | None = None) -> discord.Embed:
    if previous_attempts is None:
        previous_attempts = []

    title = f"Paperwork - {page_title(page)}"

    if page == 5:
        description = (
            f"Applicant: <@{draft.applicant_discord_id}>\n\n"
            f"**Scores:**\n"
            f"{build_rating_row(draft)}\n"
            f"`A/G A/A Form Tank Case1 Carrier Vibe Result`\n\n"
            "Review the ratings and remarks before submitting."
        )
    else:
        description = (
            f"Applicant: <@{draft.applicant_discord_id}>\n\n"
            "Select the ratings for this page. Use **Remarks** for notes."
        )

    embed = discord.Embed(
        title=title,
        description=description,
    )

    if page == 1:
        embed.add_field(
            name="Weapon Deployment",
            value=(
                f"**Air to Ground:** {rating_text(draft.ag_rating)}\n"
                f"**A/G Remarks:** {remarks_status(draft.ag_remarks)}\n\n"
                f"**Air to Air:** {rating_text(draft.aa_rating)}\n"
                f"**A/A Remarks:** {remarks_status(draft.aa_remarks)}"
            ),
            inline=False,
        )

    elif page == 2:
        embed.add_field(
            name="Flying",
            value=(
                f"**Formation:** {rating_text(draft.formation_rating)}\n"
                f"**Formation Remarks:** {remarks_status(draft.formation_remarks)}\n\n"
                f"**Tanker:** {rating_text(draft.tank_rating)}\n"
                f"**Tanker Remarks:** {remarks_status(draft.tank_remarks)}"
            ),
            inline=False,
        )

    elif page == 3:
        embed.add_field(
            name="Landing",
            value=(
                f"**Case 1:** {rating_text(draft.case1_rating)}\n"
                f"**Case 1 Remarks:** {remarks_status(draft.case1_remarks)}\n\n"
                f"**Carrier:** {rating_text(draft.carrier_rating)}\n"
                f"**Carrier Remarks:** {remarks_status(draft.carrier_remarks)}"
            ),
            inline=False,
        )

    elif page == 4:
        embed.add_field(
            name="Verdict",
            value=(
                f"**Vibe:** {rating_text(draft.vibe_rating)}\n"
                f"**Vibe Remarks:** {remarks_status(draft.vibe_remarks)}\n\n"
                f"**Result:** {result_text(draft.passed)}\n"
                f"**Verdict Remarks:** {remarks_status(draft.verdict_remarks)}"
            ),
            inline=False,
        )

    elif page == 5:
        embed.add_field(
            name="Ratings",
            value=(
                f"**Air to Ground:** {rating_text(draft.ag_rating)}\n"
                f"**Air to Air:** {rating_text(draft.aa_rating)}\n"
                f"**Formation:** {rating_text(draft.formation_rating)}\n"
                f"**Tanker:** {rating_text(draft.tank_rating)}\n"
                f"**Case 1:** {rating_text(draft.case1_rating)}\n"
                f"**Carrier:** {rating_text(draft.carrier_rating)}\n"
                f"**Vibe:** {rating_text(draft.vibe_rating)}\n"
                f"**Result:** {result_text(draft.passed)}"
            ),
            inline=False,
        )

        embed.add_field(
            name="Remarks",
            value=build_review_remarks(draft),
            inline=False,
        )

        errors = draft.validate()
        if errors:
            embed.add_field(
                name="Missing Before Submit",
                value="\n".join(f"- {error}" for error in errors)[:1024],
                inline=False,
            )
        else:
            embed.add_field(
                name="Ready",
                value="All required fields are filled out.",
                inline=False,
            )

    if previous_attempts:
        embed.add_field(
            name="Previous Qual Attempts",
            value=build_previous_attempts_block(previous_attempts),
            inline=False,
        )

    embed.set_footer(text="0=N/A, 1=Red, 2=Orange, 3=Yellow, 4=Green. N/A saves as NULL in qual_log.")

    return embed


async def give_mission_qualified_role(
    interaction: discord.Interaction,
    applicant_discord_id: str,
) -> str:
    if interaction.guild is None:
        return "Role not assigned: command was not used inside a guild."

    if not MISSION_QUALIFIED_ROLE:
        return "Role not assigned: MISSION_QUALIFIED_ROLE is not set in config.py."

    role = interaction.guild.get_role(int(MISSION_QUALIFIED_ROLE))

    if role is None:
        return "Role not assigned: MISSION_QUALIFIED_ROLE was not found in this server."

    try:
        member = interaction.guild.get_member(int(applicant_discord_id))

        if member is None:
            member = await interaction.guild.fetch_member(int(applicant_discord_id))

        await member.add_roles(
            role,
            reason="Passed qualification paperwork.",
        )

        return f"Role assigned: {role.mention}"
    except discord.Forbidden:
        return "Role not assigned: bot does not have permission or role hierarchy is too low."
    except Exception as error:
        return f"Role not assigned: {error}"


class PaperworkView(discord.ui.View):
    def __init__(
        self,
        owner_id: int,
        draft: PaperworkDraft,
        page: int,
    ):
        super().__init__(timeout=1800)

        self.owner_id = owner_id
        self.draft = draft
        self.page = page

        if page == 1:
            self.add_item(RatingSelect("ag_rating", "Air to Ground", draft.ag_rating, row=0))
            self.add_item(RatingSelect("aa_rating", "Air to Air", draft.aa_rating, row=1))
            self.add_item(CancelButton(row=2))
            self.add_item(RemarksButton(row=2))
            self.add_item(NextButton(row=2))

        elif page == 2:
            self.add_item(RatingSelect("formation_rating", "Formation", draft.formation_rating, row=0))
            self.add_item(RatingSelect("tank_rating", "Tanker", draft.tank_rating, row=1))
            self.add_item(BackButton(row=2))
            self.add_item(CancelButton(row=2))
            self.add_item(RemarksButton(row=2))
            self.add_item(NextButton(row=2))

        elif page == 3:
            self.add_item(RatingSelect("case1_rating", "Case 1", draft.case1_rating, row=0))
            self.add_item(RatingSelect("carrier_rating", "Carrier", draft.carrier_rating, row=1))
            self.add_item(BackButton(row=2))
            self.add_item(CancelButton(row=2))
            self.add_item(RemarksButton(row=2))
            self.add_item(NextButton(row=2))

        elif page == 4:
            self.add_item(VibeRatingSelect(draft.vibe_rating, row=0))
            self.add_item(ResultSelect(draft.passed, row=1))
            self.add_item(BackButton(row=2))
            self.add_item(CancelButton(row=2))
            self.add_item(RemarksButton(row=2))
            self.add_item(NextButton(row=2))

        elif page == 5:
            self.add_item(BackButton(row=0))
            self.add_item(CancelButton(row=0))
            self.add_item(SubmitButton(row=0))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the instructor who opened this paperwork can use these controls.",
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
        await interaction.response.edit_message(
            embed=build_page_embed(
                self.draft,
                self.page,
                get_attempts_for_draft(self.owner_id, self.draft),
            ),
            view=PaperworkView(self.owner_id, self.draft, self.page),
        )


class RatingSelect(discord.ui.Select):
    def __init__(
        self,
        field_name: str,
        label: str,
        current_value: int | None,
        row: int,
    ):
        self.field_name = field_name

        options = []
        for option in RATING_OPTIONS:
            options.append(
                discord.SelectOption(
                    label=option.label,
                    value=option.value,
                    emoji=option.emoji,
                    default=current_value is not None and int(option.value) == current_value,
                )
            )

        super().__init__(
            placeholder=f"{label} rating",
            min_values=1,
            max_values=1,
            options=options,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, PaperworkView)

        set_rating(self.view.draft, self.field_name, int(self.values[0]))

        await self.view.refresh(interaction)


class VibeRatingSelect(discord.ui.Select):
    def __init__(
        self,
        current_value: int | None,
        row: int,
    ):
        options = []
        for option in VIBE_RATING_OPTIONS:
            options.append(
                discord.SelectOption(
                    label=option.label,
                    value=option.value,
                    emoji=option.emoji,
                    default=current_value is not None and int(option.value) == current_value,
                )
            )

        super().__init__(
            placeholder="Vibe rating",
            min_values=1,
            max_values=1,
            options=options,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, PaperworkView)

        self.view.draft.vibe_rating = int(self.values[0])

        await self.view.refresh(interaction)


class ResultSelect(discord.ui.Select):
    def __init__(
        self,
        current_value: bool | None,
        row: int,
    ):
        options = []
        for option in RESULT_OPTIONS:
            options.append(
                discord.SelectOption(
                    label=option.label,
                    value=option.value,
                    emoji=option.emoji,
                    default=(
                        (option.value == "pass" and current_value is True)
                        or (option.value == "fail" and current_value is False)
                    ),
                )
            )

        super().__init__(
            placeholder="Result",
            min_values=1,
            max_values=1,
            options=options,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, PaperworkView)

        self.view.draft.passed = self.values[0] == "pass"

        await self.view.refresh(interaction)


class RemarksButton(discord.ui.Button):
    def __init__(
        self,
        row: int,
    ):
        super().__init__(
            label="Remarks",
            emoji="📝",
            style=discord.ButtonStyle.secondary,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, PaperworkView)

        await interaction.response.send_modal(
            RemarksModal(
                owner_id=self.view.owner_id,
                draft=self.view.draft,
                page=self.view.page,
            )
        )


class RemarksModal(discord.ui.Modal):
    def __init__(
        self,
        owner_id: int,
        draft: PaperworkDraft,
        page: int,
    ):
        super().__init__(title=f"{page_title(page)} Remarks")

        self.owner_id = owner_id
        self.draft = draft
        self.page = page

        if page == 1:
            self.first_field = "ag_remarks"
            self.second_field = "aa_remarks"
            self.first_label = "Air to Ground Remarks"
            self.second_label = "Air to Air Remarks"
            first_default = draft.ag_remarks or ""
            second_default = draft.aa_remarks or ""

        elif page == 2:
            self.first_field = "formation_remarks"
            self.second_field = "tank_remarks"
            self.first_label = "Formation Remarks"
            self.second_label = "Tanker Remarks"
            first_default = draft.formation_remarks or ""
            second_default = draft.tank_remarks or ""

        elif page == 3:
            self.first_field = "case1_remarks"
            self.second_field = "carrier_remarks"
            self.first_label = "Case 1 Remarks"
            self.second_label = "Carrier Remarks"
            first_default = draft.case1_remarks or ""
            second_default = draft.carrier_remarks or ""

        else:
            self.first_field = "vibe_remarks"
            self.second_field = "verdict_remarks"
            self.first_label = "Vibe Remarks"
            self.second_label = "Verdict Remarks"
            first_default = draft.vibe_remarks or ""
            second_default = draft.verdict_remarks or ""

        self.first_input = discord.ui.TextInput(
            label=self.first_label,
            placeholder="Optional remarks",
            default=first_default,
            style=discord.TextStyle.paragraph,
            max_length=1000,
            required=False,
        )

        self.add_item(self.first_input)

        if self.second_field is not None:
            self.second_input = discord.ui.TextInput(
                label=self.second_label,
                placeholder="Optional remarks",
                default=second_default,
                style=discord.TextStyle.paragraph,
                max_length=1000,
                required=False,
            )
            self.add_item(self.second_input)
        else:
            self.second_input = None

    async def on_submit(self, interaction: discord.Interaction):
        setattr(
            self.draft,
            self.first_field,
            clean_optional_text(str(self.first_input.value)),
        )

        if self.second_field is not None and self.second_input is not None:
            setattr(
                self.draft,
                self.second_field,
                clean_optional_text(str(self.second_input.value)),
            )

        await interaction.response.edit_message(
            embed=build_page_embed(
                self.draft,
                self.page,
                get_attempts_for_draft(self.owner_id, self.draft),
            ),
            view=PaperworkView(self.owner_id, self.draft, self.page),
        )


class BackButton(discord.ui.Button):
    def __init__(
        self,
        row: int,
    ):
        super().__init__(
            label="Back",
            style=discord.ButtonStyle.primary,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, PaperworkView)

        previous_page = max(1, self.view.page - 1)

        await interaction.response.edit_message(
            embed=build_page_embed(
                self.view.draft,
                previous_page,
                get_attempts_for_draft(self.view.owner_id, self.view.draft),
            ),
            view=PaperworkView(self.view.owner_id, self.view.draft, previous_page),
        )


class CancelButton(discord.ui.Button):
    def __init__(
        self,
        row: int,
    ):
        super().__init__(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, PaperworkView)

        key = draft_key(
            self.view.owner_id,
            int(self.view.draft.applicant_discord_id),
        )

        active_paperwork_drafts.pop(key, None)
        active_previous_attempts.pop(key, None)

        await interaction.response.edit_message(
            content="Qualification paperwork cancelled.",
            embed=None,
            view=None,
        )


class NextButton(discord.ui.Button):
    def __init__(
        self,
        row: int,
    ):
        super().__init__(
            label="Next",
            style=discord.ButtonStyle.success,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, PaperworkView)

        next_page = min(5, self.view.page + 1)

        await interaction.response.edit_message(
            embed=build_page_embed(
                self.view.draft,
                next_page,
                get_attempts_for_draft(self.view.owner_id, self.view.draft),
            ),
            view=PaperworkView(self.view.owner_id, self.view.draft, next_page),
        )


class SubmitButton(discord.ui.Button):
    def __init__(
        self,
        row: int,
    ):
        super().__init__(
            label="Submit",
            style=discord.ButtonStyle.success,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, PaperworkView)

        errors = self.view.draft.validate()

        if errors:
            await interaction.response.send_message(
                "Cannot submit yet:\n"
                + "\n".join(f"- {error}" for error in errors),
                ephemeral=True,
            )
            return

        try:
            record_id = create_qual_log_record(self.view.draft)
        except Exception as error:
            await interaction.response.send_message(
                f"Failed to save paperwork: `{error}`",
                ephemeral=True,
            )
            return

        role_status = "Role not assigned: applicant did not pass."

        if self.view.draft.passed is True:
            role_status = await give_mission_qualified_role(
                interaction,
                self.view.draft.applicant_discord_id,
            )

            mark_request_completed_if_passed(
                applicant_discord_id=self.view.draft.applicant_discord_id,
                request_qual_id=self.view.draft.request_qual_id,
            )

        key = draft_key(
            self.view.owner_id,
            int(self.view.draft.applicant_discord_id),
        )

        active_paperwork_drafts.pop(key, None)
        active_previous_attempts.pop(key, None)

        embed = discord.Embed(
            title="Qualification Paperwork Saved",
            description=(
                f"Saved qualification attempt for <@{self.view.draft.applicant_discord_id}>.\n"
                f"Record ID: `{record_id}`\n"
                f"{role_status}"
            ),
        )

        await interaction.response.edit_message(
            embed=embed,
            view=None,
        )


def pending_applicant_choice_name(
    applicant: PendingQualApplicant,
    guild: discord.Guild | None,
) -> str:
    member_name = None

    if guild is not None:
        try:
            member = guild.get_member(int(applicant.discord_id))
            if member is not None:
                member_name = member.display_name
        except Exception:
            member_name = None

    base_name = member_name or applicant.discord_username or applicant.discord_id

    return f"{base_name} | request {applicant.request_id}"[:100]


async def pending_applicant_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    applicants = search_pending_qual_applicants(current, limit=25)

    return [
        app_commands.Choice(
            name=pending_applicant_choice_name(applicant, interaction.guild),
            value=applicant.discord_id,
        )
        for applicant in applicants
    ]


async def resolve_pending_applicant_member(
    interaction: discord.Interaction,
    discord_id: str,
) -> discord.Member | None:
    if interaction.guild is None:
        return None

    try:
        member = interaction.guild.get_member(int(discord_id))

        if member is None:
            member = await interaction.guild.fetch_member(int(discord_id))

        return member
    except Exception:
        return None




async def start_paperwork_message(
    interaction: discord.Interaction,
    *,
    instructor: discord.Member,
    applicant_member: discord.Member,
    pending_applicant: PendingQualApplicant,
    edit_existing: bool = False,
) -> None:
    key = draft_key(instructor.id, applicant_member.id)
    previous_attempts = get_previous_qual_attempts_for_user(str(applicant_member.id))

    draft = PaperworkDraft(
        request_qual_id=pending_applicant.request_id,
        applicant_discord_id=str(applicant_member.id),
        applicant_username=str(applicant_member.name),
        instructor_discord_id=str(instructor.id),
        instructor_username=str(instructor.name),
    )

    apply_previous_green_defaults(draft, previous_attempts)

    active_paperwork_drafts[key] = draft
    active_previous_attempts[key] = previous_attempts

    kwargs = {
        "embed": build_page_embed(
            draft,
            page=1,
            previous_attempts=previous_attempts,
        ),
        "view": PaperworkView(instructor.id, draft, page=1),
    }

    if edit_existing:
        await interaction.response.edit_message(**kwargs)
    else:
        await interaction.response.send_message(
            **kwargs,
            ephemeral=True,
        )


class ApplicationWarningView(discord.ui.View):
    def __init__(
        self,
        *,
        owner_id: int,
        applicant_member: discord.Member,
        pending_applicant: PendingQualApplicant,
    ):
        super().__init__(timeout=900)
        self.owner_id = int(owner_id)
        self.applicant_member = applicant_member
        self.pending_applicant = pending_applicant

        self.add_item(WarningExitButton(row=0))
        self.add_item(WarningContinueButton(row=0))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the instructor who opened this warning can use these controls.",
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
                "You need the instructor role to continue.",
                ephemeral=True,
            )
            return False

        return True


class WarningExitButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(
            label="Exit",
            style=discord.ButtonStyle.secondary,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content="Paperwork canceled.",
            embed=None,
            view=None,
        )


class WarningContinueButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(
            label="Continue",
            style=discord.ButtonStyle.success,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, ApplicationWarningView)

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This can only be used inside the server.",
                ephemeral=True,
            )
            return

        await start_paperwork_message(
            interaction,
            instructor=interaction.user,
            applicant_member=self.view.applicant_member,
            pending_applicant=self.view.pending_applicant,
            edit_existing=True,
        )


class PaperworkCommands(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="paperwork",
        description="Record qualification paperwork for a pending applicant.",
    )
    @app_commands.describe(user="Pending applicant")
    @app_commands.autocomplete(user=pending_applicant_autocomplete)
    @app_commands.guild_only()
    async def paperwork_command(
        self,
        interaction: discord.Interaction,
        user: str,
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

        pending_applicant = get_pending_qual_applicant(str(user))

        if pending_applicant is None:
            await interaction.response.send_message(
                "That user does not have a pending qualification application. "
                "Use the autocomplete list and pick a pending applicant.",
                ephemeral=True,
            )
            return

        applicant_member = await resolve_pending_applicant_member(
            interaction,
            pending_applicant.discord_id,
        )

        if applicant_member is None:
            await interaction.response.send_message(
                "That pending applicant could not be found in this server.",
                ephemeral=True,
            )
            return

        warnings = application_warning_lines(pending_applicant)

        if warnings:
            await interaction.response.send_message(
                embed=build_application_warning_embed(
                    member=applicant_member,
                    applicant=pending_applicant,
                ),
                view=ApplicationWarningView(
                    owner_id=interaction.user.id,
                    applicant_member=applicant_member,
                    pending_applicant=pending_applicant,
                ),
                ephemeral=True,
            )
            return

        await start_paperwork_message(
            interaction,
            instructor=interaction.user,
            applicant_member=applicant_member,
            pending_applicant=pending_applicant,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(PaperworkCommands(bot))
