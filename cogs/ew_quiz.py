from __future__ import annotations

import asyncio
import traceback

import discord
from discord import app_commands
from discord.ext import commands, tasks
from services.permission_service import (
    require_mission_qualified_command,
    member_is_admin,
)

try:
    from config import MISSION_QUALIFIED_ROLE
except ImportError:
    MISSION_QUALIFIED_ROLE = 0

try:
    from config import EW_QUALIFIED_ROLE
except ImportError:
    EW_QUALIFIED_ROLE = 0

try:
    from config import EW_QUIZ_TIME_LIMIT_MINUTES
except ImportError:
    EW_QUIZ_TIME_LIMIT_MINUTES = 30

try:
    from config import TEST_COOLDOWN_HOURS
except ImportError:
    TEST_COOLDOWN_HOURS = 0

try:
    from config import EW_RESULTS_CHANNEL
except ImportError:
    EW_RESULTS_CHANNEL = 0

try:
    from config import NATOPS_CHANNEL_ID
except ImportError:
    NATOPS_CHANNEL_ID = 0

try:
    from config import NATOPS_MESSAGE_ID
except ImportError:
    NATOPS_MESSAGE_ID = 0

from services.ew_quiz_service import (
    EWQuizError,
    QuizExpiredError,
    STATUS_PASSED,
    QuestionViewData,
    active_quiz,
    attempt_result_summary,
    cooldown_remaining_for_user,
    current_question_data,
    ensure_schema,
    expire_started_attempts,
    get_started_attempt_for_user,
    mark_attempt_incomplete,
    move_question,
    move_to_next_unanswered_question,
    record_answer,
    resume_or_start_attempt,
    set_role_awarded,
    start_new_attempt,
    submit_attempt,
)


def member_has_role(member: discord.Member, role_id: int | str | None) -> bool:
    try:
        rid = int(role_id or 0)
    except (TypeError, ValueError):
        rid = 0

    if not rid:
        return False

    return any(int(role.id) == rid for role in member.roles)


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    minutes, seconds = divmod(seconds, 60)

    if minutes:
        return f"{minutes}m {seconds:02d}s"

    return f"{seconds}s"


