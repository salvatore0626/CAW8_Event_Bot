from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands
from services.permission_service import (
    require_admin_command,
    member_is_admin,
)

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

try:
    from config import OP_EVENT_MEETING_VCS
except ImportError:
    OP_EVENT_MEETING_VCS = []

try:
    from config import SCHEDULE_EVENT_DURATION_HOURS
except ImportError:
    SCHEDULE_EVENT_DURATION_HOURS = 3

from services.schedule_service import (
    OpTemplateSummary,
    build_day_choices,
    create_op_event,
    custom_datetime_timestamp,
    format_timestamp_local,
    get_next_default_slot_timestamps,
    get_op_templates,
    get_user_timezone,
    new_schedule_series_id,
    now_ts,
    resolve_op_template,
)


@dataclass
class CustomScheduleDraft:
    template: OpTemplateSummary
    timezone_name: str
    selected_channel_id: int
    day_iso: str | None = None
    hour: int = 17
    minute: int = 0

    def selected_timestamp(self) -> int | None:
        if not self.day_iso:
            return None

        return custom_datetime_timestamp(
            day_iso=self.day_iso,
            hour_value=self.hour,
            minute_value=self.minute,
            timezone_name=self.timezone_name,
        )


def configured_role_ids() -> set[int]:
    role_ids: set[int] = set()

    for value in [MISSION_EXECUTER_ROLE, STAFF_ROLE]:
        try:
            if value:
                role_ids.add(int(value))
        except Exception:
            pass

    for value in MISSION_EXECUTER_ROLES or []:
        try:
            if value:
                role_ids.add(int(value))
        except Exception:
            pass

    return role_ids


def has_schedule_permission(member: discord.Member) -> bool:
    if member_is_admin(member):
        return True
    role_ids = configured_role_ids()

    # If no role config is present yet, do not block testing.
    if not role_ids:
        return True

    return any(role.id in role_ids for role in member.roles)


def configured_meeting_vc_ids() -> list[int]:
    channel_ids: list[int] = []
    seen_channel_ids: set[int] = set()

    for raw_channel_id in OP_EVENT_MEETING_VCS or []:
        try:
            channel_id = int(raw_channel_id or 0)
        except (TypeError, ValueError):
            continue

        if channel_id <= 0 or channel_id in seen_channel_ids:
            continue

        seen_channel_ids.add(channel_id)
        channel_ids.append(channel_id)

    return channel_ids


def channel_name_for_select(
    guild: discord.Guild | None,
    channel_id: int,
) -> str:
    if guild is None:
        return "Unknown channel"

    channel = guild.get_channel(int(channel_id))

    if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
        return channel.name[:100]

    if channel is not None:
        return f"{channel.name[:90]} (not voice)"

    return "Unknown channel"


def configured_vc_options(
    guild: discord.Guild | None,
) -> list[tuple[str, int]]:
    options: list[tuple[str, int]] = []

    for channel_id in configured_meeting_vc_ids():
        options.append((channel_name_for_select(guild, channel_id), channel_id))

    return options


def default_channel_id_for_template(template: OpTemplateSummary | None = None) -> int:
    channel_ids = configured_meeting_vc_ids()

    if channel_ids:
        return channel_ids[0]

    return 0


async def get_event_channel_by_id(
    guild: discord.Guild,
    channel_id: int,
) -> discord.abc.GuildChannel:
    if not channel_id:
        raise ValueError(
            "No meet VC is selected. Set OP_EVENT_MEETING_VCS in config.py."
        )

    channel = guild.get_channel(int(channel_id))

    if channel is None:
        channel = await guild.fetch_channel(int(channel_id))

    if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
        raise ValueError(
            "The selected meet VC must be a voice or stage channel."
        )

    return channel


def scheduled_event_entity_type(channel: discord.abc.GuildChannel) -> discord.EntityType:
    if isinstance(channel, discord.StageChannel):
        return discord.EntityType.stage_instance

    return discord.EntityType.voice


async def create_discord_server_event(
    *,
    guild: discord.Guild,
    template: OpTemplateSummary,
    scheduled_at: int,
    selected_channel_id: int,
    created_by: discord.abc.User,
) -> discord.ScheduledEvent:
    channel = await get_event_channel_by_id(guild, selected_channel_id)

    start_time = datetime.fromtimestamp(int(scheduled_at), timezone.utc)
    end_time = start_time + timedelta(hours=float(SCHEDULE_EVENT_DURATION_HOURS or 3))

    description = template.description or f"{template.name} operation event."

    return await guild.create_scheduled_event(
        name=template.name[:100],
        description=description[:1000],
        start_time=start_time,
        end_time=end_time,
        privacy_level=discord.PrivacyLevel.guild_only,
        entity_type=scheduled_event_entity_type(channel),
        channel=channel,
        reason=f"Scheduled by {created_by}",
    )


