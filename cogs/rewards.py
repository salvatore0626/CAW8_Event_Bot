from __future__ import annotations

from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from services.leaderboard_service import queue_leaderboard_refresh
from services.reward_service import (
    active_awards_for_player,
    active_manual_awards_for_player,
    completed_normal_events_for_autocomplete,
    configured_manual_awards,
    grant_manual_award,
    manual_award_display_name,
    reconcile_rewards_and_refresh_leaderboard_now,
    revoke_manual_award,
)
from services.permission_service import member_is_admin


try:
    from config import MISSION_EXECUTER_ROLE
except ImportError:
    MISSION_EXECUTER_ROLE = 0

try:
    from config import MISSION_EXECUTER_ROLES
except ImportError:
    MISSION_EXECUTER_ROLES = []

try:
    from config import STAFF_ROLE
except ImportError:
    STAFF_ROLE = 0


AUTO_AWARD_REQUIREMENTS = [
    (
        "ACE",
        "Completed Normal op, Arrested landing, 3-wire, 0 bolters, 0 combat deaths.",
    ),
    (
        "Golden Wrench",
        "Every 5 completed Normal ops attended without a combat death.",
    ),
    (
        "Safety S",
        "Every 5 clean Arrested recoveries in completed Normal ops with 0 bolters.",
    ),
]


def clean_text(value: Any) -> str | None:
    if value is None:
        return None

    text = str(value).strip()
    return text or None


def configured_award_staff_role_ids() -> set[int]:
    role_ids: set[int] = set()

    for raw_value in [STAFF_ROLE, MISSION_EXECUTER_ROLE]:
        try:
            if raw_value:
                role_ids.add(int(raw_value))
        except (TypeError, ValueError):
            pass

    for raw_value in MISSION_EXECUTER_ROLES or []:
        try:
            if raw_value:
                role_ids.add(int(raw_value))
        except (TypeError, ValueError):
            pass

    return role_ids


def can_manage_awards(member: discord.abc.User) -> bool:
    role_ids = configured_award_staff_role_ids()

    if not role_ids:
        return True

    if not isinstance(member, discord.Member):
        return False

    if member_is_admin(member):
        return True

    return any(role.id in role_ids for role in member.roles)


def parse_award_id(value: str | int) -> int | None:
    text = str(value or "").strip()

    if text.startswith("#"):
        text = text[1:].strip()

    first = text.split(" ", 1)[0].strip()
    return int(first) if first.isdigit() else None


def award_display_name_for_record(record) -> str:
    if getattr(record, "award_source", None) == "manual":
        return manual_award_display_name(record.award_type, record.details_json)

    return {
        "ACE": "ACE",
        "GOLDEN_WRENCH": "Golden Wrench",
        "SAFETY_S": "Safety S",
        "BATTLE_E": "Battle E",
    }.get(str(record.award_type), str(record.award_type).replace("_", " ").title())


def manual_awards_text() -> str:
    awards = configured_manual_awards()

    if not awards:
        return "No manual awards are configured."

    return "\n".join(f"- {award_name}" for award_name in awards)


def requirements_text() -> str:
    lines = [
        "**Automatic Awards**",
        *[
            f"- **{name}:** {requirement}"
            for name, requirement in AUTO_AWARD_REQUIREMENTS
        ],
        "",
        "**Manual Awards from config.py**",
        manual_awards_text(),
    ]

    return "\n".join(lines)


def user_awards_text(target: discord.Member, awards: list) -> str:
    if not awards:
        return f"{target.mention} has no active recorded awards yet."

    lines: list[str] = []

    for record in awards[:30]:
        event_text = (
            f" | Op #{record.source_event_id}"
            if record.source_event_id is not None
            else ""
        )
        note_text = f" | {record.notes}" if clean_text(record.notes) else ""
        lines.append(
            f"`#{record.award_id}` **{award_display_name_for_record(record)}**"
            f"{event_text} | <t:{record.earned_at}:d>{note_text}"
        )

    if len(awards) > 30:
        lines.append(f"...and {len(awards) - 30} more.")

    return "\n".join(lines)


def build_awards_embed(target: discord.Member, awards: list) -> discord.Embed:
    embed = discord.Embed(
        title="Awards",
        description=(
            requirements_text()
            + "\n\n"
            + f"**{target.display_name}'s Active Awards**\n"
            + user_awards_text(target, awards)
        ),
    )

    embed.set_footer(text="Manual awards are configured in config.py MANUAL_AWARDS.")
    return embed




async def normal_completed_op_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    rows = completed_normal_events_for_autocomplete(
        query=current,
        limit=25,
    )

    return [
        app_commands.Choice(
            name=f"#{int(row['event_id'])} {row['op_name']}"[:100],
            value=str(int(row["event_id"])),
        )
        for row in rows
    ]

async def manual_award_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    query = str(current or "").casefold()
    choices: list[app_commands.Choice[str]] = []

    for award_name in configured_manual_awards():
        if query and query not in award_name.casefold():
            continue

        choices.append(
            app_commands.Choice(
                name=award_name[:100],
                value=award_name,
            )
        )

    return choices[:25]


