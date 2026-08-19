from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from services.greenie_service import (
    BOLTER_EMOJI,
    CAG_DCAG_BOLTER_EMOJI,
    SPECIAL_SPEAKING_BOLTER_EMOJI,
    build_greenie_embed,
    build_greenie_gpa_graph_file,
    greenie_graph_pages,
)
from services.permission_service import (
    require_admin_command,
    require_mission_qualified_command,
)


try:
    from config import RANK_ROLES
except ImportError:
    RANK_ROLES = []


SPECIAL_SPEAKING_BOLTER_DISCORD_ID = "165270837648293890"

GREENIE_VISIBILITY_CHOICES = [
    app_commands.Choice(name="Public", value="public"),
    app_commands.Choice(name="Hidden", value="hidden"),
]


def greenie_is_hidden(value: str | None) -> bool:
    return str(value or "public").strip().casefold() == "hidden"


def greenie_caw8_enabled(value: str | None) -> bool:
    return str(value or "no").strip().casefold() in {"yes", "y", "true", "1", "on"}


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


class GreenieGraphPageButton(discord.ui.Button):
    """A blue direct-jump button for one /greenie graph page."""

    def __init__(self, *, page_index: int, label: str, row: int):
        super().__init__(
            label=label,
            style=discord.ButtonStyle.primary,
            row=row,
        )
        self.page_index = page_index

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view

        if not isinstance(view, GreenieGraphView):
            await interaction.response.send_message(
                "That Greenie graph control is no longer available.",
                ephemeral=True,
            )
            return

        view.current_index = self.page_index
        await view.show_current_page(interaction)


class GreenieGraphView(discord.ui.View):
    """Direct-jump buttons for the /greenie total graph and airframe graphs."""

    # Discord allows five action rows with five buttons per row. Row 0 is kept
    # for the Total page. Airframes are then grouped 1-5, 6-10, 11-15, etc.
    MAX_AIRFRAME_BUTTONS = 20

    def __init__(
        self,
        *,
        requester_id: int,
        target_id: int | str,
        target_display_name: str | None,
        base_embed: discord.Embed,
        caw8: bool = False,
    ):
        super().__init__(timeout=300)
        self.requester_id = int(requester_id)
        self.target_id = str(target_id)
        self.target_display_name = target_display_name
        self.base_embed = base_embed
        self.caw8 = bool(caw8)
        self.pages = greenie_graph_pages()
        self.current_index = 0
        self._add_graph_page_buttons()

    def _add_graph_page_buttons(self) -> None:
        self.clear_items()

        if not self.pages:
            return

        # Keep Total separate so the aircraft rows are easy to scan.
        self.add_item(
            GreenieGraphPageButton(
                page_index=0,
                label=self.pages[0].label,
                row=0,
            )
        )

        # Page 0 is Total. Pages 1+ are airframes in GREENIE_AIRFRAME_ORDER.
        for aircraft_index, page in enumerate(
            self.pages[1:self.MAX_AIRFRAME_BUTTONS + 1]
        ):
            self.add_item(
                GreenieGraphPageButton(
                    page_index=aircraft_index + 1,
                    label=page.label,
                    row=1 + (aircraft_index // 5),
                )
            )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) == self.requester_id:
            return True

        await interaction.response.send_message(
            "Only the person who ran `/greenie` can use these buttons.",
            ephemeral=True,
        )
        return False

    async def show_current_page(self, interaction: discord.Interaction) -> None:
        page = self.pages[self.current_index]
        embed = self.base_embed.copy()

        graph_file = build_greenie_gpa_graph_file(
            discord_id=self.target_id,
            fallback_name=self.target_display_name,
            aircraft_filter=page.aircraft_filter,
            graph_label=page.label,
            caw8=self.caw8,
        )

        if graph_file is None:
            embed.set_image(url=None)
            await interaction.response.edit_message(
                embed=embed,
                attachments=[],
                view=self,
            )
            return

        embed.set_image(url=f"attachment://{graph_file.filename}")
        await interaction.response.edit_message(
            embed=embed,
            attachments=[graph_file],
            view=self,
        )


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
        target="Member to view. Ignored when CAW8 is Yes.",
        caw8="Yes = show CAW8-wide Greenie Board with all users combined.",
        visibility="Whether the response is public or only visible to you.",
    )
    @app_commands.choices(
        caw8=[
            app_commands.Choice(name="No", value="no"),
            app_commands.Choice(name="Yes", value="yes"),
        ],
        visibility=GREENIE_VISIBILITY_CHOICES,
    )
    async def greenie(
        self,
        interaction: discord.Interaction,
        target: discord.Member | None = None,
        caw8: str = "no",
        visibility: str = "public",
    ):
        caw8_mode = greenie_caw8_enabled(caw8)
        hidden = greenie_is_hidden(visibility)

        if caw8_mode:
            if not await require_admin_command(interaction):
                return
        elif not await require_mission_qualified_command(interaction):
            return

        # Acknowledge the slash command immediately so Discord does not show
        # "Application did not respond" while DB reads or graph rendering run.
        if not interaction.response.is_done():
            await interaction.response.defer(thinking=True, ephemeral=hidden)

        target = target or interaction.user
        target_id = "CAW8" if caw8_mode else str(target.id)
        target_display_name = "CAW8" if caw8_mode else getattr(target, "display_name", None)

        try:
            embed = build_greenie_embed(
                discord_id=target_id,
                fallback_name=target_display_name,
                bolter_emoji=BOLTER_EMOJI if caw8_mode else greenie_bolter_emoji_for_member(target),
                caw8=caw8_mode,
            )
        except Exception as error:
            await interaction.followup.send(
                f"Could not build that Greenie Board: `{error}`",
                ephemeral=True,
            )
            return

        graph_file = None
        view = None

        try:
            pages = greenie_graph_pages()
            first_page = pages[0]
            graph_file = build_greenie_gpa_graph_file(
                discord_id=target_id,
                fallback_name=target_display_name,
                aircraft_filter=first_page.aircraft_filter,
                graph_label=first_page.label,
                caw8=caw8_mode,
            )

            if graph_file is not None:
                embed.set_image(url=f"attachment://{graph_file.filename}")
                view = GreenieGraphView(
                    requester_id=int(interaction.user.id),
                    target_id=target_id,
                    target_display_name=target_display_name,
                    base_embed=embed,
                    caw8=caw8_mode,
                )
        except Exception:
            # Do not fail the whole /greenie command if the graph has a problem.
            # The normal Greenie embed is still useful by itself.
            graph_file = None
            view = None

        if graph_file is not None:
            await interaction.followup.send(
                embed=embed,
                file=graph_file,
                view=view,
                ephemeral=hidden,
            )
        else:
            await interaction.followup.send(embed=embed, ephemeral=hidden)


async def setup(bot: commands.Bot):
    await bot.add_cog(GreenieCog(bot))
