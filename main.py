import asyncio

import discord
from discord.ext import commands

from config import DISCORD_TOKEN, GUILD_ID
from database import init_db

class AirBossBot(commands.Bot):
    async def setup_hook(self):
        await self.load_extension("watchdog")
        await self.load_extension("cogs.user_settings")
        await self.load_extension("cogs.get_qualified")
        await self.load_extension("cogs.requests")
        await self.load_extension("cogs.paperwork")
        await self.load_extension("cogs.qualification_record")
        await self.load_extension("cogs.qual_export")
        await self.load_extension("cogs.scheduleop")
        await self.load_extension("cogs.scheduleview")
        await self.load_extension("cogs.flightlead")
        await self.load_extension("cogs.flightlead_reminders")
        await self.load_extension("cogs.op_lifecycle")
        await self.load_extension("cogs.attend")
        await self.load_extension("cogs.recordedit")
        await self.load_extension("cogs.promotions")
        await self.load_extension("cogs.op_templates")
        await self.load_extension("cogs.rewards")
        await self.load_extension("cogs.leaderboard")
        await self.load_extension("cogs.greenie")
        await self.load_extension("cogs.lookup")
        await self.load_extension("cogs.training")
        await self.load_extension("cogs.ew_quiz")
        await self.load_extension("cogs.asvab")
        await self.load_extension("cogs.after_action")

        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            print(f"✅ Slash commands synced to guild {GUILD_ID}")
        else:
            await self.tree.sync()
            print("✅ Slash commands synced globally")

async def main():
    if not DISCORD_TOKEN:
        raise RuntimeError("Missing DISCORD_TOKEN in .env")

    init_db()

    intents = discord.Intents.default()
    intents.guilds = True
    intents.members = True
    intents.presences = True

    bot = AirBossBot(
        command_prefix="!",
        intents=intents,
        member_cache_flags=discord.MemberCacheFlags.all(),
    )

    await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())