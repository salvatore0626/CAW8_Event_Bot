from __future__ import annotations

import asyncio
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from config import INSTRUCTOR_ROLE
from services.admin_log_service import log_admin_action
from services.permission_service import (
    member_is_admin,
    require_instructor_command,
)
from services.qual_export_service import (
    create_attendance_export_file,
    create_database_backup_file,
    create_flight_lead_reviews_export_file,
    create_operation_reviews_export_file,
    create_qual_export_file,
    zip_export_file,
)


EXPORT_LABELS = {
    "qualifications": "Qualifications",
    "operation_reviews": "Operation Reviews",
    "flight_lead_reviews": "Flight Lead Reviews",
    "attendance": "Attendance",
    "database": "Full Database Copy",
}

EXPORT_DESCRIPTIONS = {
    "qualifications": "All qualification attempts with the existing formatted columns.",
    "operation_reviews": "Anonymous operation remarks grouped by operation template in a text file.",
    "flight_lead_reviews": "Anonymous FL reviews grouped alphabetically by flight leader in a text file.",
    "attendance": "All completed attendance records with review/internal columns removed.",
    "database": "A consistent SQLite backup of the complete Airboss database.",
}

EXPORT_LOG_ACTIONS = {
    "qualifications": "fax_qualifications",
    "operation_reviews": "fax_operation_reviews",
    "flight_lead_reviews": "fax_flight_lead_reviews",
    "attendance": "fax_attendance",
    "database": "fax_database",
}


def has_instructor_role(member: discord.Member) -> bool:
    if member_is_admin(member):
        return True
    return any(role.id == INSTRUCTOR_ROLE for role in member.roles)


def fax_embed(selected_type: str, *, status: str | None = None) -> discord.Embed:
    label = EXPORT_LABELS.get(selected_type, "Qualifications")
    description = EXPORT_DESCRIPTIONS.get(selected_type, "")

    if status:
        description = f"{status}\n\n**Selected export:** {label}\n{description}"
    else:
        description = (
            f"**Selected export:** {label}\n"
            f"{description}\n\n"
            "Choose an export type, then press **Export**."
        )

    return discord.Embed(
        title="Fax Center",
        description=description,
    )


def create_export_file(export_type: str) -> Path:
    if export_type == "qualifications":
        return create_qual_export_file()
    if export_type == "operation_reviews":
        return create_operation_reviews_export_file()
    if export_type == "flight_lead_reviews":
        return create_flight_lead_reviews_export_file()
    if export_type == "attendance":
        return create_attendance_export_file()
    if export_type == "database":
        return create_database_backup_file()

    raise ValueError(f"Unknown fax export type: {export_type}")


def safe_unlink(path: Path | None) -> None:
    if path is None:
        return

    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