async def delete_discord_server_event(
    guild: discord.Guild,
    server_event_id: str | None,
) -> None:
    if not server_event_id:
        return

    try:
        event = await guild.fetch_scheduled_event(int(server_event_id))
        await event.delete(reason="Cleaning up after schedule DB insert failure")
    except Exception:
        pass


def build_template_summary(template: OpTemplateSummary) -> str:
    return (
        f"**Op:** {template.name}\n"
        f"**Type:** {template.op_type}\n"
        f"**Players:** {template.total_players}\n"
        f"**Flights:** {template.flight_count}\n"
        f"**Description:** {template.description or 'None'}"
    )


def build_default_confirm_embed(
    template: OpTemplateSummary,
    timezone_name: str,
    selected_channel_id: int,
) -> discord.Embed:
    slot_rows = []

    for slot, timestamp in get_next_default_slot_timestamps():
        slot_rows.append(
            f"- **{slot.label}:** {format_timestamp_local(timestamp, timezone_name)}"
        )

    channel_text = f"<#{selected_channel_id}>" if selected_channel_id else "Not configured"

    embed = discord.Embed(
        title="Confirm Default Schedule",
        description=(
            f"{build_template_summary(template)}\n"
            f"**Discord Event Channel:** {channel_text}\n\n"
            "**This will create one scheduled event for each default slot:**\n"
            + "\n".join(slot_rows)
        ),
    )

    embed.set_footer(text=f"Displayed in your timezone: {timezone_name}")

    return embed


def build_custom_embed(draft: CustomScheduleDraft) -> discord.Embed:
    timestamp = draft.selected_timestamp()

    if timestamp:
        when_text = f"{format_timestamp_local(timestamp, draft.timezone_name)} / <t:{timestamp}:R>"
    else:
        when_text = "Select a day."

    channel_text = f"<#{draft.selected_channel_id}>" if draft.selected_channel_id else "Not configured"

    embed = discord.Embed(
        title="Custom Schedule",
        description=(
            f"{build_template_summary(draft.template)}\n"
            f"**Discord Event Channel:** {channel_text}\n\n"
            f"**Selected time:** {when_text}"
        ),
    )

    embed.set_footer(text=f"Displayed in your timezone: {draft.timezone_name}")

    return embed


async def op_template_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    templates = get_op_templates(limit=25, search_text=current)

    return [
        app_commands.Choice(
            name=f"{template.name} | {template.op_type} | {template.total_players} players"[:100],
            value=str(template.id),
        )
        for template in templates
    ]


def lock_schedule_view(view: discord.ui.View) -> None:
    for item in view.children:
        if hasattr(item, "disabled"):
            item.disabled = True


class DefaultScheduleConfirmView(discord.ui.View):
    def __init__(
        self,
        owner_id: int,
        template: OpTemplateSummary,
        timezone_name: str,
        selected_channel_id: int,
        guild: discord.Guild | None,
    ):
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.template = template
        self.timezone_name = timezone_name
        self.selected_channel_id = selected_channel_id
        self.guild = guild
        self.schedule_in_progress = False

        self.add_item(MeetVcSelect(selected_channel_id, row=0, guild=guild))
        self.add_item(ConfirmDefaultScheduleButton(row=1))
        self.add_item(CancelScheduleButton(row=1))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the person who opened this scheduler can use these controls.",
                ephemeral=True,
            )
            return False

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This can only be used inside the server.",
                ephemeral=True,
            )
            return False

        if not has_schedule_permission(interaction.user):
            await interaction.response.send_message(
                "You do not have permission to schedule ops.",
                ephemeral=True,
            )
            return False

        return True


class MeetVcSelect(discord.ui.Select):
    def __init__(
        self,
        selected_channel_id: int,
        row: int,
        guild: discord.Guild | None,
    ):
        options = []

        for index, (label, channel_id) in enumerate(configured_vc_options(guild)):
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=str(channel_id),
                    description="Default meeting VC" if index == 0 else "Meeting VC option",
                    default=int(selected_channel_id or 0) == int(channel_id),
                )
            )

        if not options:
            options = [
                discord.SelectOption(
                    label="No meeting VCs configured",
                    value="0",
                    description="Set OP_EVENT_MEETING_VCS in config.py",
                    default=True,
                )
            ]

        super().__init__(
            placeholder="Meet VC",
            min_values=1,
            max_values=1,
            options=options,
            row=row,
            disabled=options[0].value == "0",
        )

    async def callback(self, interaction: discord.Interaction):
        if isinstance(self.view, DefaultScheduleConfirmView):
            self.view.selected_channel_id = int(self.values[0])

            await interaction.response.edit_message(
                embed=build_default_confirm_embed(
                    self.view.template,
                    self.view.timezone_name,
                    self.view.selected_channel_id,
                ),
                view=DefaultScheduleConfirmView(
                    owner_id=self.view.owner_id,
                    template=self.view.template,
                    timezone_name=self.view.timezone_name,
                    selected_channel_id=self.view.selected_channel_id,
                    guild=interaction.guild,
                ),
            )
            return

        if isinstance(self.view, CustomScheduleView):
            self.view.draft.selected_channel_id = int(self.values[0])

            await self.view.refresh(interaction)
            return

        await interaction.response.send_message(
            "This VC selector is not attached to a scheduler view.",
            ephemeral=True,
        )


