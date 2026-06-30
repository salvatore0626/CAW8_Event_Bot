from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from services.greenie_service import (
    BOLTER_EMOJI,
    CAG_DCAG_BOLTER_EMOJI,
    SPECIAL_SPEAKING_BOLTER_EMOJI,
    build_greenie_embed,
)
from services.permission_service import (
    require_mission_qualified_command,
)


try:
    from config import RANK_ROLES
except ImportError:
    RANK_ROLES = []


SPECIAL_SPEAKING_BOLTER_DISCORD_ID = "165270837648293890"


def configured_cag_dcag_role_ids() -> set[int]:
    """Read CAG/DCAG role IDs from config.RANK_ROLES."""
    role_ids: set[int] = set()

    if not isinstance(RANK_ROLES, (list, tuple)):
        return role_ids

    for row in RANK_ROLES:
        if not isinstance(row, dict):
            continue

        rank = str(row.get("rank") or "").strip().upper()
        if rank not in {"CAG", "DCAG"}:
            continue

        try:
            role_id = int(row.get("role_id") or 0)
        except (TypeError, ValueError):
            continue

        if role_id:
            role_ids.add(role_id)

    return role_ids


def greenie_bolter_emoji_for_member(member: discord.Member) -> str:
    """Special member override, then live CAG/DCAG role, then normal blue."""
    if str(member.id) == SPECIAL_SPEAKING_BOLTER_DISCORD_ID:
        return SPECIAL_SPEAKING_BOLTER_EMOJI

    rank_role_ids = configured_cag_dcag_role_ids()
    member_role_ids = {
        int(role.id)
        for role in getattr(member, "roles", [])
        if getattr(role, "id", None) is not None
    }

    if rank_role_ids & member_role_ids:
        return CAG_DCAG_BOLTER_EMOJI

    return BOLTER_EMOJI


class GreenieCog(commands.Cog):
    """Displays a member's carrier recovery history and Greenie GPA."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="greenie",
        description="Show a member's recent carrier attempts and wire GPA.",
    )
    @app_commands.guild_only()
    @app_commands.rename(target="user")
    @app_commands.describe(
        target="Member to view. Leave blank to view your own Greenie Board.",
    )
    async def greenie(
        self,
        interaction: discord.Interaction,
        target: discord.Member | None = None,
    ):
        if not await require_mission_qualified_command(interaction):
            return
        target = target or interaction.user

        await interaction.response.defer()

        try:
            embed = build_greenie_embed(
                discord_id=str(target.id),
                fallback_name=getattr(target, "display_name", None),
                bolter_emoji=greenie_bolter_emoji_for_member(target),
            )
        except Exception as error:
            await interaction.followup.send(
                f"Could not build that Greenie Board: `{error}`",
                ephemeral=True,
            )
            return

        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(GreenieCog(bot))
