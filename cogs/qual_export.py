from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from config import INSTRUCTOR_ROLE
from services.qual_export_service import create_qual_export_file
from services.permission_service import (
    require_instructor_command,
    member_is_admin,
)


def has_instructor_role(member: discord.Member) -> bool:
    if member_is_admin(member):
        return True
    return any(role.id == INSTRUCTOR_ROLE for role in member.roles)


class FaxConfirmView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.is_exporting = False

        self.add_item(FaxCancelButton(row=0))
        self.add_item(FaxExportButton(row=0))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the person who ran /fax can use these controls.",
                ephemeral=True,
            )
            return False

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This command can only be used inside the server.",
                ephemeral=True,
            )
            return False

        if not has_instructor_role(interaction.user):
            await interaction.response.send_message(
                "You need the instructor role to use this command.",
                ephemeral=True,
            )
            return False

        return True


class FaxCancelButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="Fax Cancelled",
                description="No export was created.",
            ),
            view=None,
        )


class FaxExportButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(
            label="Export Excel",
            style=discord.ButtonStyle.success,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, FaxConfirmView)

        if self.view.is_exporting:
            await interaction.response.send_message(
                "The export is already being created.",
                ephemeral=True,
            )
            return

        self.view.is_exporting = True

        await interaction.response.edit_message(
            content="Loading...",
            embed=None,
            view=None,
        )

        try:
            export_path = create_qual_export_file()
        except Exception as error:
            await interaction.edit_original_response(
                content=f"Failed to export qualification attempts: `{error}`",
                embed=None,
                view=None,
            )
            return

        await interaction.followup.send(
            content="Qualification attempt export created.",
            file=discord.File(
                fp=str(export_path),
                filename=export_path.name,
            ),
            ephemeral=True,
        )


class QualExportCommands(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="fax",
        description="Export all qualification attempts with instructor/submitted-by info to a formatted Excel workbook.",
    )
    @app_commands.guild_only()
    async def fax_command(
        self,
        interaction: discord.Interaction,
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

        await interaction.response.send_message(
            embed=discord.Embed(
                title="Fax Qualification Attempts?",
                description="Export all qualification attempts with instructor/submitted-by info to a formatted Excel workbook.",
            ),
            view=FaxConfirmView(owner_id=interaction.user.id),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(QualExportCommands(bot))
