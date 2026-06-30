from __future__ import annotations

from io import BytesIO
import re
import traceback

import discord
from discord import app_commands
from discord.ext import commands

from services.lookup_service import (
    LookupSummary,
    build_lookup_export,
    build_lookup_summary,
    manual_award_summary_lines,
)
from services.permission_service import (
    require_mission_qualified_command,
)


LOOKUP_VISIBILITY_CHOICES = [
    app_commands.Choice(name="Hidden", value="hidden"),
    app_commands.Choice(name="Public", value="public"),
]


def lookup_is_public(visibility: str | None) -> bool:
    return str(visibility or "hidden").casefold() == "public"


def safe_export_filename(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return clean[:64] or "user"


def position_text(value: int | None) -> str:
    return f"#{value}" if value is not None else "—"



def awards_summary_text(summary: LookupSummary) -> str:
    lines = [
        f"**ACE:** {summary.ace_awards}  |  **Golden Wrench:** {summary.golden_wrench_awards}",
        f"**Safety S:** {summary.safety_s_awards}",
    ]

    manual_lines = manual_award_summary_lines(summary.manual_award_counts)

    if manual_lines:
        lines.append("**Manual Awards:**")
        lines.extend(manual_lines)

    return "\n".join(lines)

def summary_embed(summary: LookupSummary) -> discord.Embed:
    gpa = (
        f"{summary.career_gpa:.3f} "
        f"({summary.career_gpa_attempts} attempts)"
        if summary.career_gpa is not None
        else "—"
    )

    embed = discord.Embed(
        title=f"Op Record for {summary.display_name}",
        description=(
            "Full attendance and award history is attached as a text export."
        ),
    )
    embed.add_field(
        name="Attendance",
        value=(
            f"**Ops attended:** {summary.ops_attended}\n"
            f"**Unique op templates:** {summary.unique_ops_attended}"
        ),
        inline=True,
    )
    embed.add_field(
        name="Survival",
        value=(
            f"**Clean-op streak:** {summary.deathless_current_streak}\n"
            f"**Deathless ops:** {summary.deathless_total}"
        ),
        inline=True,
    )
    embed.add_field(
        name="Carrier Recovery",
        value=(
            f"**Clean-carrier streak:** {summary.bolterless_current_streak}\n"
            f"**Bolterless carrier ops:** {summary.bolterless_total}\n"
            f"**Career GPA:** {gpa}"
        ),
        inline=True,
    )
    embed.add_field(
        name="Awards",
        value=awards_summary_text(summary),
        inline=False,
    )
    embed.add_field(
        name="Qualification",
        value=f"**Highest qualified rank:** {summary.highest_qualified_rank}",
        inline=True,
    )
    embed.add_field(
        name="All-Time Leaderboard Positions",
        value=(
            f"**Attendance:** {position_text(summary.attendance_position)}\n"
            f"**Wire GPA:** {position_text(summary.wire_gpa_position)}\n"
            f"**Survival:** {position_text(summary.survival_position)}"
        ),
        inline=True,
    )
    embed.set_footer(
        text=(
            "GPA scale: 1-wire = 1.0 | 2-wire = 2.0 | "
            "3-wire = 4.0 | 4-wire = 3.0 | Bolter = 0.0"
        )
    )
    return embed


class LookupCog(commands.Cog):
    """Public summary plus full text export for one member's records."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="lookup",
        description="Show a user's operation summary and export their records.",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        user="Member whose attendance and awards to export.",
        visibility="Whether this lookup should be hidden or public.",
    )
    @app_commands.choices(visibility=LOOKUP_VISIBILITY_CHOICES)
    async def lookup(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        visibility: str = "hidden",
    ):
        # Acknowledge immediately. The export can run several full-history
        # queries and build a sizeable text attachment.
        if not await require_mission_qualified_command(interaction):
            return

        hidden = not lookup_is_public(visibility)

        try:
            await interaction.response.defer(thinking=True, ephemeral=hidden)
        except Exception:
            # Keep the traceback in the bot console; Discord may already have
            # acknowledged the interaction in a rare double-response case.
            traceback.print_exc()
            return

        try:
            role_ids = {
                int(role.id)
                for role in getattr(user, "roles", [])
                if getattr(role, "id", None) is not None
            }
            summary = build_lookup_summary(
                discord_id=str(user.id),
                fallback_name=getattr(user, "display_name", None),
                member_role_ids=role_ids,
            )
            export_text = build_lookup_export(summary=summary)

            filename = (
                f"lookup_{safe_export_filename(summary.display_name)}_"
                f"{summary.discord_id}.txt"
            )
            text_file = discord.File(
                BytesIO(export_text.encode("utf-8")),
                filename=filename,
            )

            await interaction.followup.send(
                embed=summary_embed(summary),
                file=text_file,
                ephemeral=hidden,
            )
        except Exception as error:
            traceback.print_exc()

            try:
                await interaction.followup.send(
                    (
                        "Lookup failed after the command was accepted. "
                        f"Check the bot console for the traceback. "
                        f"Error: `{type(error).__name__}: {error}`"
                    )[:1900],
                    ephemeral=True,
                )
            except Exception:
                traceback.print_exc()

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ):
        """Return a useful Discord error for failures before callback handling."""
        original = getattr(error, "original", error)
        traceback.print_exception(
            type(original),
            original,
            original.__traceback__,
        )

        message = (
            "Lookup could not start. Check the bot console for the traceback. "
            f"Error: `{type(original).__name__}: {original}`"
        )[:1900]

        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except Exception:
            traceback.print_exc()


async def setup(bot: commands.Bot):
    await bot.add_cog(LookupCog(bot))