class ConfirmDefaultScheduleButton(discord.ui.Button):
    def __init__(self, row: int | None = None):
        super().__init__(
            label="Confirm",
            style=discord.ButtonStyle.success,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, DefaultScheduleConfirmView)

        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used inside a server.",
                ephemeral=True,
            )
            return

        if self.view.schedule_in_progress:
            await interaction.response.send_message(
                "This schedule request is already being created. Please wait.",
                ephemeral=True,
            )
            return

        self.view.schedule_in_progress = True
        lock_schedule_view(self.view)

        await interaction.response.edit_message(
            content="Creating scheduled events. Please wait...",
            view=self.view,
        )

        created_ids: list[int] = []
        created_discord_ids: list[str] = []
        series_id = new_schedule_series_id()

        try:
            for _slot, scheduled_at in get_next_default_slot_timestamps():
                server_event = await create_discord_server_event(
                    guild=interaction.guild,
                    template=self.view.template,
                    scheduled_at=scheduled_at,
                    selected_channel_id=self.view.selected_channel_id,
                    created_by=interaction.user,
                )

                created_discord_ids.append(str(server_event.id))

                event_id = create_op_event(
                    op_template_id=self.view.template.id,
                    scheduled_at=scheduled_at,
                    scheduled_by=str(interaction.user.id),
                    server_event_id=str(server_event.id),
                    schedule_series_id=series_id,
                )

                created_ids.append(event_id)
        except Exception as error:
            # Do not leave orphan Discord server events if this fails mid-series.
            for server_event_id in created_discord_ids:
                await delete_discord_server_event(interaction.guild, server_event_id)

            await interaction.edit_original_response(
                content=None,
                embed=discord.Embed(
                    title="Schedule Failed",
                    description=f"Failed to schedule op series: `{error}`",
                ),
                view=None,
            )
            return

        await interaction.edit_original_response(
            content=None,
            embed=discord.Embed(
                title="Op Series Scheduled",
                description=(
                    f"Created {len(created_ids)} scheduled event(s) for **{self.view.template.name}**.\n"
                    f"Event IDs: `{', '.join(str(event_id) for event_id in created_ids)}`"
                ),
            ),
            view=None,
        )


class CancelScheduleButton(discord.ui.Button):
    def __init__(self, row: int | None = None):
        super().__init__(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content="Scheduling cancelled.",
            embed=None,
            view=None,
        )


class CustomScheduleView(discord.ui.View):
    def __init__(
        self,
        owner_id: int,
        draft: CustomScheduleDraft,
        guild: discord.Guild | None,
    ):
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.draft = draft
        self.guild = guild
        self.schedule_in_progress = False

        self.add_item(MeetVcSelect(draft.selected_channel_id, row=0, guild=guild))
        self.add_item(CustomDaySelect(draft))
        self.add_item(CustomHourSelect(draft))
        self.add_item(CustomMinuteSelect(draft))
        self.add_item(ConfirmCustomScheduleButton())
        self.add_item(CancelScheduleButton(row=4))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the person who opened this scheduler can use these controls.",
                ephemeral=True,
            )
            return False

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This can only be used inside the server.",
                ephemeral=True,
            )
            return False

        if not has_schedule_permission(interaction.user):
            await interaction.response.send_message(
                "You do not have permission to schedule ops.",
                ephemeral=True,
            )
            return False

        return True

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=build_custom_embed(self.draft),
            view=CustomScheduleView(self.owner_id, self.draft, interaction.guild),
        )


class CustomDaySelect(discord.ui.Select):
    def __init__(self, draft: CustomScheduleDraft):
        self.draft = draft

        options = []

        for label, value in build_day_choices(draft.timezone_name, days=25):
            options.append(
                discord.SelectOption(
                    label=label,
                    value=value,
                    default=draft.day_iso == value,
                )
            )

        super().__init__(
            placeholder="Day",
            min_values=1,
            max_values=1,
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, CustomScheduleView)

        self.draft.day_iso = self.values[0]

        await self.view.refresh(interaction)


