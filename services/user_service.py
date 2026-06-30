import discord

from config import DEFAULT_RANK, RANK_ROLES
from database import (
    calculate_member_status,
    mark_users_not_in_server_as_mia,
    upsert_user,
)


def get_highest_rank_from_member(member: discord.Member) -> str:
    """
    Checks the member's Discord roles against config.RANK_ROLES.

    RANK_ROLES should be ordered lowest to highest.
    If the user has multiple rank roles, the highest matching role wins.
    """
    member_role_ids = {role.id for role in member.roles}

    highest_rank = DEFAULT_RANK

    for rank_data in RANK_ROLES:
        rank_name = rank_data["rank"]
        role_id = int(rank_data["role_id"])

        if role_id in member_role_ids:
            highest_rank = rank_name

    return highest_rank


def get_discord_username(member: discord.Member) -> str:
    """
    Discord usernames changed over time.
    This keeps the current username snapshot.
    """
    return str(member.name)


def get_display_name(member: discord.Member) -> str:
    return str(member.display_name)


def sync_member_to_users_table(member: discord.Member) -> None:
    if member.bot:
        return

    discord_id = str(member.id)
    rank = get_highest_rank_from_member(member)
    status = calculate_member_status(discord_id, is_in_server=True)

    upsert_user(
        discord_id=discord_id,
        discord_username=get_discord_username(member),
        display_name=get_display_name(member),
        rank=rank,
        status=status,
    )


async def sync_guild_users(guild: discord.Guild) -> None:
    """
    Runs on bot startup.
    Updates every non-bot member in the users table.
    Also marks users missing from the server as MIA.
    """
    current_member_ids: set[str] = set()

    async for member in guild.fetch_members(limit=None):
        if member.bot:
            continue

        current_member_ids.add(str(member.id))
        sync_member_to_users_table(member)

    mark_users_not_in_server_as_mia(current_member_ids)