class FaxExportTypeSelect(discord.ui.Select):
    def __init__(self, *, include_database: bool, row: int):
        options = [
            discord.SelectOption(
                label="Qualifications",
                value="qualifications",
                description="All qualification attempts.",
                default=True,
            ),
            discord.SelectOption(
                label="Operation Reviews",
                value="operation_reviews",
                description="Anonymous op remarks grouped in a text file.",
            ),
            discord.SelectOption(
                label="Flight Lead Reviews",
                value="flight_lead_reviews",
                description="Anonymous FL reviews in a text file.",
            ),
            discord.SelectOption(
                label="Attendance",
                value="attendance",
                description="All attendance with internal review fields removed.",
            ),
        ]

        if include_database:
            options.append(
                discord.SelectOption(
                    label="Full Database Copy",
                    value="database",
                    description="Admin-only consistent SQLite backup.",
                )
            )

        super().__init__(
            placeholder="Select an export type",
            min_values=1,
            max_values=1,
            options=options,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, FaxCenterView)

        selected = self.values[0]

        if selected == "database" and not member_is_admin(interaction.user):
            await interaction.response.send_message(
                "The full database copy is available to admins only.",
                ephemeral=True,
            )
            return

        self.view.selected_type = selected

        for option in self.options:
            option.default = option.value == selected

        await interaction.response.edit_message(
            embed=fax_embed(selected),
            view=self.view,
        )


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
            label="Export",
            style=discord.ButtonStyle.success,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, FaxCenterView)

        if self.view.is_exporting:
            await interaction.response.send_message(
                "The export is already being created.",
                ephemeral=True,
            )
            return

        export_type = self.view.selected_type

        if export_type == "database" and not member_is_admin(interaction.user):
            await interaction.response.send_message(
                "The full database copy is available to admins only.",
                ephemeral=True,
            )
            return

        self.view.is_exporting = True
        self.view.set_controls_disabled(True)

        await interaction.response.edit_message(
            embed=fax_embed(export_type, status="Creating the export…"),
            view=self.view,
        )

        export_path: Path | None = None
        send_path: Path | None = None

        try:
            export_path = await asyncio.to_thread(create_export_file, export_type)
            send_path = export_path

            upload_limit = int(
                getattr(interaction.guild, "filesize_limit", 10 * 1024 * 1024)
                or 10 * 1024 * 1024
            )

            # Database files often compress well. Try ZIP before reporting that the
            # attachment is too large for this server's Discord upload limit.
            if export_type == "database" and export_path.stat().st_size > upload_limit:
                send_path = await asyncio.to_thread(zip_export_file, export_path)

            if send_path.stat().st_size > upload_limit:
                size_mb = send_path.stat().st_size / (1024 * 1024)
                limit_mb = upload_limit / (1024 * 1024)
                raise ValueError(
                    f"The export is {size_mb:.1f} MB, which exceeds this server's "
                    f"{limit_mb:.1f} MB Discord attachment limit."
                )

            await interaction.followup.send(
                content=f"{EXPORT_LABELS[export_type]} export created.",
                file=discord.File(
                    fp=str(send_path),
                    filename=send_path.name,
                ),
                ephemeral=True,
            )

            log_warning = None
            try:
                await asyncio.to_thread(
                    log_admin_action,
                    action=EXPORT_LOG_ACTIONS[export_type],
                    performed_by_id=str(interaction.user.id),
                    after_json={
                        "export_type": export_type,
                        "filename": send_path.name,
                        "attachment_size_bytes": send_path.stat().st_size,
                    },
                )
            except Exception as log_error:
                log_warning = f"\n\nAudit log warning: `{type(log_error).__name__}: {log_error}`"

            await interaction.edit_original_response(
                embed=discord.Embed(
                    title="Fax Complete",
                    description=(
                        f"The **{EXPORT_LABELS[export_type]}** export was sent as an "
                        f"ephemeral attachment.{log_warning or ''}"
                    ),
                ),
                view=None,
            )

        except Exception as error:
            self.view.is_exporting = False
            self.view.set_controls_disabled(False)

            await interaction.edit_original_response(
                embed=discord.Embed(
                    title="Fax Failed",
                    description=f"Failed to create the export: `{type(error).__name__}: {error}`",
                ),
                view=self.view,
            )
        finally:
            if send_path is not None and send_path != export_path:
                safe_unlink(send_path)
            safe_unlink(export_path)


class FaxCenterView(discord.ui.View):
    def __init__(self, owner_id: int, *, include_database: bool):
        super().__init__(timeout=900)
        self.owner_id = int(owner_id)
        self.selected_type = "qualifications"
        self.is_exporting = False

        self.add_item(FaxExportTypeSelect(include_database=include_database, row=0))
        self.add_item(FaxCancelButton(row=1))
        self.add_item(FaxExportButton(row=1))

    def set_controls_disabled(self, disabled: bool) -> None:
        for child in self.children:
            child.disabled = bool(disabled)

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


class QualExportCommands(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="fax",
        description="Export Airboss qualification, review, attendance, or database records.",
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
            embed=fax_embed("qualifications"),
            view=FaxCenterView(
                owner_id=interaction.user.id,
                include_database=member_is_admin(interaction.user),
            ),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(QualExportCommands(bot))