def format_cooldown(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, _seconds = divmod(remainder, 60)

    if hours and minutes:
        return f"{hours} hour(s) {minutes} minute(s)"

    if hours:
        return f"{hours} hour(s)"

    if minutes:
        return f"{minutes} minute(s)"

    return "less than 1 minute"


def passing_score_text(attempt: dict) -> str:
    try:
        value = float(attempt.get("passing_score") or 0)
    except (TypeError, ValueError):
        value = 0

    if value.is_integer():
        return str(int(value))

    return f"{value:g}"


def score_text(attempt: dict) -> str:
    try:
        score = float(attempt.get("score_percent") or 0)
    except (TypeError, ValueError):
        score = 0

    if score == 100:
        return "100%"

    return f"{score:.1f}%"


def natops_jump_url(guild_id: int | None) -> str | None:
    try:
        guild = int(guild_id or 0)
        channel = int(NATOPS_CHANNEL_ID or 0)
        message = int(NATOPS_MESSAGE_ID or 0)
    except (TypeError, ValueError):
        return None

    if not guild or not channel or not message:
        return None

    return f"https://discord.com/channels/{guild}/{channel}/{message}"


class NATOPSLinkView(discord.ui.View):
    def __init__(self, guild_id: int | None):
        super().__init__(timeout=None)
        url = natops_jump_url(guild_id)

        if url:
            self.add_item(
                discord.ui.Button(
                    label="Study NATOPS",
                    style=discord.ButtonStyle.link,
                    url=url,
                )
            )


def build_question_embed(data: QuestionViewData) -> discord.Embed:
    choices = [
        f"**{choice['letter']}.** {choice['text']}"
        for choice in data.displayed_choices
    ]

    if data.selected_letters and data.selected_answers:
        selected_lines = [
            f"**{letter}.** {answer}"
            for letter, answer in zip(data.selected_letters, data.selected_answers)
        ]
        selected = "\n".join(selected_lines)
    else:
        selected = "No answer selected yet."

    category_line = f"\nCategory: **{data.category}**" if data.category else ""
    answer_mode = "Multi-select" if data.multi_select else "Single answer"

    embed = discord.Embed(
        title=f"{data.title} — Question {data.current_index + 1}/{data.total_questions}",
        description=(
            f"Version: **{data.quiz_version}**{category_line}\n"
            f"Mode: **{answer_mode}**\n"
            f"Time remaining: **{format_duration(data.remaining_seconds)}**\n\n"
            f"**{data.question_text}**\n\n"
            + "\n".join(choices)
        ),
    )

    embed.add_field(
        name="Selected Answer" if not data.multi_select else "Selected Answers",
        value=selected,
        inline=False,
    )

    skipped_count = max(0, data.total_questions - data.answered_count)

    embed.set_footer(
        text=(
            f"Answered {data.answered_count}/{data.total_questions} | "
            f"Skipped {skipped_count} | "
            f"Passing score: {data.passing_score:g}%"
        )
    )

    return embed

def build_start_embed(member: discord.Member) -> discord.Embed:
    quiz = active_quiz()

    embed = discord.Embed(
        title=quiz.title,
        description=(
            f"Would you like to take the EW test?\n\n"
            f"Version: **{quiz.version}**\n"
            f"Questions: **{len(quiz.questions)}**\n"
            f"Passing Score: **{quiz.passing_score:g}%**\n"
            f"Time Limit: **{EW_QUIZ_TIME_LIMIT_MINUTES} minutes**"
        ),
    )

    embed.set_footer(text=f"Applicant: {member.display_name}")
    return embed


def build_expired_embed(message: str, cooldown_remaining: int = 0) -> discord.Embed:
    cooldown_line = ""

    if cooldown_remaining > 0:
        cooldown_line = f"\n\nYou can start over in **{format_cooldown(cooldown_remaining)}**."

    return discord.Embed(
        title="EW Quiz Incomplete",
        description=f"{message}{cooldown_line}",
    )


def build_finished_embed(
    attempt: dict,
    *,
    role_message: str | None = None,
    cooldown_remaining: int = 0,
) -> discord.Embed:
    status = str(attempt.get("status") or "Unknown")
    passed = status == STATUS_PASSED
    score = score_text(attempt)

    if passed and score == "100%":
        description = (
            "✅ **Pass** 🎉\n"
            f"Score: **{score}**\n\n"
            "Congratulations! You have passed with a perfect score. "
            "You are now EW Qualified!"
        )
    elif passed:
        description = (
            "✅ **Pass** 🎉\n"
            f"Score: **{score}**\n\n"
            "Congratulations! You have passed. You are now EW Qualified!"
        )
    else:
        wait_text = format_cooldown(cooldown_remaining)
        description = (
            "❌ **Failed** ❌\n"
            f"Score: **{score}**\n\n"
            f"Sorry but you need {passing_score_text(attempt)} percent to pass. "
            f"Please study the NATOPS and try again in {wait_text}."
        )

    embed = discord.Embed(
        title="EW Quiz Results",
        description=description,
    )

    if role_message:
        embed.add_field(
            name="Role",
            value=role_message,
            inline=False,
        )

    return embed


class EWQuizStartView(discord.ui.View):
    def __init__(self, cog: "EWQuizCog", member: discord.Member):
        super().__init__(timeout=300)
        self.cog = cog
        self.member_id = int(member.id)

        url = natops_jump_url(member.guild.id if member.guild else None)

        if url:
            self.add_item(
                discord.ui.Button(
                    label="Study NATOPS",
                    style=discord.ButtonStyle.link,
                    url=url,
                    row=1,
                )
            )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.member_id:
            await interaction.response.send_message(
                "Only the person who opened this quiz prompt can use these buttons.",
                ephemeral=True,
            )
            return False

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This has to be used inside the server.",
                ephemeral=True,
            )
            return False

        return True

    @discord.ui.button(label="No", style=discord.ButtonStyle.secondary)
    async def no_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.edit_message(
            content="EW quiz canceled.",
            embed=None,
            view=None,
        )

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success)
    async def yes_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        assert isinstance(interaction.user, discord.Member)

        try:
            attempt, resumed = resume_or_start_attempt(
                discord_id=str(interaction.user.id),
                discord_username=interaction.user.name,
                display_name=interaction.user.display_name,
            )
            data = current_question_data(int(attempt["attempt_id"]))
        except EWQuizError as error:
            await interaction.response.edit_message(
                content=f"Could not start the EW quiz: {error}",
                embed=None,
                view=None,
            )
            return

        title = "Resuming your EW quiz." if resumed else "EW quiz started."

        await interaction.response.edit_message(
            content=title,
            embed=build_question_embed(data),
            view=EWQuizQuestionView(self.cog, int(attempt["attempt_id"]), self.member_id),
        )


