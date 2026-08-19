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
            await sync_guild_users(
                guild,
                performed_by_id=(self.bot.user.id if self.bot.user else None),
            )
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

        from database import deny_qual_requests_for_departed_user, upsert_user

        rank = get_highest_rank_from_member(member)

        upsert_user(
            discord_id=str(member.id),
            discord_username=str(member.name),
            display_name=str(member.display_name),
            rank=rank,
            status="MIA",
        )

        denied_quals = deny_qual_requests_for_departed_user(
            str(member.id),
            performed_by_id=(self.bot.user.id if self.bot.user else None),
        )

        print(
            f"🚪 Member left, marked MIA: {member.display_name} ({member.id})"
            f" | qualification requests denied: {denied_quals}"
        )

    @commands.Cog.listener()
    async def on_member_update(
        self,
        before: discord.Member,
        after: discord.Member,
    ):
        """
        Watches for member changes that should refresh users table.

        This keeps Discord display names current between bot restarts and also
        preserves the existing rank-role sync behavior.
        """
        if after.bot:
            return

        before_rank = get_highest_rank_from_member(before)
        after_rank = get_highest_rank_from_member(after)

        rank_changed = before_rank != after_rank
        display_name_changed = before.display_name != after.display_name
        username_changed = before.name != after.name
        global_name_changed = (
            getattr(before, "global_name", None)
            != getattr(after, "global_name", None)
        )

        if not (
            rank_changed
            or display_name_changed
            or username_changed
            or global_name_changed
        ):
            return

        sync_member_to_users_table(after)

        if rank_changed:
            print(
                f"🎖️ Rank updated: {after.display_name} "
                f"{before_rank} -> {after_rank}"
            )

        if display_name_changed or username_changed or global_name_changed:
            print(
                f"🪪 Member name updated: "
                f"{before.display_name} -> {after.display_name} ({after.id})"
            )

    @commands.Cog.listener()
    async def on_user_update(
        self,
        before: discord.User,
        after: discord.User,
    ):
        """
        Watches for account-level username/global name changes.

        Nickname changes usually arrive through on_member_update. Username and
        global display-name changes can arrive through on_user_update, so we
        refresh any cached guild member record for that user.
        """
        if after.bot:
            return

        username_changed = before.name != after.name
        global_name_changed = (
            getattr(before, "global_name", None)
            != getattr(after, "global_name", None)
        )

        if not (username_changed or global_name_changed):
            return

        synced = False

        for guild in self.bot.guilds:
            member = guild.get_member(after.id)

            if member is None:
                continue

            sync_member_to_users_table(member)
            synced = True

        if synced:
            print(
                f"🪪 User account name updated: "
                f"{before.name} -> {after.name} ({after.id})"
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(Watchdog(bot))