async def active_manual_award_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    namespace_user = getattr(interaction.namespace, "user", None)
    if namespace_user is None:
        return []

    discord_id = str(getattr(namespace_user, "id", "") or "")
    if not discord_id:
        return []

    query = str(current or "").casefold()
    awards = active_manual_awards_for_player(discord_id, limit=25)
    choices: list[app_commands.Choice[str]] = []

    for award in awards:
        display_name = manual_award_display_name(award.award_type, award.details_json)
        label = f"#{award.award_id} {display_name}"

        if award.source_event_id is not None:
            label += f" - Op #{award.source_event_id}"

        if query and query not in label.casefold():
            continue

        choices.append(
            app_commands.Choice(
                name=label[:100],
                value=str(award.award_id),
            )
        )

    return choices[:25]


class RewardsCog(commands.Cog):
    award = app_commands.Group(
        name="award",
        description="Manage player decorations and award calculations.",
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="awards",
        description="View award requirements, configured manual awards, and a user's awards.",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        user="Optional user to inspect. If omitted, this shows your awards.",
    )
    async def awards(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
    ):
        target = user or interaction.user
        awards = active_awards_for_player(str(target.id), limit=100)

        await interaction.response.send_message(
            embed=build_awards_embed(target, awards),
            ephemeral=True,
        )

    @app_commands.command(
        name="giveaward",
        description="Grant a configured manual award to a user.",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        user="Player receiving the manual award.",
        award="Award name from config.MANUAL_AWARDS.",
        op="Completed Normal operation where it was earned.",
        reason="Why they got the award.",
    )
    @app_commands.autocomplete(
        award=manual_award_autocomplete,
        op=normal_completed_op_autocomplete,
    )
    async def giveaward(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        award: str,
        op: str,
        reason: str,
    ):
        if not can_manage_awards(interaction.user):
            await interaction.response.send_message(
                "You need Staff or Mission Executer access to grant manual awards.",
                ephemeral=True,
            )
            return

        event_id = parse_award_id(op)
        if event_id is None:
            await interaction.response.send_message(
                "Select a completed Normal operation from the list.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            record = grant_manual_award(
                discord_id=str(user.id),
                award_name=award,
                source_event_id=event_id,
                granted_by_id=str(interaction.user.id),
                notes=reason,
            )
        except Exception as error:
            await interaction.followup.send(
                f"Could not grant manual award: `{error}`",
                ephemeral=True,
            )
            return

        display_name = manual_award_display_name(record.award_type, record.details_json)

        queue_leaderboard_refresh(
            interaction.client,
            reason=f"manual award granted:{record.award_type}",
        )

        await interaction.followup.send(
            (
                f"{display_name} granted to {user.mention}.\n"
                f"Award ID: `#{record.award_id}`\n"
                f"Operation: `#{event_id}`\n"
                f"Reason: {record.notes}"
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="revokeaward",
        description="Revoke one active manual award from a user.",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        user="Player whose manual award should be revoked.",
        award="Active manual award ID/name from the selected user.",
    )
    @app_commands.autocomplete(award=active_manual_award_autocomplete)
    async def revokeaward(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        award: str,
    ):
        if not can_manage_awards(interaction.user):
            await interaction.response.send_message(
                "You need Staff or Mission Executer access to revoke manual awards.",
                ephemeral=True,
            )
            return

        award_id = parse_award_id(award)
        if award_id is None:
            await interaction.response.send_message(
                "Select an active manual award from the list.",
                ephemeral=True,
            )
            return

        active_ids = {
            record.award_id
            for record in active_manual_awards_for_player(str(user.id), limit=100)
        }

        if int(award_id) not in active_ids:
            await interaction.response.send_message(
                "That active manual award does not belong to the selected user.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            record = revoke_manual_award(
                award_id=int(award_id),
                revoked_by_id=str(interaction.user.id),
                reason=None,
            )
        except Exception as error:
            await interaction.followup.send(
                f"Could not revoke manual award: `{error}`",
                ephemeral=True,
            )
            return

        display_name = manual_award_display_name(record.award_type, record.details_json)

        queue_leaderboard_refresh(
            interaction.client,
            reason=f"manual award revoked:{record.award_type}",
        )

        await interaction.followup.send(
            f"Revoked {display_name} award `#{record.award_id}` from {user.mention}.",
            ephemeral=True,
        )




    @award.command(
        name="recalculate",
        description="Recalculate automatic awards and refresh the leaderboard.",
    )
    @app_commands.guild_only()
    async def recalculate(self, interaction: discord.Interaction):
        if not can_manage_awards(interaction.user):
            await interaction.response.send_message(
                "You need Staff or Mission Executer access to recalculate awards.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            result = await reconcile_rewards_and_refresh_leaderboard_now(
                interaction.client,
            )
        except Exception as error:
            await interaction.followup.send(
                f"Could not recalculate awards: `{error}`",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            (
                "Automatic award reconciliation finished.\n"
                f"Granted: `{result.granted}`\n"
                f"Restored: `{result.reactivated}`\n"
                f"Revoked: `{result.revoked}`\n"
                f"Still valid: `{result.unchanged}`"
            ),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(RewardsCog(bot))