class AnswerSelect(discord.ui.Select):
    def __init__(self, data: QuestionViewData):
        options = []
        selected_indexes = set(data.selected_display_indexes)

        for choice in data.displayed_choices:
            display_index = int(choice["display_index"])
            options.append(
                discord.SelectOption(
                    label=choice["letter"],
                    value=str(display_index),
                    description=str(choice["text"])[:100],
                    default=display_index in selected_indexes,
                )
            )

        super().__init__(
            placeholder="Select all correct answers" if data.multi_select else "Select your answer",
            min_values=1,
            max_values=len(options) if data.multi_select else 1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, EWQuizQuestionView)

        try:
            selected = [int(value) for value in self.values]
            data = record_answer(
                attempt_id=self.view.attempt_id,
                selected_display_indexes=selected,
            )
        except QuizExpiredError as error:
            cooldown_remaining = cooldown_remaining_for_user(str(interaction.user.id))
            result_view = NATOPSLinkView(interaction.guild.id if interaction.guild else None)

            if not result_view.children:
                result_view = None

            await interaction.response.edit_message(
                content=None,
                embed=build_expired_embed(str(error), cooldown_remaining),
                view=result_view,
            )
            return
        except Exception as error:
            traceback.print_exc()
            await interaction.response.send_message(
                f"Could not save answer: `{type(error).__name__}: {error}`",
                ephemeral=True,
            )
            return

        await interaction.response.edit_message(
            embed=build_question_embed(data),
            view=EWQuizQuestionView(
                self.view.cog,
                self.view.attempt_id,
                self.view.owner_id,
            ),
        )

class QuitQuizButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Quit",
            style=discord.ButtonStyle.danger,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, EWQuizQuestionView)
        await self.view.quit(interaction)


class SubmitQuizButton(discord.ui.Button):
    def __init__(self, disabled: bool):
        super().__init__(
            label="Submit",
            style=discord.ButtonStyle.success,
            disabled=disabled,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, EWQuizQuestionView)
        await self.view.submit(interaction)


class PreviousQuestionButton(discord.ui.Button):
    def __init__(self, disabled: bool):
        super().__init__(
            label="Prev",
            style=discord.ButtonStyle.primary,
            disabled=disabled,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, EWQuizQuestionView)
        await self.view.move_and_refresh(interaction, -1)


class SkippedQuestionButton(discord.ui.Button):
    def __init__(self, disabled: bool):
        super().__init__(
            label="Skipped",
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, EWQuizQuestionView)
        await self.view.jump_to_skipped(interaction)


class NextQuestionButton(discord.ui.Button):
    def __init__(self, disabled: bool):
        super().__init__(
            label="Next",
            style=discord.ButtonStyle.primary,
            disabled=disabled,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, EWQuizQuestionView)
        await self.view.move_and_refresh(interaction, 1)


