from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands
from services.permission_service import (
    require_admin_command,
    member_is_admin,
)

try:
    from config import STAFF_ROLE
except ImportError:
    STAFF_ROLE = 0

try:
    from config import MISSION_EXECUTER_ROLE
except ImportError:
    MISSION_EXECUTER_ROLE = 0

try:
    from config import MISSION_EXECUTER_ROLES
except ImportError:
    MISSION_EXECUTER_ROLES = []

try:
    from config import INSTRUCTOR_ROLE
except ImportError:
    INSTRUCTOR_ROLE = 0

try:
    from config import PROMOTION_ANNOUNCEMENT_CHANNEL_ID
except ImportError:
    PROMOTION_ANNOUNCEMENT_CHANNEL_ID = 0

try:
    from config import PROMOTION_SINGLE_TEMPLATE
except ImportError:
    PROMOTION_SINGLE_TEMPLATE = (
        "🎖️ Congratulations {mention}! You have been promoted from "
        "**{old_rank}** to **{new_rank}**."
    )

try:
    from config import PROMOTION_BATCH_TEMPLATE
except ImportError:
    PROMOTION_BATCH_TEMPLATE = (
        "🎖️ Congratulations to everyone promoted today!\n\n"
        "{promotion_lines}"
    )

try:
    from config import PROMOTION_BATCH_LINE_TEMPLATE
except ImportError:
    PROMOTION_BATCH_LINE_TEMPLATE = (
        "- {mention}: **{old_rank}** → **{new_rank}** "
        "({total_ops} total ops, {unique_ops} unique ops)"
    )

from services.promotion_service import (
    PromotionCandidate,
    all_rank_role_ids,
    eligible_rank_choices,
    ensure_user_record,
    find_promotion_candidate_for_user,
    find_promotion_candidates,
    highest_rank_from_role_ids,
    is_managed_rank,
    normalize_rank,
    rank_role_id,
    remove_do_not_promote,
    set_do_not_promote,
    update_user_rank,
)


PROMOTION_LOCKS: set[str] = set()


def promotion_lock_key(
    *,
    guild_id: int | None,
    mode: str,
    candidate_discord_id: str | None,
) -> str:
    guild_part = str(guild_id or 0)

    if mode == "single":
        return f"{guild_part}:single:{candidate_discord_id or 'none'}"

    return f"{guild_part}:all"


def configured_role_ids() -> set[int]:
    ids: set[int] = set()

    for value in (STAFF_ROLE, MISSION_EXECUTER_ROLE, INSTRUCTOR_ROLE):
        try:
            role_id = int(value or 0)
        except (TypeError, ValueError):
            role_id = 0

        if role_id:
            ids.add(role_id)

    for value in MISSION_EXECUTER_ROLES:
        try:
            role_id = int(value or 0)
        except (TypeError, ValueError):
            role_id = 0

        if role_id:
            ids.add(role_id)

    return ids


async def has_promotion_permission(interaction: discord.Interaction) -> bool:
    role_ids = configured_role_ids()

    if not role_ids:
        return True

    if not isinstance(interaction.user, discord.Member):
        return False

    if member_is_admin(interaction.user):
        return True

    return any(role.id in role_ids for role in interaction.user.roles)


def promotion_line(candidate: PromotionCandidate) -> str:
    return (
        f"- <@{candidate.discord_id}> `{candidate.username}`: "
        f"**{candidate.current_rank}** → **{candidate.next_rank}** "
        f"({candidate.total_ops}/{candidate.required_total_ops} total, "
        f"{candidate.unique_ops}/{candidate.required_unique_ops} unique)"
    )


def blocked_line(blocked) -> str:
    return (
        f"- `{blocked.username}`: {blocked.current_rank} → {blocked.next_rank} blocked "
        f"by max rank **{blocked.max_rank}** "
        f"({blocked.total_ops} total, {blocked.unique_ops} unique)"
    )


def promotion_confirm_text(candidates: list[PromotionCandidate]) -> str:
    lines = [promotion_line(candidate) for candidate in candidates[:20]]

    if len(candidates) > 20:
        lines.append(f"\n...and {len(candidates) - 20} more users.")

    return "\n".join(lines) if lines else "No eligible users found."


