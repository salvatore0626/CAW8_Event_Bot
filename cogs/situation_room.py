from __future__ import annotations

from discord.ext import commands, tasks

from services.situation_room_service import (
    is_tracked_situation_room_message,
    queue_situation_room_refresh,
    queue_situation_room_refresh_now,
    queue_situation_room_startup_rebuild,
)


class SituationRoomCog(commands.Cog):
    """Keeps persistent situation-room boards synchronized after bot startup."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._initial_refresh_started = False
        self.periodic_refresh.start()

    def cog_unload(self):
        self.periodic_refresh.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        # on_ready can fire again after reconnects. One startup reconciliation is
        # enough because normal command changes use the shared debounced queue.
        if self._initial_refresh_started:
            return

        self._initial_refresh_started = True
        queue_situation_room_startup_rebuild(self.bot)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload):
        """Restore a manually deleted tracked Situation Room board."""
        if payload.guild_id is None:
            return

        if not is_tracked_situation_room_message(
            channel_id=payload.channel_id,
            message_id=payload.message_id,
        ):
            return

        queue_situation_room_refresh(
            self.bot,
            reason=f"situation room board deleted:{payload.message_id}",
        )

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload):
        """Restore tracked boards if somebody purges messages in the Situation Room."""
        if payload.guild_id is None:
            return

        deleted_board_ids = [
            message_id
            for message_id in payload.message_ids
            if is_tracked_situation_room_message(
                channel_id=payload.channel_id,
                message_id=message_id,
            )
        ]

        if not deleted_board_ids:
            return

        queue_situation_room_refresh(
            self.bot,
            reason="situation room board bulk delete",
        )

    @tasks.loop(minutes=15)
    async def periodic_refresh(self):
        queue_situation_room_refresh_now(self.bot)

    @periodic_refresh.before_loop
    async def before_periodic_refresh(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(SituationRoomCog(bot))
