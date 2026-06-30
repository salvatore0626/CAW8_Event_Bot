from __future__ import annotations

import traceback

import discord
from discord import app_commands
from discord.ext import commands, tasks

try:
    from config import ASVAB_TIME_LIMIT_MINUTES
except ImportError:
    ASVAB_TIME_LIMIT_MINUTES = 45

from services.asvab_service import (
    ASVABQuestionViewData,
    ASVABQuizError,
    ASVABQuizExpiredError,
    active_quiz,
    category_plan_counts,
    category_score_summary,
    current_question_data,
    ensure_schema,
    expire_started_attempts,
    get_started_attempt_for_user,
    mark_attempt_incomplete,
    move_question,
    move_to_next_unanswered_question,
    planned_question_count,
    record_answer,
    resume_or_start_attempt,
    submit_attempt,
)


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)

    if hours:
        return f"{hours}h {minutes}m"

    if minutes:
        return f"{minutes}m {sec}s"

    return f"{sec}s"


def score_text(value: float | None) -> str:
    if value is None:
        return "0%"

    try:
        score = float(value)
    except (TypeError, ValueError):
        score = 0

    if score == 100:
        return "100%"

    return f"{score:.1f}%"


def build_start_embed(member: discord.Member) -> discord.Embed:
    quiz = active_quiz()
    question_count = planned_question_count()
    category_counts = category_plan_counts()

    lines = [
        "You are about to take the **ASVAB**.",
        "",
        f"Questions: **{question_count}**",
        f"Time Limit: **{ASVAB_TIME_LIMIT_MINUTES} minutes**",
        "",
        "This is scored only. There is no pass/fail result.",
        "Your final report will show your score by category, but it will not show which questions you missed.",
    ]

    if category_counts:
        lines.append("")
        lines.append("**Category Mix**")
        for category, count in category_counts.items():
            lines.append(f"- {category}: {count}")

    embed = discord.Embed(
        title=quiz.title,
        description="\n".join(lines),
    )

    embed.set_footer(text=f"Applicant: {member.display_name}")
    return embed


def build_question_embed(data: ASVABQuestionViewData) -> discord.Embed:
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
            f"Skipped {skipped_count}"
        )
    )

    return embed


def build_expired_embed(message: str) -> discord.Embed:
    return discord.Embed(
        title="ASVAB Incomplete",
        description=message,
    )


def build_results_embed(attempt: dict) -> discord.Embed:
    correct = int(attempt.get("correct_count") or 0)
    total = int(attempt.get("total_questions") or 0)
    score = score_text(attempt.get("score_percent"))
    category_scores = category_score_summary(attempt)

    embed = discord.Embed(
        title="ASVAB Results",
        description=(
            f"Overall Score: **{score}**\n"
            f"Correct: **{correct}/{total}**\n\n"
            "Category scores are shown below. Missed questions are not shown."
        ),
    )

    for row in category_scores:
        embed.add_field(
            name=str(row["category"]),
            value=(
                f"Score: **{score_text(row['percent'])}**\n"
                f"Correct: **{int(row['correct'])}/{int(row['total'])}**"
            ),
            inline=True,
        )

    return embed