def announcement_line(candidate: PromotionCandidate) -> str:
    return PROMOTION_BATCH_LINE_TEMPLATE.format(
        mention=f"<@{candidate.discord_id}>",
        old_rank=candidate.current_rank,
        new_rank=candidate.next_rank,
        total_ops=candidate.total_ops,
        unique_ops=candidate.unique_ops,
    )


async def send_single_announcement(
    bot: commands.Bot,
    candidate: PromotionCandidate,
) -> None:
    if not PROMOTION_ANNOUNCEMENT_CHANNEL_ID:
        return

    channel = bot.get_channel(int(PROMOTION_ANNOUNCEMENT_CHANNEL_ID))

    if channel is None:
        return

    message = PROMOTION_SINGLE_TEMPLATE.format(
        mention=f"<@{candidate.discord_id}>",
        old_rank=candidate.current_rank,
        new_rank=candidate.next_rank,
    )

    await channel.send(message)


async def send_batch_announcement(
    bot: commands.Bot,
    candidates: list[PromotionCandidate],
) -> None:
    if not PROMOTION_ANNOUNCEMENT_CHANNEL_ID or not candidates:
        return

    channel = bot.get_channel(int(PROMOTION_ANNOUNCEMENT_CHANNEL_ID))

    if channel is None:
        return

    lines = "\n".join(announcement_line(candidate) for candidate in candidates)
    message = PROMOTION_BATCH_TEMPLATE.format(promotion_lines=lines)

    await channel.send(message)


async def guild_member_for_candidate(
    guild: discord.Guild,
    candidate: PromotionCandidate,
) -> discord.Member | None:
    try:
        member = guild.get_member(int(candidate.discord_id))

        if member is not None:
            return member

        return await guild.fetch_member(int(candidate.discord_id))
    except Exception:
        return None


def member_rank_role_ids(member: discord.Member) -> set[int]:
    return {
        int(role.id)
        for role in getattr(member, "roles", [])
        if getattr(role, "id", None) is not None
    }


def live_rank_for_member(member: discord.Member) -> str | None:
    return highest_rank_from_role_ids(member_rank_role_ids(member))


async def sync_user_rank_from_live_roles(member: discord.Member) -> str | None:
    """Sync users.rank to the highest current Discord rank role when present."""
    live_rank = live_rank_for_member(member)

    if live_rank is not None:
        update_user_rank(str(member.id), live_rank)

    return live_rank


async def filter_candidates_by_live_rank(
    *,
    guild: discord.Guild,
    candidates: list[PromotionCandidate],
) -> tuple[list[PromotionCandidate], list[str]]:
    """Remove stale/high-rank candidates before showing or confirming boards.

    The database can lag behind Discord roles. If a member currently has a
    higher/non-managed role like CAG/DCAG, sync users.rank and remove them from
    the automatic promotion list instead of letting them be downgraded.
    """
    filtered: list[PromotionCandidate] = []
    skipped: list[str] = []

    for candidate in candidates:
        member = await guild_member_for_candidate(guild, candidate)

        if member is None:
            filtered.append(candidate)
            continue

        live_rank = await sync_user_rank_from_live_roles(member)

        if live_rank is None:
            filtered.append(candidate)
            continue

        if not is_managed_rank(live_rank):
            skipped.append(
                f"{member.display_name}: has **{live_rank}** role; skipped automatic ladder."
            )
            continue

        if normalize_rank(live_rank) != normalize_rank(candidate.current_rank):
            refreshed, reason = find_promotion_candidate_for_user(candidate.discord_id)

            if refreshed is not None:
                filtered.append(refreshed)
            else:
                skipped.append(
                    f"{member.display_name}: synced live rank to **{live_rank}**; "
                    f"not eligible. {reason or ''}"
                )
            continue

        filtered.append(candidate)

    # Deduplicate in case live-rank refresh returns duplicates.
    deduped: list[PromotionCandidate] = []
    seen: set[str] = set()

    for candidate in filtered:
        if candidate.discord_id in seen:
            continue

        seen.add(candidate.discord_id)
        deduped.append(candidate)

    return deduped, skipped


