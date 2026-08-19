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
    award_count_items,
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


def metric_lines(rows: list[tuple[str, str]]) -> str:
    return "\n".join(f"**{label}:** `{value}`" for label, value in rows)


def recent_awards_text(summary: LookupSummary) -> str:
    if not summary.recent_awards:
        return "No active awards recorded."

    lines: list[str] = []
    for award in summary.recent_awards:
        award_type = str(award.get("award_type") or "Award")
        display = {
            "ACE": "ACE",
            "GOLDEN_WRENCH": "Golden Wrench",
            "SAFETY_S": "Safety S",
        }.get(award_type, award_type.replace("_", " ").title())
        operation = str(award.get("operation_name") or "Unknown Operation")
        event_id = award.get("source_event_id")
        event_text = f" · OP #{event_id}" if event_id is not None else ""
        lines.append(f"**{display}** — {operation}{event_text}")
    return "\n".join(lines)


def summary_embed(summary: LookupSummary) -> discord.Embed:
    gpa = (
        f"{summary.career_gpa:.3f} ({summary.career_gpa_attempts} attempts)"
        if summary.career_gpa is not None
        else f"— ({summary.career_gpa_attempts} attempts)"
    )

    embed = discord.Embed(
        title=f"Op Record for {summary.display_name}",
        description="Full attendance and active award history is attached.",
    )
    embed.add_field(
        name="Attendance",
        value=metric_lines([
            ("Ops Attended", str(summary.ops_attended)),
            ("Unique Ops", str(summary.unique_ops_attended)),
        ]),
        inline=False,
    )
    embed.add_field(
        name="Stats",
        value=metric_lines([
            ("Deathless Ops", str(summary.deathless_total)),
            ("Deathless Streak", str(summary.deathless_current_streak)),
            ("Bolterless Ops", str(summary.bolterless_total)),
            ("Bolterless Streak", str(summary.bolterless_current_streak)),
            ("Career GPA", gpa),
            ("Flight lead rating", summary.flight_lead_rating_text),
        ]),
        inline=False,
    )
    embed.add_field(
        name="Awards",
        value=metric_lines([
            (name, str(count))
            for name, count in award_count_items(summary.manual_award_counts)
        ]),
        inline=False,
    )
    embed.add_field(
        name="Leaderboard Positions",
        value=metric_lines([
            ("Attendance", position_text(summary.attendance_position)),
            ("GPA", position_text(summary.wire_gpa_position)),
            ("Survival", position_text(summary.survival_position)),
        ]),
        inline=False,
    )
    embed.add_field(
        name="Most Recent Awards",
        value=recent_awards_text(summary),
        inline=False,
    )
    embed.set_footer(
        text=(
            f"Highest qualified rank: {summary.highest_qualified_rank} | "
            "GPA: bolters count as 0-point attempts"
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
        user: discord.Member | None = None,
        visibility: str = "hidden",
    ):
        # Acknowledge immediately. The export can run several full-history
        # queries and build a sizeable text attachment.
        if not await require_mission_qualified_command(interaction):
            return

        hidden = not lookup_is_public(visibility)
        user = user or interaction.user

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
