from __future__ import annotations

import asyncio
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands, tasks

from services.permission_service import (
    member_can_receive_weekly_report,
    require_admin_command,
)
from services.weekly_report_service import (
    configured_report_day,
    configured_timezone,
    current_boundary_key,
    ensure_cached_report,
    format_report_message,
    generate_and_cache_weekly_report,
    load_cached_snapshot,
    load_cached_previous_snapshot,
    load_report_state,
    report_assets,
    save_report_state,
    send_weekly_report_dm,
    weekly_report_subscribers,
)


class WeeklyReportCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._last_generated_local_date: str | None = None
        self.report_scheduler.start()

    def cog_unload(self):
        self.report_scheduler.cancel()

    async def cog_load(self):
        # Generate the cached report/images at boot so /server report and a newly
        # enabled subscriber always have something ready to send.
        try:
            await asyncio.to_thread(generate_and_cache_weekly_report)
            self._last_generated_local_date = datetime.now(configured_timezone()).date().isoformat()
            print("✅ Weekly report images generated on startup.")
        except Exception as error:
            print(
                "⚠️ Weekly report image generation failed: "
                f"{type(error).__name__}: {error}"
            )

    async def _resolve_report_member(self, discord_id: int) -> discord.Member | None:
        # A Discord User object does not contain roles, so scheduled delivery
        # resolves a guild Member before the Admin/Instructor eligibility check.
        for guild in self.bot.guilds:
            member = guild.get_member(discord_id)
            if member is not None:
                return member

            try:
                member = await guild.fetch_member(discord_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                continue

            if member is not None:
                return member

        return None

    @tasks.loop(minutes=1)
    async def report_scheduler(self):
        tz = configured_timezone()
        now_local = datetime.now(tz)
        today_key = now_local.date().isoformat()

        # Rebuild once at/after midnight every day. On the configured report day
        # this generation happens first, and only then can the scheduled DM send.
        if now_local.hour == 0 and self._last_generated_local_date != today_key:
            try:
                await asyncio.to_thread(generate_and_cache_weekly_report, now_local)
                self._last_generated_local_date = today_key
                print("✅ Weekly report images regenerated at midnight.")
            except Exception as error:
                print(
                    "⚠️ Weekly report midnight generation failed: "
                    f"{type(error).__name__}: {error}"
                )
                # Never send a report-day DM with stale images if the midnight
                # refresh failed. The scheduler can retry on the next minute.
                return

        # Scheduled delivery is intentionally restricted to the midnight hour of
        # WEEKLY_REPORT_DAY. User notify_start/notify_end windows are not used.
        if now_local.hour != 0:
            return

        if now_local.isoweekday() != configured_report_day():
            return

        state = load_report_state()
        boundary_key = current_boundary_key(now_local)
        if state.get("last_sent_boundary") == boundary_key:
            return

        # If the cog booted during the midnight hour, cog_load may already have
        # generated today's images. Otherwise ensure the fresh boundary report is
        # available before sending.
        if self._last_generated_local_date != today_key:
            try:
                await asyncio.to_thread(generate_and_cache_weekly_report, now_local)
                self._last_generated_local_date = today_key
            except Exception as error:
                print(
                    "⚠️ Weekly report send generation failed: "
                    f"{type(error).__name__}: {error}"
                )
                return

        sent = 0
        failed = 0
        skipped_ineligible = 0

        for discord_id in weekly_report_subscribers():
            try:
                numeric_id = int(discord_id)
            except (TypeError, ValueError):
                failed += 1
                continue

            member = await self._resolve_report_member(numeric_id)
            if member is None:
                failed += 1
                continue

            if not member_can_receive_weekly_report(member):
                skipped_ineligible += 1
                continue

            if await send_weekly_report_dm(member, regenerate_if_missing=False):
                sent += 1
            else:
                failed += 1

        state["last_sent_boundary"] = boundary_key
        state["last_sent_at"] = int(now_local.timestamp())
        state["sent_count"] = sent
        state["failed_count"] = failed
        state["skipped_ineligible_count"] = skipped_ineligible
        save_report_state(state)
        print(
            f"✅ Weekly Server Report sent to {sent} user(s); "
            f"{failed} failed; {skipped_ineligible} skipped (not Admin/Instructor)."
        )

    @report_scheduler.before_loop
    async def before_report_scheduler(self):
        await self.bot.wait_until_ready()

    @app_commands.command(
        name="server-report",
        description="Post the current cached CAW 8 Weekly Server Report.",
    )
    @app_commands.guild_only()
    async def server_report(self, interaction: discord.Interaction):
        if not await require_admin_command(interaction):
            return

        await interaction.response.defer(ephemeral=False, thinking=True)

        try:
            snapshot, assets = await asyncio.to_thread(ensure_cached_report)
            files: list[discord.File] = []
            if assets.server_activity_path.exists():
                files.append(
                    discord.File(
                        assets.server_activity_path,
                        filename="server_activity.png",
                    )
                )
            if assets.operation_trends_path.exists():
                files.append(
                    discord.File(
                        assets.operation_trends_path,
                        filename="operation_trends.png",
                    )
                )
            if assets.qualification_activity_path.exists():
                files.append(
                    discord.File(
                        assets.qualification_activity_path,
                        filename="qualification_activity.png",
                    )
                )

            previous_snapshot = await asyncio.to_thread(load_cached_previous_snapshot)
            await interaction.followup.send(
                content=format_report_message(snapshot, previous_snapshot),
                files=files,
            )
        except Exception as error:
            await interaction.followup.send(
                "Could not load the Weekly Server Report: "
                f"`{type(error).__name__}: {error}`",
                ephemeral=True,
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(WeeklyReportCog(bot))