class EWQuizQuestionView(discord.ui.View):
    def __init__(self, cog: "EWQuizCog", attempt_id: int, owner_id: int):
        super().__init__(timeout=1800)
        self.cog = cog
        self.attempt_id = int(attempt_id)
        self.owner_id = int(owner_id)

        try:
            data = current_question_data(self.attempt_id)
        except Exception:
            return

        all_answered = data.answered_count >= data.total_questions
        has_skipped = not all_answered

        self.add_item(AnswerSelect(data))
        self.add_item(QuitQuizButton())
        self.add_item(SubmitQuizButton(disabled=not all_answered))
        self.add_item(PreviousQuestionButton(disabled=data.current_index <= 0))
        self.add_item(SkippedQuestionButton(disabled=not has_skipped))
        self.add_item(NextQuestionButton(disabled=data.current_index >= data.total_questions - 1))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the person taking this EW quiz can use these controls.",
                ephemeral=True,
            )
            return False

        return True

    async def move_and_refresh(self, interaction: discord.Interaction, delta: int) -> None:
        try:
            move_question(self.attempt_id, delta)
            data = current_question_data(self.attempt_id)
        except QuizExpiredError as error:
            cooldown_remaining = cooldown_remaining_for_user(str(interaction.user.id))
            result_view = NATOPSLinkView(interaction.guild.id if interaction.guild else None)

            if not result_view.children:
                result_view = None

            await interaction.response.edit_message(
                content=None,
                embed=build_expired_embed(str(error), cooldown_remaining),
                view=result_view,
            )
            return
        except Exception as error:
            traceback.print_exc()
            await interaction.response.send_message(
                f"Could not change question: `{type(error).__name__}: {error}`",
                ephemeral=True,
            )
            return

        await interaction.response.edit_message(
            embed=build_question_embed(data),
            view=EWQuizQuestionView(self.cog, self.attempt_id, self.owner_id),
        )

    async def jump_to_skipped(self, interaction: discord.Interaction) -> None:
        try:
            data = move_to_next_unanswered_question(self.attempt_id)
        except QuizExpiredError as error:
            cooldown_remaining = cooldown_remaining_for_user(str(interaction.user.id))
            result_view = NATOPSLinkView(interaction.guild.id if interaction.guild else None)

            if not result_view.children:
                result_view = None

            await interaction.response.edit_message(
                content=None,
                embed=build_expired_embed(str(error), cooldown_remaining),
                view=result_view,
            )
            return
        except Exception as error:
            traceback.print_exc()
            await interaction.response.send_message(
                f"Could not jump to skipped question: `{type(error).__name__}: {error}`",
                ephemeral=True,
            )
            return

        await interaction.response.edit_message(
            embed=build_question_embed(data),
            view=EWQuizQuestionView(self.cog, self.attempt_id, self.owner_id),
        )

    async def quit(self, interaction: discord.Interaction) -> None:
        mark_attempt_incomplete(self.attempt_id)
        cooldown_remaining = cooldown_remaining_for_user(str(interaction.user.id))
        result_view = NATOPSLinkView(interaction.guild.id if interaction.guild else None)

        if not result_view.children:
            result_view = None

        await interaction.response.edit_message(
            content=None,
            embed=build_expired_embed(
                "You quit the EW quiz. This attempt was marked Incomplete.",
                cooldown_remaining,
            ),
            view=result_view,
        )

    async def submit(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This has to be used inside the server.",
                ephemeral=True,
            )
            return

        try:
            attempt = submit_attempt(self.attempt_id)
        except QuizExpiredError as error:
            cooldown_remaining = cooldown_remaining_for_user(str(interaction.user.id))
            result_view = NATOPSLinkView(interaction.guild.id if interaction.guild else None)

            if not result_view.children:
                result_view = None

            await interaction.response.edit_message(
                content=None,
                embed=build_expired_embed(str(error), cooldown_remaining),
                view=result_view,
            )
            return
        except EWQuizError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        except Exception as error:
            traceback.print_exc()
            await interaction.response.send_message(
                f"Could not submit quiz: `{type(error).__name__}: {error}`",
                ephemeral=True,
            )
            return

        role_message = None
        result_view = None

        if str(attempt.get("status")) == STATUS_PASSED:
            role_message = await self.cog.award_ew_role(interaction.user, self.attempt_id)
            await self.cog.post_pass_announcement(interaction.user, attempt)
        else:
            result_view = NATOPSLinkView(interaction.guild.id if interaction.guild else None)

            if not result_view.children:
                result_view = None

        cooldown_remaining = cooldown_remaining_for_user(str(interaction.user.id))

        await interaction.response.edit_message(
            content=None,
            embed=build_finished_embed(
                attempt,
                role_message=role_message,
                cooldown_remaining=cooldown_remaining,
            ),
            view=result_view,
        )