async def apply_discord_rank_roles(
    *,
    guild: discord.Guild,
    member: discord.Member,
    candidate: PromotionCandidate,
) -> tuple[bool, str]:
    live_rank = await sync_user_rank_from_live_roles(member)

    if live_rank is not None and normalize_rank(live_rank) != normalize_rank(candidate.current_rank):
        return (
            False,
            (
                f"{member.display_name}: current Discord rank role is {live_rank}, "
                f"but the queued promotion expected {candidate.current_rank}. "
                "Synced users.rank and skipped to prevent a downgrade."
            ),
        )

    if live_rank is not None and not is_managed_rank(live_rank):
        return (
            False,
            (
                f"{member.display_name}: current Discord rank role is {live_rank}; "
                "automatic promotion ladder skipped."
            ),
        )

    old_role_id = rank_role_id(candidate.current_rank)
    new_role_id = rank_role_id(candidate.next_rank)

    if new_role_id is None:
        return False, f"No role configured for new rank {candidate.next_rank}."

    old_role = guild.get_role(old_role_id) if old_role_id else None
    new_role = guild.get_role(new_role_id)

    if new_role is None:
        return False, f"Discord role for {candidate.next_rank} was not found."

    try:
        roles_to_remove = []

        if old_role is not None and old_role in member.roles:
            roles_to_remove.append(old_role)

        # Safety: remove any other managed rank role the member has, except the new role.
        for role_id in all_rank_role_ids():
            role = guild.get_role(role_id)
            if role is None or role == new_role:
                continue

            if role in member.roles and role not in roles_to_remove:
                roles_to_remove.append(role)

        if roles_to_remove:
            await member.remove_roles(
                *roles_to_remove,
                reason=f"Promotion {candidate.current_rank} -> {candidate.next_rank}",
            )

        if new_role not in member.roles:
            await member.add_roles(
                new_role,
                reason=f"Promotion {candidate.current_rank} -> {candidate.next_rank}",
            )

        update_user_rank(candidate.discord_id, candidate.next_rank)

        return True, f"{member.display_name}: {candidate.current_rank} → {candidate.next_rank}"
    except Exception as error:
        return False, f"{member.display_name}: {type(error).__name__}: {error}"


async def promote_candidates(
    *,
    bot: commands.Bot,
    guild: discord.Guild,
    candidates: list[PromotionCandidate],
) -> tuple[list[PromotionCandidate], list[str]]:
    promoted: list[PromotionCandidate] = []
    errors: list[str] = []

    for candidate in candidates:
        member = await guild_member_for_candidate(guild, candidate)

        if member is None:
            errors.append(f"{candidate.username}: member not found in server.")
            continue

        ok, message = await apply_discord_rank_roles(
            guild=guild,
            member=member,
            candidate=candidate,
        )

        if ok:
            promoted.append(candidate)
        else:
            errors.append(message)

    if len(promoted) == 1:
        await send_single_announcement(bot, promoted[0])
    elif len(promoted) > 1:
        await send_batch_announcement(bot, promoted)

    return promoted, errors


class PromotionBoardView(discord.ui.View):
    def __init__(self, *, owner_id: int, can_promote_all: bool):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.add_item(DismissPromotionViewButton(row=0))
        self.add_item(PromoteAllButton(disabled=not can_promote_all, row=0))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This promotion view belongs to someone else.",
                ephemeral=True,
            )
            return False

        return True


class PromotionConfirmView(discord.ui.View):
    def __init__(
        self,
        *,
        owner_id: int,
        mode: str,
        candidate_discord_id: str | None = None,
    ):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.mode = mode
        self.candidate_discord_id = candidate_discord_id
        self.add_item(CancelPromotionButton(row=0))
        self.add_item(ConfirmPromotionButton(row=0))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This promotion confirmation belongs to someone else.",
                ephemeral=True,
            )
            return False

        return True


class DismissPromotionViewButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(
            label="Dismiss",
            style=discord.ButtonStyle.secondary,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=None)


class CancelPromotionButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content="Promotion cancelled.",
            embed=None,
            view=None,
        )


