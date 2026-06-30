import discord
from discord.ext import commands

from services.user_service import (
    get_highest_rank_from_member,
    sync_guild_users,
    sync_member_to_users_table,
)


class Watchdog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.startup_synced = False

    @commands.Cog.listener()
    async def on_ready(self):
        """
        Runs when the bot is online.

        This updates the users table using the current server members.
        """
        if self.startup_synced:
            return

        self.startup_synced = True

        print(f"✅ Logged in as {self.bot.user}")

        for guild in self.bot.guilds:
            print(f"🔄 Syncing users for guild: {guild.name} ({guild.id})")
            await sync_guild_users(guild)
            print(f"✅ User sync complete for guild: {guild.name}")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """
        New member joined the server.
        Add/update them in users table.
        """
        if member.bot:
            return

        sync_member_to_users_table(member)
        print(f"👋 Member joined and synced: {member.display_name} ({member.id})")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        """
        Member left the server.
        Mark them MIA.
        """
        if member.bot:
            return

        from database import upsert_user

        rank = get_highest_rank_from_member(member)

        upsert_user(
            discord_id=str(member.id),
            discord_username=str(member.name),
            display_name=str(member.display_name),
            rank=rank,
            status="MIA",
        )

        print(f"🚪 Member left, marked MIA: {member.display_name} ({member.id})")

    @commands.Cog.listener()
    async def on_member_update(
        self,
        before: discord.Member,
        after: discord.Member,
    ):
        """
        Watches for rank role changes.

        If the user's highest rank role changes, update the users table.
        """
        if after.bot:
            return

        before_rank = get_highest_rank_from_member(before)
        after_rank = get_highest_rank_from_member(after)

        if before_rank != after_rank:
            sync_member_to_users_table(after)

            print(
                f"🎖️ Rank updated: {after.display_name} "
                f"{before_rank} -> {after_rank}"
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(Watchdog(bot))