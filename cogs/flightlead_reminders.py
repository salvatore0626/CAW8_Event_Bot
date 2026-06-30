from __future__ import annotations

import traceback
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord.ext import commands, tasks

try:
    from config import (
        FLIGHTLEAD_REMINDER_LOOP_SECONDS,
        FLIGHTLEAD_REMINDER_MINUTES_BEFORE,
    )
except ImportError:
    FLIGHTLEAD_REMINDER_LOOP_SECONDS = 60
    FLIGHTLEAD_REMINDER_MINUTES_BEFORE = 60

from services.flightlead_reminder_service import (
    FlightLeadReminderCandidate,
    due_flightlead_reminders,
)


def parse_notification_time(value: str | None, default: str) -> int:
    text = str(value or default).strip()

    try:
        hour_text, minute_text = text.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except (TypeError, ValueError):
        hour_text, minute_text = default.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)

    hour = max(0, min(23, hour))
    minute = max(0, min(59, minute))

    return hour * 60 + minute


def is_within_notification_window(
    *,
    timezone: str | None,
    notify_start: str | None,
    notify_end: str | None,
) -> bool:
    if not timezone:
        return False

    try:
        local_now = datetime.now(ZoneInfo(str(timezone)))
    except ZoneInfoNotFoundError:
        return False
    except Exception:
        return False

    now_minutes = local_now.hour * 60 + local_now.minute
    start_minutes = parse_notification_time(notify_start, "09:00")
    end_minutes = parse_notification_time(notify_end, "21:00")

    if start_minutes == end_minutes:
        return True

    if start_minutes < end_minutes:
        return start_minutes <= now_minutes < end_minutes

    # Overnight window, example 21:00 -> 09:00.
    return now_minutes >= start_minutes or now_minutes < end_minutes


def format_time_until_event(candidate: FlightLeadReminderCandidate) -> str:
    return f"<t:{candidate.scheduled_at}:R>"


def format_event_time(candidate: FlightLeadReminderCandidate) -> str:
    return f"<t:{candidate.scheduled_at}:F>"


def flight_line(candidate: FlightLeadReminderCandidate) -> str:
    letter = candidate.flight_letter or str(candidate.slot_index)
    name = candidate.flight_name or candidate.slot_label
    aircraft = candidate.aircraft or "Unknown aircraft"

    if candidate.aircraft_count:
        aircraft = f"{candidate.aircraft_count}x {aircraft}"

    return f"{letter} | {name} — {aircraft}"


def build_reminder_embed(candidate: FlightLeadReminderCandidate, guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title="Flight Lead Reminder",
        description=(
            f"You have a flight lead reservation for **{candidate.op_name}** in **{guild.name}**.\n\n"
            f"The op starts {format_time_until_event(candidate)}."
        ),
    )

    embed.add_field(
        name="Event Time",
        value=format_event_time(candidate),
        inline=False,
    )

    embed.add_field(
        name="Reserved Flight",
        value=flight_line(candidate),
        inline=False,
    )

    embed.add_field(
        name="Event ID",
        value=str(candidate.event_id),
        inline=True,
    )

    embed.set_footer(
        text="You were sent this because Flight Lead Notifications are enabled in /user settings."
    )

    return embed


def member_is_in_voice(member: discord.Member) -> bool:
    try:
        return member.voice is not None and member.voice.channel is not None
    except Exception:
        return False


class FlightLeadReminderCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.tried_reminders: set[tuple[int, int, str]] = set()

    async def cog_load(self):
        self.flightlead_reminder_loop.change_interval(
            seconds=max(30, int(FLIGHTLEAD_REMINDER_LOOP_SECONDS))
        )
        self.flightlead_reminder_loop.start()

    async def cog_unload(self):
        self.flightlead_reminder_loop.cancel()

    @tasks.loop(seconds=60)
    async def flightlead_reminder_loop(self):
        try:
            await self.process_due_reminders()
        except Exception:
            traceback.print_exc()

    @flightlead_reminder_loop.before_loop
    async def before_flightlead_reminder_loop(self):
        await self.bot.wait_until_ready()

    async def process_due_reminders(self) -> None:
        candidates = due_flightlead_reminders(
            minutes_before=int(FLIGHTLEAD_REMINDER_MINUTES_BEFORE),
            window_seconds=max(30, int(FLIGHTLEAD_REMINDER_LOOP_SECONDS)),
        )

        for candidate in candidates:
            key = self.reminder_key(candidate)

            if key in self.tried_reminders:
                continue

            # Try once this bot runtime and call it a day.
            self.tried_reminders.add(key)
            await self.process_candidate(candidate)

    @staticmethod
    def reminder_key(candidate: FlightLeadReminderCandidate) -> tuple[int, int, str]:
        return (
            int(candidate.event_id),
            int(candidate.reservation_id),
            str(candidate.reserved_by),
        )

    async def process_candidate(self, candidate: FlightLeadReminderCandidate) -> None:
        guild = self.guild_for_candidate(candidate)

        if guild is None:
            return

        member = self.member_for_candidate(guild, candidate)

        if member is None:
            return

        if member.bot:
            return

        # First check VC presence. If they are already in a server VC, do not DM.
        if member_is_in_voice(member):
            return

        # Then check opt-in and notification window.
        if not candidate.notify_flightlead:
            return

        if not is_within_notification_window(
            timezone=candidate.timezone,
            notify_start=candidate.notify_start,
            notify_end=candidate.notify_end,
        ):
            return

        try:
            await member.send(embed=build_reminder_embed(candidate, guild))
        except (discord.Forbidden, discord.HTTPException):
            return
        except Exception:
            return

    def guild_for_candidate(self, candidate: FlightLeadReminderCandidate) -> discord.Guild | None:
        # This bot is usually single-guild. Use the first guild that contains the member.
        for guild in self.bot.guilds:
            member = self.member_for_candidate(guild, candidate)
            if member is not None:
                return guild

        return self.bot.guilds[0] if self.bot.guilds else None

    @staticmethod
    def member_for_candidate(
        guild: discord.Guild,
        candidate: FlightLeadReminderCandidate,
    ) -> discord.Member | None:
        try:
            return guild.get_member(int(candidate.reserved_by))
        except (TypeError, ValueError):
            return None


async def setup(bot: commands.Bot):
    await bot.add_cog(FlightLeadReminderCog(bot))
