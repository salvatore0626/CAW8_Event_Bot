from __future__ import annotations

import traceback

import discord
from discord.ext import commands, tasks

try:
    from config import GUILD_ID
except ImportError:
    GUILD_ID = 0

from services.op_execution_reminder_service import (
    GLOBAL_COMPLETE_REMINDER_SECONDS,
    OPENER_REMINDER_SECONDS,
    due_open_events_for_global_reminder,
    due_scheduled_events_for_cancel_reminder,
    forget_op_opened,
    get_event,
    is_within_notification_window,
    opened_op_contexts,
    operation_reminder_setting,
    operation_reminder_subscribers,
)
from services.permission_service import member_can_receive_operation_reminders


class OpExecutionReminderCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.sent_opener_reminders: set[int] = set()
        self.sent_global_complete: set[tuple[int, int]] = set()
        self.sent_global_cancel: set[tuple[int, int]] = set()

    async def cog_load(self):
        self.reminder_loop.start()

    async def cog_unload(self):
        self.reminder_loop.cancel()

    @tasks.loop(seconds=60)
    async def reminder_loop(self):
        try:
            await self.process_reminders()
        except Exception:
            traceback.print_exc()

    @reminder_loop.before_loop
    async def before_reminder_loop(self):
        await self.bot.wait_until_ready()

    def configured_guild(self) -> discord.Guild | None:
        try:
            guild_id = int(GUILD_ID or 0)
        except (TypeError, ValueError):
            guild_id = 0

        if guild_id:
            guild = self.bot.get_guild(guild_id)
            if guild is not None:
                return guild

        return self.bot.guilds[0] if self.bot.guilds else None

    async def get_member(self, guild: discord.Guild, user_id: int) -> discord.Member | None:
        member = guild.get_member(int(user_id))
        if member is not None:
            return member

        try:
            return await guild.fetch_member(int(user_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    async def process_reminders(self) -> None:
        guild = self.configured_guild()
        if guild is None:
            return

        await self.process_30_minute_opener_reminders(guild)
        await self.process_4_hour_complete_reminders(guild)
        await self.process_4_hour_cancel_reminders(guild)

    async def process_30_minute_opener_reminders(self, guild: discord.Guild) -> None:
        import time

        current_ts = int(time.time())

        for context in opened_op_contexts():
            event = get_event(context.event_id)

            if event is None or event.status != "Open":
                forget_op_opened(context.event_id)
                self.sent_opener_reminders.discard(context.event_id)
                continue

            if current_ts < context.opened_at + OPENER_REMINDER_SECONDS:
                continue

            if context.event_id in self.sent_opener_reminders:
                continue

            subscriber = operation_reminder_setting(str(context.opener_id))
            if subscriber is None or not is_within_notification_window(subscriber):
                continue

            member = await self.get_member(guild, context.opener_id)
            if member is None or not member_can_receive_operation_reminders(member):
                continue

            # Try only once after the user's notification window permits it.
            self.sent_opener_reminders.add(context.event_id)
            try:
                await member.send(
                    f"Operation {event.op_name} #{event.event_id} is still opened. "
                    "please use /complete to avoid attends from users who didn't attend."
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

    async def process_4_hour_complete_reminders(self, guild: discord.Guild) -> None:
        events = due_open_events_for_global_reminder()
        if not events:
            return

        subscribers = operation_reminder_subscribers()

        for event in events:
            for subscriber in subscribers:
                try:
                    user_id = int(subscriber.discord_id)
                except (TypeError, ValueError):
                    continue

                key = (event.event_id, user_id)
                if key in self.sent_global_complete:
                    continue

                if not is_within_notification_window(subscriber):
                    continue

                member = await self.get_member(guild, user_id)
                if member is None or not member_can_receive_operation_reminders(member):
                    continue

                self.sent_global_complete.add(key)
                try:
                    await member.send(
                        f"Operation {event.op_name} #{event.event_id} is still opened. "
                        "please use /complete to avoid attends from users who didn't attend."
                    )
                except (discord.Forbidden, discord.HTTPException):
                    pass

    async def process_4_hour_cancel_reminders(self, guild: discord.Guild) -> None:
        events = due_scheduled_events_for_cancel_reminder()
        if not events:
            return

        subscribers = operation_reminder_subscribers()

        for event in events:
            for subscriber in subscribers:
                try:
                    user_id = int(subscriber.discord_id)
                except (TypeError, ValueError):
                    continue

                key = (event.event_id, user_id)
                if key in self.sent_global_cancel:
                    continue

                if not is_within_notification_window(subscriber):
                    continue

                member = await self.get_member(guild, user_id)
                if member is None or not member_can_receive_operation_reminders(member):
                    continue

                self.sent_global_cancel.add(key)
                try:
                    await member.send(
                        f"Operation {event.op_name} #{event.event_id} has not been started, opened, or completed. "
                        "Please confirm that the op does not intend to start late and use /scheduleview to cancel the op."
                    )
                except (discord.Forbidden, discord.HTTPException):
                    pass


async def setup(bot: commands.Bot):
    await bot.add_cog(OpExecutionReminderCog(bot))