class EWQuizCog(commands.GroupCog, name="ew"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        ensure_schema()
        self.expire_loop.start()

    async def cog_unload(self):
        self.expire_loop.cancel()

    @tasks.loop(seconds=60)
    async def expire_loop(self):
        try:
            expire_started_attempts()
        except Exception:
            traceback.print_exc()

    @expire_loop.before_loop
    async def before_expire_loop(self):
        await self.bot.wait_until_ready()

    async def award_ew_role(self, member: discord.Member, attempt_id: int) -> str:
        if not EW_QUALIFIED_ROLE:
            set_role_awarded(attempt_id, False)
            return "Passed, but EW_QUALIFIED_ROLE is not set in config.py."

        role = member.guild.get_role(int(EW_QUALIFIED_ROLE))

        if role is None:
            set_role_awarded(attempt_id, False)
            return "Passed, but EW_QUALIFIED_ROLE was not found in this server."

        try:
            await member.add_roles(role, reason="Passed EW quiz")
            set_role_awarded(attempt_id, True)
            return f"EW Qualified role awarded: {role.mention}"
        except discord.Forbidden:
            set_role_awarded(attempt_id, False)
            return "Passed, but I do not have permission to assign the EW role."
        except discord.HTTPException:
            set_role_awarded(attempt_id, False)
            return "Passed, but Discord rejected the role assignment."

    async def post_pass_announcement(self, member: discord.Member, attempt: dict) -> None:
        try:
            channel_id = int(EW_RESULTS_CHANNEL or 0)
        except (TypeError, ValueError):
            channel_id = 0

        if not channel_id:
            return

        channel = member.guild.get_channel(channel_id)

        if channel is None:
            try:
                channel = await member.guild.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return

        if not isinstance(channel, discord.abc.Messageable):
            return

        score = score_text(attempt)

        if score == "100%":
            message = (
                f"🎉 Congratulations {member.mention}! "
                "They passed the EW Qualification Test with a **perfect score** "
                "and are now **EW Qualified**!"
            )
        else:
            message = (
                f"🎉 Congratulations {member.mention}! "
                f"They passed the EW Qualification Test with a score of **{score}** "
                "and are now **EW Qualified**!"
            )

        try:
            await channel.send(message)
        except discord.HTTPException:
            return

    def check_requirements(self, member: discord.Member) -> str | None:
        if not MISSION_QUALIFIED_ROLE:
            return "MISSION_QUALIFIED_ROLE is not set in config.py."

        if not EW_QUALIFIED_ROLE:
            return "EW_QUALIFIED_ROLE is not set in config.py."

        if not member_is_admin(member) and not member_has_role(member, MISSION_QUALIFIED_ROLE):
            return "You need to be Mission Qualified before taking the EW quiz."

        if member_has_role(member, EW_QUALIFIED_ROLE):
            return "You are already EW Qualified."

        return None

    @app_commands.command(name="quiz", description="Take the EW qualification quiz.")
    async def quiz(self, interaction: discord.Interaction):
        if not await require_mission_qualified_command(interaction):
            return
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This command can only be used inside the server.",
                ephemeral=True,
            )
            return

        ensure_schema()
        expire_started_attempts()

        try:
            active_quiz()
        except EWQuizError as error:
            await interaction.response.send_message(
                f"EW quiz is not configured correctly: {error}",
                ephemeral=True,
            )
            return

        started_attempt = get_started_attempt_for_user(str(interaction.user.id))

        if started_attempt is not None:
            try:
                data = current_question_data(int(started_attempt["attempt_id"]))
            except QuizExpiredError as error:
                await interaction.response.send_message(
                    embed=build_expired_embed(str(error)),
                    ephemeral=True,
                )
                return

            await interaction.response.send_message(
                content="Resuming your started EW quiz.",
                embed=build_question_embed(data),
                view=EWQuizQuestionView(
                    self,
                    int(started_attempt["attempt_id"]),
                    interaction.user.id,
                ),
                ephemeral=True,
            )
            return

        requirement_error = self.check_requirements(interaction.user)

        if requirement_error:
            await interaction.response.send_message(requirement_error, ephemeral=True)
            return

        cooldown_remaining = cooldown_remaining_for_user(str(interaction.user.id))

        if cooldown_remaining > 0:
            view = NATOPSLinkView(interaction.guild.id if interaction.guild else None)

            if not view.children:
                view = None

            await interaction.response.send_message(
                (
                    "You recently took the EW quiz. "
                    f"Please study the NATOPS and try again in **{format_cooldown(cooldown_remaining)}**."
                ),
                view=view,
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=build_start_embed(interaction.user),
            view=EWQuizStartView(self, interaction.user),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(EWQuizCog(bot))