class CustomHourSelect(discord.ui.Select):
    def __init__(self, draft: CustomScheduleDraft):
        self.draft = draft

        options = [
            discord.SelectOption(
                label=str(hour),
                value=str(hour),
                default=draft.hour == hour,
            )
            for hour in range(1, 25)
        ]

        super().__init__(
            placeholder="Hour 1-24",
            min_values=1,
            max_values=1,
            options=options,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, CustomScheduleView)

        self.draft.hour = int(self.values[0])

        await self.view.refresh(interaction)


class CustomMinuteSelect(discord.ui.Select):
    def __init__(self, draft: CustomScheduleDraft):
        self.draft = draft

        options = [
            discord.SelectOption(
                label=f"{minute:02d}",
                value=str(minute),
                default=draft.minute == minute,
            )
            for minute in [0, 15, 30, 45]
        ]

        super().__init__(
            placeholder="Minute",
            min_values=1,
            max_values=1,
            options=options,
            row=3,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, CustomScheduleView)

        self.draft.minute = int(self.values[0])

        await self.view.refresh(interaction)


class ConfirmCustomScheduleButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Confirm",
            style=discord.ButtonStyle.success,
            row=4,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, CustomScheduleView)

        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used inside a server.",
                ephemeral=True,
            )
            return

        if self.view.schedule_in_progress:
            await interaction.response.send_message(
                "This schedule request is already being created. Please wait.",
                ephemeral=True,
            )
            return

        timestamp = self.view.draft.selected_timestamp()

        if timestamp is None:
            await interaction.response.send_message(
                "Select a day first.",
                ephemeral=True,
            )
            return

        if timestamp <= now_ts():
            await interaction.response.send_message(
                "That time is in the past. Pick a future time.",
                ephemeral=True,
            )
            return

        self.view.schedule_in_progress = True
        lock_schedule_view(self.view)

        await interaction.response.edit_message(
            content="Creating scheduled event. Please wait...",
            view=self.view,
        )

        server_event_id = None

        try:
            server_event = await create_discord_server_event(
                guild=interaction.guild,
                template=self.view.draft.template,
                scheduled_at=timestamp,
                selected_channel_id=self.view.draft.selected_channel_id,
                created_by=interaction.user,
            )

            server_event_id = str(server_event.id)

            event_id = create_op_event(
                op_template_id=self.view.draft.template.id,
                scheduled_at=timestamp,
                scheduled_by=str(interaction.user.id),
                server_event_id=server_event_id,
                schedule_series_id=None,
            )
        except Exception as error:
            await delete_discord_server_event(interaction.guild, server_event_id)

            await interaction.edit_original_response(
                content=None,
                embed=discord.Embed(
                    title="Schedule Failed",
                    description=f"Failed to schedule op: `{error}`",
                ),
                view=None,
            )
            return

        await interaction.edit_original_response(
            content=None,
            embed=discord.Embed(
                title="Op Scheduled",
                description=(
                    f"Created scheduled event `{event_id}` for **{self.view.draft.template.name}**.\n"
                    f"Time: {format_timestamp_local(timestamp, self.view.draft.timezone_name)}"
                ),
            ),
            view=None,
        )


class ScheduleOpCommands(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="scheduleop",
        description="Schedule an op template into default slots or a custom time.",
    )
    @app_commands.describe(
        opname="Op template name",
        custom="True = choose one custom day/time. False = use default slots.",
    )
    @app_commands.autocomplete(opname=op_template_autocomplete)
    @app_commands.guild_only()
    async def scheduleop_command(
        self,
        interaction: discord.Interaction,
        opname: str,
        custom: bool = False,
    ):
        if not await require_admin_command(interaction):
            return
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This command can only be used inside the server.",
                ephemeral=True,
            )
            return

        if not has_schedule_permission(interaction.user):
            await interaction.response.send_message(
                "You do not have permission to schedule ops.",
                ephemeral=True,
            )
            return

        template = resolve_op_template(opname)

        if template is None:
            await interaction.response.send_message(
                "I could not find that op template. Use the autocomplete list.",
                ephemeral=True,
            )
            return

        timezone_name = get_user_timezone(str(interaction.user.id))

        if custom:
            draft = CustomScheduleDraft(
                template=template,
                timezone_name=timezone_name,
                selected_channel_id=default_channel_id_for_template(template),
            )

            await interaction.response.send_message(
                embed=build_custom_embed(draft),
                view=CustomScheduleView(interaction.user.id, draft, interaction.guild),
                ephemeral=True,
            )
            return

        selected_channel_id = default_channel_id_for_template(template)

        await interaction.response.send_message(
            embed=build_default_confirm_embed(template, timezone_name, selected_channel_id),
            view=DefaultScheduleConfirmView(
                owner_id=interaction.user.id,
                template=template,
                timezone_name=timezone_name,
                selected_channel_id=selected_channel_id,
                guild=interaction.guild,
            ),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(ScheduleOpCommands(bot))