class PromoteAllButton(discord.ui.Button):
    def __init__(self, *, disabled: bool, row: int):
        super().__init__(
            label="Promote All",
            style=discord.ButtonStyle.success,
            disabled=disabled,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        candidates, _blocked = find_promotion_candidates()

        if interaction.guild is not None and candidates:
            candidates, _skipped = await filter_candidates_by_live_rank(
                guild=interaction.guild,
                candidates=candidates,
            )

        if not candidates:
            await interaction.response.send_message(
                "No users are currently eligible for promotion.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="Confirm Promote All",
            description=(
                f"You are about to promote **{len(candidates)}** eligible user(s):\n\n"
                f"{promotion_confirm_text(candidates)}"
            )[:4000],
        )

        embed.set_footer(text="This will remove old rank roles, add new rank roles, and update users.rank.")

        await interaction.response.edit_message(
            embed=embed,
            view=PromotionConfirmView(
                owner_id=interaction.user.id,
                mode="all",
            ),
        )


class ConfirmPromotionButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(
            label="Confirm Promotion",
            style=discord.ButtonStyle.success,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, PromotionConfirmView)

        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        lock_key = promotion_lock_key(
            guild_id=interaction.guild.id,
            mode=self.view.mode,
            candidate_discord_id=self.view.candidate_discord_id,
        )

        if lock_key in PROMOTION_LOCKS:
            await interaction.response.send_message(
                "That promotion is already running. Wait for it to finish.",
                ephemeral=True,
            )
            return

        PROMOTION_LOCKS.add(lock_key)

        try:
            await interaction.response.edit_message(
                content="Promoting... please wait.",
                embed=None,
                view=None,
            )

            if self.view.mode == "single":
                if not self.view.candidate_discord_id:
                    await interaction.edit_original_response(
                        content="No user was selected for promotion.",
                        embed=None,
                        view=None,
                    )
                    return

                candidate, reason = find_promotion_candidate_for_user(self.view.candidate_discord_id)

                if candidate is None:
                    await interaction.edit_original_response(
                        content=f"That user is no longer eligible. {reason or ''}",
                        embed=None,
                        view=None,
                    )
                    return

                candidates = [candidate]
            else:
                candidates, _blocked = find_promotion_candidates()

            if candidates:
                candidates, live_rank_skips = await filter_candidates_by_live_rank(
                    guild=interaction.guild,
                    candidates=candidates,
                )
            else:
                live_rank_skips = []

            # One promotion command only advances each user one rank.
            # We use the candidate list calculated once at confirmation time.
            promoted, errors = await promote_candidates(
                bot=interaction.client,
                guild=interaction.guild,
                candidates=candidates,
            )

            lines: list[str] = []

            if promoted:
                lines.append("Promoted:")
                lines.extend(
                    f"- <@{candidate.discord_id}>: **{candidate.current_rank}** → **{candidate.next_rank}**"
                    for candidate in promoted
                )

            combined_errors = live_rank_skips + errors

            if combined_errors:
                if lines:
                    lines.append("")

                lines.append("Errors:")
                lines.extend(f"- {error}" for error in combined_errors[:20])

                if len(combined_errors) > 20:
                    lines.append(f"...and {len(combined_errors) - 20} more errors.")

            if not lines:
                lines.append("No promotions were completed.")

            await interaction.edit_original_response(
                content="\n".join(lines)[:1900],
                embed=None,
                view=None,
            )
        finally:
            PROMOTION_LOCKS.discard(lock_key)


class PromotionsCog(commands.Cog):
    do_group = app_commands.Group(
        name="do",
        description="Administrative restrictions.",
    )

    do_not_group = app_commands.Group(
        name="not",
        description="Do-not administrative restrictions.",
        parent=do_group,
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="promote",
        description="Show promotion board, or check/promote one eligible user.",
    )
    @app_commands.describe(user="Optional single user to promote if eligible")
    async def promote(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
    ):
        if not await require_admin_command(interaction):
            return
        if not await has_promotion_permission(interaction):
            await interaction.response.send_message(
                "You do not have permission to use promotion tools.",
                ephemeral=True,
            )
            return

        if user is not None:
            ensure_user_record(
                discord_id=str(user.id),
                discord_username=user.name,
                display_name=user.display_name,
            )
            live_rank = await sync_user_rank_from_live_roles(user)

            candidate, reason = find_promotion_candidate_for_user(str(user.id))

            if candidate is None:
                live_note = f" Current Discord rank role: **{live_rank}**." if live_rank else ""
                await interaction.response.send_message(
                    f"{user.mention} is not currently eligible for promotion. {reason or ''}{live_note}",
                    ephemeral=True,
                )
                return

            embed = discord.Embed(
                title="Confirm Single Promotion",
                description=(
                    f"You are about to promote {user.mention}:\n\n"
                    f"{promotion_line(candidate)}"
                ),
            )
            embed.set_footer(text="This will remove their old rank role, add their new rank role, and update users.rank.")

            await interaction.response.send_message(
                embed=embed,
                view=PromotionConfirmView(
                    owner_id=interaction.user.id,
                    mode="single",
                    candidate_discord_id=str(user.id),
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        candidates, blocked = find_promotion_candidates()
        live_rank_skips: list[str] = []

        if interaction.guild is not None and candidates:
            candidates, live_rank_skips = await filter_candidates_by_live_rank(
                guild=interaction.guild,
                candidates=candidates,
            )

        if candidates:
            lines = [promotion_line(candidate) for candidate in candidates[:20]]
            description = "\n".join(lines)

            if len(candidates) > 20:
                description += f"\n\n...and {len(candidates) - 20} more eligible users."
        else:
            description = "No users are currently eligible for promotion."

        embed = discord.Embed(
            title="Promotion Board",
            description=description[:4000],
        )

        if live_rank_skips:
            embed.add_field(
                name="Skipped Live Rank Roles",
                value="\n".join(f"- {line}" for line in live_rank_skips[:10])[:1000],
                inline=False,
            )

        if blocked:
            blocked_lines = [blocked_line(item) for item in blocked[:10]]
            value = "\n".join(blocked_lines)

            if len(blocked) > 10:
                value += f"\n...and {len(blocked) - 10} more capped users."

            embed.add_field(
                name="Capped by Do Not Promote",
                value=value[:1000],
                inline=False,
            )

        embed.set_footer(text="Use Promote All to confirm and apply all listed eligible promotions.")

        await interaction.followup.send(
            embed=embed,
            view=PromotionBoardView(
                owner_id=interaction.user.id,
                can_promote_all=bool(candidates),
            ),
            ephemeral=True,
        )

    @do_not_group.command(
        name="promote",
        description="Set the max rank a user may be promoted to.",
    )
    @app_commands.describe(
        user="User to cap",
        maxrank="Highest rank this user is allowed to reach",
    )
    async def do_not_promote(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        maxrank: str,
    ):
        if not await require_admin_command(interaction):
            return
        if not await has_promotion_permission(interaction):
            await interaction.response.send_message(
                "You do not have permission to use promotion tools.",
                ephemeral=True,
            )
            return

        ensure_user_record(
            discord_id=str(user.id),
            discord_username=user.name,
            display_name=user.display_name,
        )

        try:
            saved_rank = set_do_not_promote(
                discord_id=str(user.id),
                max_rank=maxrank,
                performed_by_id=str(interaction.user.id),
                reason=None,
            )
        except ValueError as error:
            await interaction.response.send_message(
                str(error),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"Set {user.mention}'s max promotion rank to **{saved_rank}**.",
            ephemeral=True,
        )

    @do_not_promote.autocomplete("maxrank")
    async def maxrank_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return [
            app_commands.Choice(name=rank, value=rank)
            for rank in eligible_rank_choices(current)
        ]

    @app_commands.command(
        name="clearpromotecap",
        description="Clear a user's do-not-promote max rank cap.",
    )
    @app_commands.describe(user="User to clear")
    async def clear_promotion_cap(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
    ):
        if not await require_admin_command(interaction):
            return
        if not await has_promotion_permission(interaction):
            await interaction.response.send_message(
                "You do not have permission to use promotion tools.",
                ephemeral=True,
            )
            return

        remove_do_not_promote(str(user.id))

        await interaction.response.send_message(
            f"Cleared promotion cap for {user.mention}.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(PromotionsCog(bot))