class ASVABStartView(discord.ui.View):
    def __init__(self, cog: "ASVABCog", member: discord.Member):
        super().__init__(timeout=300)
        self.cog = cog
        self.member_id = int(member.id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.member_id:
            await interaction.response.send_message(
                "Only the person who opened this ASVAB prompt can use these buttons.",
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
            content="ASVAB canceled.",
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
        except ASVABQuizError as error:
            await interaction.response.edit_message(
                content=f"Could not start the ASVAB: {error}",
                embed=None,
                view=None,
            )
            return

        title = "Resuming your ASVAB." if resumed else "ASVAB started."

        await interaction.response.edit_message(
            content=title,
            embed=build_question_embed(data),
            view=ASVABQuestionView(self.cog, int(attempt["attempt_id"]), self.member_id),
        )


class ASVABAnswerSelect(discord.ui.Select):
    def __init__(self, data: ASVABQuestionViewData):
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
        assert isinstance(self.view, ASVABQuestionView)

        try:
            selected = [int(value) for value in self.values]
            data = record_answer(
                attempt_id=self.view.attempt_id,
                selected_display_indexes=selected,
            )
        except ASVABQuizExpiredError as error:
            await interaction.response.edit_message(
                content=None,
                embed=build_expired_embed(str(error)),
                view=None,
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
            view=ASVABQuestionView(
                self.view.cog,
                self.view.attempt_id,
                self.view.owner_id,
            ),
        )


class ASVABQuitButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Quit",
            style=discord.ButtonStyle.danger,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, ASVABQuestionView)
        await self.view.quit(interaction)


class ASVABSubmitButton(discord.ui.Button):
    def __init__(self, disabled: bool):
        super().__init__(
            label="Submit",
            style=discord.ButtonStyle.success,
            disabled=disabled,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, ASVABQuestionView)
        await self.view.submit(interaction)


class ASVABPreviousQuestionButton(discord.ui.Button):
    def __init__(self, disabled: bool):
        super().__init__(
            label="Prev",
            style=discord.ButtonStyle.primary,
            disabled=disabled,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, ASVABQuestionView)
        await self.view.move_and_refresh(interaction, -1)


class ASVABSkippedQuestionButton(discord.ui.Button):
    def __init__(self, disabled: bool):
        super().__init__(
            label="Skipped",
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, ASVABQuestionView)
        await self.view.jump_to_skipped(interaction)


class ASVABNextQuestionButton(discord.ui.Button):
    def __init__(self, disabled: bool):
        super().__init__(
            label="Next",
            style=discord.ButtonStyle.primary,
            disabled=disabled,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, ASVABQuestionView)
        await self.view.move_and_refresh(interaction, 1)


class ASVABQuestionView(discord.ui.View):
    def __init__(self, cog: "ASVABCog", attempt_id: int, owner_id: int):
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

        self.add_item(ASVABAnswerSelect(data))
        self.add_item(ASVABQuitButton())
        self.add_item(ASVABSubmitButton(disabled=not all_answered))
        self.add_item(ASVABPreviousQuestionButton(disabled=data.current_index <= 0))
        self.add_item(ASVABSkippedQuestionButton(disabled=not has_skipped))
        self.add_item(ASVABNextQuestionButton(disabled=data.current_index >= data.total_questions - 1))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the person taking this ASVAB can use these controls.",
                ephemeral=True,
            )
            return False

        return True

    async def move_and_refresh(self, interaction: discord.Interaction, delta: int) -> None:
        try:
            move_question(self.attempt_id, delta)
            data = current_question_data(self.attempt_id)
        except ASVABQuizExpiredError as error:
            await interaction.response.edit_message(
                content=None,
                embed=build_expired_embed(str(error)),
                view=None,
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
            view=ASVABQuestionView(self.cog, self.attempt_id, self.owner_id),
        )

    async def jump_to_skipped(self, interaction: discord.Interaction) -> None:
        try:
            data = move_to_next_unanswered_question(self.attempt_id)
        except ASVABQuizExpiredError as error:
            await interaction.response.edit_message(
                content=None,
                embed=build_expired_embed(str(error)),
                view=None,
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
            view=ASVABQuestionView(self.cog, self.attempt_id, self.owner_id),
        )

    async def quit(self, interaction: discord.Interaction) -> None:
        mark_attempt_incomplete(self.attempt_id)

        await interaction.response.edit_message(
            content=None,
            embed=build_expired_embed(
                "You quit the ASVAB. This attempt was marked Incomplete.",
            ),
            view=None,
        )

    async def submit(self, interaction: discord.Interaction) -> None:
        try:
            attempt = submit_attempt(self.attempt_id)
        except ASVABQuizExpiredError as error:
            await interaction.response.edit_message(
                content=None,
                embed=build_expired_embed(str(error)),
                view=None,
            )
            return
        except ASVABQuizError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        except Exception as error:
            traceback.print_exc()
            await interaction.response.send_message(
                f"Could not submit ASVAB: `{type(error).__name__}: {error}`",
                ephemeral=True,
            )
            return

        await interaction.response.edit_message(
            content=None,
            embed=build_results_embed(attempt),
            view=None,
        )


class ASVABCog(commands.Cog):
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

    @app_commands.command(name="asvab", description="Take the ASVAB quiz.")
    @app_commands.guild_only()
    async def asvab(self, interaction: discord.Interaction):
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
        except ASVABQuizError as error:
            await interaction.response.send_message(
                f"ASVAB is not configured correctly: {error}",
                ephemeral=True,
            )
            return

        started_attempt = get_started_attempt_for_user(str(interaction.user.id))

        if started_attempt is not None:
            try:
                data = current_question_data(int(started_attempt["attempt_id"]))
            except ASVABQuizExpiredError as error:
                await interaction.response.send_message(
                    embed=build_expired_embed(str(error)),
                    ephemeral=True,
                )
                return

            await interaction.response.send_message(
                content="Resuming your started ASVAB.",
                embed=build_question_embed(data),
                view=ASVABQuestionView(
                    self,
                    int(started_attempt["attempt_id"]),
                    interaction.user.id,
                ),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=build_start_embed(interaction.user),
            view=ASVABStartView(self, interaction.user),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(ASVABCog(bot))
