from __future__ import annotations

import asyncio
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
    from config import CHANNEL_ID_NORMAL
except ImportError:
    CHANNEL_ID_NORMAL = 0

try:
    from config import CHANNEL_ID_TOURNAMENT
except ImportError:
    CHANNEL_ID_TOURNAMENT = 0

try:
    from config import CHANNEL_ID_MINI
except ImportError:
    CHANNEL_ID_MINI = 0

try:
    from config import CHANNEL_ID_ARCADE
except ImportError:
    CHANNEL_ID_ARCADE = 0

try:
    from config import SCHEDULE_EVENT_DURATION_HOURS
except ImportError:
    SCHEDULE_EVENT_DURATION_HOURS = 3

from services.schedule_service import (
    OpEventRecord,
    cancel_op_event,
    format_timestamp_local,
    format_timestamp_short,
    get_reservations_for_event,
    get_scheduled_op_events,
    get_user_timezone,
    is_default_slot_timestamp,
    now_ts,
    set_event_server_event_id,
    uncancel_op_event,
)


from services.private_view_service import (
    PrivateTimeoutView,
    bind_private_view,
    bind_view_to_original_response,
)

ACTIVE_STATUSES = {"Scheduled", "Briefing", "Open"}

ANSI_RESET = "\u001b[0m"
ANSI_BOLD = "\u001b[1m"
ANSI_GREEN = "\u001b[32m"
ANSI_RED = "\u001b[31m"
ANSI_BLUE = "\u001b[34m"
ANSI_CYAN = "\u001b[36m"
ANSI_YELLOW = "\u001b[33m"
ANSI_PURPLE = "\u001b[35m"
ANSI_WHITE = "\u001b[37m"


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

    if not role_ids:
        return True

    return any(role.id in role_ids for role in member.roles)


def selected_event(events: list[OpEventRecord], index: int) -> OpEventRecord | None:
    if not events:
        return None

    return events[index % len(events)]


async def cancel_discord_server_event(
    guild: discord.Guild | None,
    server_event_id: str | None,
) -> None:
    if guild is None or not server_event_id:
        return

    try:
        event = await guild.fetch_scheduled_event(int(server_event_id))

        event_status = getattr(discord, "EventStatus", None)
        canceled_status = None

        if event_status is not None:
            canceled_status = (
                getattr(event_status, "canceled", None)
                or getattr(event_status, "cancelled", None)
            )

        if canceled_status is not None:
            await event.edit(
                status=canceled_status,
                reason="Scheduled op canceled",
            )
        else:
            await event.delete(reason="Scheduled op canceled")
    except Exception as error:
        print(f"⚠️ Failed to cancel Discord scheduled event {server_event_id}: {error}")


def queue_discord_server_event_cancel(
    guild: discord.Guild | None,
    server_event_id: str | None,
) -> None:
    if guild is None or not server_event_id:
        return

    asyncio.create_task(
        cancel_discord_server_event(
            guild,
            server_event_id,
        )
    )


def channel_id_for_op_type(op_type: str) -> int:
    normalized = str(op_type or "").strip().lower()

    if normalized == "tournament":
        return int(CHANNEL_ID_TOURNAMENT or CHANNEL_ID_NORMAL or 0)

    if normalized in {"mini", "mini op", "mini-op"}:
        return int(CHANNEL_ID_MINI or CHANNEL_ID_NORMAL or 0)

    if normalized == "arcade":
        return int(CHANNEL_ID_ARCADE or CHANNEL_ID_NORMAL or 0)

    return int(CHANNEL_ID_NORMAL or 0)


async def get_event_channel_by_type(
    guild: discord.Guild,
    op_type: str,
) -> discord.abc.GuildChannel:
    channel_id = channel_id_for_op_type(op_type)

    if not channel_id:
        raise ValueError(
            "No event channel is configured for this op type. "
            "Set CHANNEL_ID_NORMAL / CHANNEL_ID_TOURNAMENT / CHANNEL_ID_MINI / CHANNEL_ID_ARCADE in config.py."
        )

    channel = guild.get_channel(int(channel_id))

    if channel is None:
        channel = await guild.fetch_channel(int(channel_id))

    if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
        raise ValueError("The configured event channel must be a voice or stage channel.")

    return channel


def scheduled_event_entity_type(channel: discord.abc.GuildChannel) -> discord.EntityType:
    if isinstance(channel, discord.StageChannel):
        return discord.EntityType.stage_instance

    return discord.EntityType.voice


async def recreate_discord_server_event(
    *,
    guild: discord.Guild,
    event: OpEventRecord,
    created_by: discord.abc.User,
) -> discord.ScheduledEvent:
    channel = await get_event_channel_by_type(guild, event.op_type)

    start_time = datetime.fromtimestamp(int(event.scheduled_at), timezone.utc)
    end_time = start_time + timedelta(hours=float(SCHEDULE_EVENT_DURATION_HOURS or 3))

    description = f"{event.op_name} operation event."

    return await guild.create_scheduled_event(
        name=event.op_name[:100],
        description=description[:1000],
        start_time=start_time,
        end_time=end_time,
        privacy_level=discord.PrivacyLevel.guild_only,
        entity_type=scheduled_event_entity_type(channel),
        channel=channel,
        reason=f"Uncancelled / recreated by {created_by}",
    )


async def delete_discord_server_event_silent(
    guild: discord.Guild | None,
    server_event_id: str | None,
) -> None:
    if guild is None or not server_event_id:
        return

    try:
        event = await guild.fetch_scheduled_event(int(server_event_id))
        await event.delete(reason="Cleaning up after uncancel DB failure")
    except Exception:
        pass



def normalized_op_type(op_type: str | None) -> str:
    normalized = str(op_type or "").strip().lower()

    if normalized in {"mini op", "mini-op"}:
        return "mini"

    if normalized in {"tournement"}:
        return "tournament"

    if normalized in {"tradning"}:
        return "training"

    return normalized


def color_for_event(event: OpEventRecord) -> str:
    if event.status == "Canceled":
        return ANSI_RED

    normalized = normalized_op_type(event.op_type)

    if normalized == "normal":
        return ANSI_GREEN

    if normalized == "mini":
        return ANSI_BLUE

    if normalized == "arcade":
        return ANSI_CYAN

    if normalized == "tournament":
        return ANSI_YELLOW

    if normalized == "training":
        # Discord ANSI has no true pink; purple/magenta is the closest supported color.
        return ANSI_PURPLE

    return ANSI_WHITE


def pad(text: str, width: int) -> str:
    if len(text) >= width:
        return text[:width]

    return text + (" " * (width - len(text)))


def color_schedule_row(event: OpEventRecord, row_text: str) -> str:
    prefix = color_for_event(event)

    if event.status != "Canceled" and is_default_slot_timestamp(event.scheduled_at):
        prefix = ANSI_BOLD + prefix

    return f"{prefix}{row_text}{ANSI_RESET}"


def visible_window_bounds(
    total: int,
    selected_index: int,
    window_size: int = 11,
) -> tuple[int, int]:
    if total <= window_size:
        return 0, total

    half = window_size // 2
    start = selected_index - half

    if start < 0:
        start = 0

    end = start + window_size

    if end > total:
        end = total
        start = max(0, end - window_size)

    return start, end


def initial_selected_index(events: list[OpEventRecord]) -> int:
    if not events:
        return 0

    current_time = now_ts()

    for index, event in enumerate(events):
        if event.status in ACTIVE_STATUSES and event.scheduled_at >= current_time:
            return index

    for index, event in enumerate(events):
        if event.status in ACTIVE_STATUSES:
            return index

    return 0





FILTER_OPTIONS = [
    ("Hide Canceled", "canceled"),
    ("Hide Normal", "normal"),
    ("Hide Mini", "mini"),
    ("Hide Arcade", "arcade"),
    ("Hide Tournament", "tournament"),
    ("Hide Training", "training"),
]


def event_matches_hidden_filters(event: OpEventRecord, hidden_filters: set[str]) -> bool:
    filters = set(hidden_filters or set())

    if "canceled" in filters and event.status == "Canceled":
        return False

    event_type = normalized_op_type(event.op_type)

    if event_type in filters:
        return False

    return True


def get_filtered_schedule_events(
    *,
    sort_by: str,
    hidden_filters: set[str],
) -> list[OpEventRecord]:
    return [
        event
        for event in get_scheduled_op_events(sort_by=sort_by)
        if event_matches_hidden_filters(event, hidden_filters)
    ]


def selected_index_for_event_id(
    events: list[OpEventRecord],
    event_id: int | None,
) -> int:
    if event_id is None:
        return initial_selected_index(events)

    for index, event in enumerate(events):
        if event.event_id == event_id:
            return index

    return initial_selected_index(events)

def build_event_menu(
    events: list[OpEventRecord],
    selected_index: int,
    timezone_name: str,
) -> str:
    if not events:
        return "```ansi\nNo scheduled ops found.\n```"

    header = f"{pad('ID', 8)} {pad('Time', 21)} Name"
    start, end = visible_window_bounds(len(events), selected_index, window_size=11)

    lines = [header]

    if start > 0:
        lines.append("...")

    for index in range(start, end):
        event = events[index]
        marker = ">" if index == selected_index else " "
        event_id = f"{marker}#{event.event_id}"
        event_time = format_timestamp_short(event.scheduled_at, timezone_name)
        row_text = f"{pad(event_id, 8)} {pad(event_time, 21)} {event.op_name}"

        lines.append(color_schedule_row(event, row_text))

    if end < len(events):
        lines.append("...")

    return "```ansi\n" + "\n".join(lines) + "\n```"


def reservation_is_locked(row: dict) -> bool:
    return str(row.get("status") or "").strip().lower() == "locked"


def reservation_is_reserved(row: dict) -> bool:
    status = str(row.get("status") or "").strip().lower()
    return bool(row.get("reserved_by")) or status == "reserved"


def reservation_is_taken(row: dict) -> bool:
    # Filled/taken count should mean actually reserved, not merely locked open.
    return reservation_is_reserved(row)


def reservation_user_display_name(
    value: str | int | None,
    guild: discord.Guild | None,
) -> str:
    raw = str(value or "").strip()

    if not raw:
        return "Reserved"

    # Supports raw IDs and mention strings like <@123> / <@!123>.
    discord_id = (
        raw.replace("<@", "")
        .replace("!", "")
        .replace(">", "")
        .strip()
    )

    if discord_id.isdigit() and guild is not None:
        member = guild.get_member(int(discord_id))

        if member is not None:
            return member.display_name

    return raw


def reservation_color(row: dict) -> str:
    """Shared reservation color rules.

    Orange = open
    Green  = taken/reserved
    """
    if reservation_is_taken(row):
        return ANSI_GREEN

    # Discord ANSI does not have a true orange, so yellow is the closest orange-like color.
    return ANSI_YELLOW


def reservation_status_text(row: dict, guild: discord.Guild | None) -> str:
    locked = reservation_is_locked(row)

    if reservation_is_taken(row):
        name = reservation_user_display_name(row.get("reserved_by"), guild)
        return f"LOCKED by {name}" if locked else f"TAKEN by {name}"

    return "LOCKED OPEN" if locked else "OPEN"


def build_reservation_summary(event_id: int, guild: discord.Guild | None) -> str:
    reservations = get_reservations_for_event(event_id)

    if not reservations:
        return "```ansi\nNo reservation slots.\n```"

    lines = [
        "Orange=open | Green=taken",
        "",
    ]

    for row in reservations:
        label = row["slot_label"]
        status_text = reservation_status_text(row, guild)
        line = f"{label}: {status_text}"
        lines.append(f"{reservation_color(row)}{line}{ANSI_RESET}")

    return ("```ansi\n" + "\n".join(lines) + "\n```")[:1024]


def reservation_counts(event_id: int) -> tuple[int, int]:
    reservations = get_reservations_for_event(event_id)
    total = len(reservations)
    filled = 0

    for row in reservations:
        if reservation_is_taken(row):
            filled += 1

    return filled, total



def build_scheduleview_embed(
    events: list[OpEventRecord],
    selected_index: int,
    timezone_name: str,
    sort_by: str,
    guild: discord.Guild | None = None,
    hidden_filters: set[str] | None = None,
) -> discord.Embed:
    event = selected_event(events, selected_index)

    embed = discord.Embed(
        title="Schedule View",
        description=build_event_menu(events, selected_index, timezone_name),
    )

    if event is None:
        embed.add_field(
            name="Selected Op",
            value="None",
            inline=False,
        )
    else:
        filled, total = reservation_counts(event.event_id)

        embed.add_field(
            name="Selected Op",
            value=(
                f"**ID:** {event.event_id}\n"
                f"**Op:** {event.op_name}\n"
                f"**Type:** {event.op_type}\n"
                f"**Status:** {event.status}\n"
                f"**When:** {format_timestamp_local(event.scheduled_at, timezone_name)} / <t:{event.scheduled_at}:R>"
            ),
            inline=False,
        )

        embed.add_field(
            name=f"Reservations: {filled}/{total}",
            value=build_reservation_summary(event.event_id, guild),
            inline=False,
        )

    sort_label = "Time" if sort_by == "time" else "ID"
    filter_labels = [
        label.replace("Hide ", "")
        for label, value in FILTER_OPTIONS
        if value in set(hidden_filters or set())
    ]
    filter_text = ", ".join(filter_labels) if filter_labels else "None"
    embed.set_footer(text=f"Sort: {sort_label} | Hidden: {filter_text} | Displayed in your timezone: {timezone_name}")

    return embed



class ScheduleView(PrivateTimeoutView):
    def __init__(
        self,
        owner_id: int,
        events: list[OpEventRecord],
        selected_index: int,
        timezone_name: str,
        sort_by: str,
        hidden_filters: set[str] | None = None,
    ):
        super().__init__()
        self.owner_id = owner_id
        self.events = events
        self.selected_index = selected_index
        self.timezone_name = timezone_name
        self.sort_by = sort_by
        self.hidden_filters = set(hidden_filters or set())

        event = selected_event(events, selected_index)
        action_label = "Uncancel" if event and event.status == "Canceled" else "Cancel Event"

        self.add_item(ScheduleSortSelect(sort_by))
        self.add_item(ScheduleFilterSelect(self.hidden_filters))
        self.add_item(PrevButton(disabled=len(events) <= 1))
        self.add_item(CancelUncancelButton(label=action_label, disabled=not events))
        self.add_item(NextButton(disabled=len(events) <= 1))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the person who opened schedule view can use these controls.",
                ephemeral=True,
            )
            return False

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This command can only be used inside the server.",
                ephemeral=True,
            )
            return False

        if not has_schedule_permission(interaction.user):
            await interaction.response.send_message(
                "You do not have permission to view/manage scheduled ops.",
                ephemeral=True,
            )
            return False

        return True

    async def refresh(
        self,
        interaction: discord.Interaction,
        *,
        selected_index: int | None = None,
        sort_by: str | None = None,
        hidden_filters: set[str] | None = None,
        keep_event_id: int | None = None,
    ):
        new_sort_by = sort_by or self.sort_by
        new_hidden_filters = set(self.hidden_filters if hidden_filters is None else hidden_filters)

        if keep_event_id is None and self.events:
            current_event = selected_event(self.events, self.selected_index)
            keep_event_id = current_event.event_id if current_event else None

        events = get_filtered_schedule_events(
            sort_by=new_sort_by,
            hidden_filters=new_hidden_filters,
        )

        if keep_event_id is not None:
            selected_index = selected_index_for_event_id(events, keep_event_id)
        elif selected_index is None:
            selected_index = self.selected_index

        if events:
            selected_index %= len(events)
        else:
            selected_index = 0

        view = ScheduleView(
            owner_id=self.owner_id,
            events=events,
            selected_index=selected_index,
            timezone_name=self.timezone_name,
            sort_by=new_sort_by,
            hidden_filters=new_hidden_filters,
        )

        await interaction.response.edit_message(
            embed=build_scheduleview_embed(
                events,
                selected_index,
                self.timezone_name,
                new_sort_by,
                interaction.guild,
                new_hidden_filters,
            ),
            view=bind_private_view(view, interaction.message),
        )



class ScheduleSortSelect(discord.ui.Select):
    def __init__(self, sort_by: str):
        options = [
            discord.SelectOption(
                label="Time",
                value="time",
                description="Sort by scheduled time",
                default=sort_by == "time",
            ),
            discord.SelectOption(
                label="ID",
                value="id",
                description="Sort by event ID",
                default=sort_by == "id",
            ),
        ]

        super().__init__(
            placeholder="Sort",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, ScheduleView)

        sort_by = self.values[0]

        await self.view.refresh(
            interaction,
            sort_by=sort_by,
            selected_index=0 if sort_by == "id" else None,
        )


class ScheduleFilterSelect(discord.ui.Select):
    def __init__(self, hidden_filters: set[str]):
        hidden = set(hidden_filters or set())
        options = [
            discord.SelectOption(
                label=label,
                value=value,
                default=value in hidden,
            )
            for label, value in FILTER_OPTIONS
        ]

        super().__init__(
            placeholder="Hide filters",
            min_values=0,
            max_values=len(options),
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, ScheduleView)

        await self.view.refresh(
            interaction,
            hidden_filters=set(self.values),
        )


class PrevButton(discord.ui.Button):
    def __init__(self, disabled: bool):
        super().__init__(
            label="Prev",
            style=discord.ButtonStyle.primary,
            disabled=disabled,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, ScheduleView)

        self.view.selected_index = (self.view.selected_index - 1) % len(self.view.events)

        await self.view.refresh(interaction)


class NextButton(discord.ui.Button):
    def __init__(self, disabled: bool):
        super().__init__(
            label="Next",
            style=discord.ButtonStyle.primary,
            disabled=disabled,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, ScheduleView)

        self.view.selected_index = (self.view.selected_index + 1) % len(self.view.events)

        await self.view.refresh(interaction)


class CancelUncancelButton(discord.ui.Button):
    def __init__(self, label: str, disabled: bool):
        style = discord.ButtonStyle.success if label == "Uncancel" else discord.ButtonStyle.danger

        super().__init__(
            label=label,
            style=style,
            disabled=disabled,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, ScheduleView)

        event = selected_event(self.view.events, self.view.selected_index)

        if event is None:
            await interaction.response.send_message(
                "There is no selected scheduled op.",
                ephemeral=True,
            )
            return

        if event.status == "Canceled":
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title="Uncancel Scheduled Op?",
                    description=(
                        f"**Op:** {event.op_name}\n"
                        f"**When:** {format_timestamp_local(event.scheduled_at, self.view.timezone_name)}\n\n"
                        "This will set the local event back to `Scheduled`, reopen canceled reservation slots, "
                        "create a new Discord server scheduled event, and store the new server event ID."
                    ),
                ),
                view=ConfirmUncancelEventView(
                    owner_id=self.view.owner_id,
                    event=event,
                    timezone_name=self.view.timezone_name,
                    sort_by=self.view.sort_by,
                    hidden_filters=self.view.hidden_filters,
                ),
            )
            return

        await interaction.response.edit_message(
            embed=discord.Embed(
                title="Cancel Scheduled Event?",
                description=(
                    f"**Op:** {event.op_name}\n"
                    f"**When:** {format_timestamp_local(event.scheduled_at, self.view.timezone_name)}\n\n"
                    "This will cancel the local event and reservation slots immediately. "
                    "The Discord server event will be cleaned up right after."
                ),
            ),
            view=ConfirmCancelEventView(
                owner_id=self.view.owner_id,
                event=event,
                timezone_name=self.view.timezone_name,
                sort_by=self.view.sort_by,
                hidden_filters=self.view.hidden_filters,
            ),
        )


class ConfirmCancelEventView(PrivateTimeoutView):
    def __init__(
        self,
        owner_id: int,
        event: OpEventRecord,
        timezone_name: str,
        sort_by: str,
        hidden_filters: set[str] | None = None,
    ):
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.event = event
        self.timezone_name = timezone_name
        self.sort_by = sort_by
        self.hidden_filters = set(hidden_filters or set())

        self.add_item(ConfirmCancelEventButton())
        self.add_item(BackButton())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the person who opened schedule view can use these controls.",
                ephemeral=True,
            )
            return False

        return True


class ConfirmCancelEventButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Cancel Event",
            style=discord.ButtonStyle.danger,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, ConfirmCancelEventView)

        await interaction.response.defer()

        try:
            cancel_op_event(self.view.event.event_id)
        except Exception as error:
            await interaction.followup.send(
                f"Failed to cancel event in the database: `{error}`",
                ephemeral=True,
            )
            return

        events = get_filtered_schedule_events(
            sort_by=self.view.sort_by,
            hidden_filters=self.view.hidden_filters,
        )

        await interaction.edit_original_response(
            embed=build_scheduleview_embed(
                events,
                0,
                self.view.timezone_name,
                self.view.sort_by,
                interaction.guild,
                self.view.hidden_filters,
            ),
            view=ScheduleView(
                owner_id=self.view.owner_id,
                events=events,
                selected_index=0,
                timezone_name=self.view.timezone_name,
                sort_by=self.view.sort_by,
                hidden_filters=self.view.hidden_filters,
            ),
        )

        queue_discord_server_event_cancel(
            interaction.guild,
            self.view.event.server_event_id,
        )


class ConfirmUncancelEventView(PrivateTimeoutView):
    def __init__(
        self,
        owner_id: int,
        event: OpEventRecord,
        timezone_name: str,
        sort_by: str,
        hidden_filters: set[str] | None = None,
    ):
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.event = event
        self.timezone_name = timezone_name
        self.sort_by = sort_by
        self.hidden_filters = set(hidden_filters or set())

        self.add_item(ConfirmUncancelEventButton())
        self.add_item(BackButton())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the person who opened schedule view can use these controls.",
                ephemeral=True,
            )
            return False

        return True


class ConfirmUncancelEventButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Uncancel",
            style=discord.ButtonStyle.success,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, ConfirmUncancelEventView)

        if interaction.guild is None:
            await interaction.response.send_message(
                "This can only be used inside a server.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        new_server_event_id: str | None = None

        try:
            server_event = await recreate_discord_server_event(
                guild=interaction.guild,
                event=self.view.event,
                created_by=interaction.user,
            )
            new_server_event_id = str(server_event.id)
        except Exception as error:
            await interaction.followup.send(
                f"Failed to recreate the Discord server event: `{error}`",
                ephemeral=True,
            )
            return

        try:
            uncancel_op_event(self.view.event.event_id)
            set_event_server_event_id(
                event_id=self.view.event.event_id,
                server_event_id=new_server_event_id,
            )
        except Exception as error:
            await delete_discord_server_event_silent(
                interaction.guild,
                new_server_event_id,
            )

            await interaction.followup.send(
                f"Discord event was created, but the database update failed: `{error}`",
                ephemeral=True,
            )
            return

        events = get_filtered_schedule_events(
            sort_by=self.view.sort_by,
            hidden_filters=self.view.hidden_filters,
        )

        await interaction.edit_original_response(
            embed=build_scheduleview_embed(
                events,
                0,
                self.view.timezone_name,
                self.view.sort_by,
                interaction.guild,
                self.view.hidden_filters,
            ),
            view=ScheduleView(
                owner_id=self.view.owner_id,
                events=events,
                selected_index=0,
                timezone_name=self.view.timezone_name,
                sort_by=self.view.sort_by,
                hidden_filters=self.view.hidden_filters,
            ),
        )


class BackButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Back",
            style=discord.ButtonStyle.secondary,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, (ConfirmCancelEventView, ConfirmUncancelEventView))

        events = get_filtered_schedule_events(
            sort_by=self.view.sort_by,
            hidden_filters=self.view.hidden_filters,
        )

        await interaction.response.edit_message(
            embed=build_scheduleview_embed(
                events,
                0,
                self.view.timezone_name,
                self.view.sort_by,
                interaction.guild,
                self.view.hidden_filters,
            ),
            view=ScheduleView(
                owner_id=self.view.owner_id,
                events=events,
                selected_index=0,
                timezone_name=self.view.timezone_name,
                sort_by=self.view.sort_by,
                hidden_filters=self.view.hidden_filters,
            ),
        )


class ScheduleViewCommands(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="scheduleview",
        description="View and flip through scheduled ops.",
    )
    @app_commands.guild_only()
    async def scheduleview_command(
        self,
        interaction: discord.Interaction,
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
                "You do not have permission to view/manage scheduled ops.",
                ephemeral=True,
            )
            return

        timezone_name = get_user_timezone(str(interaction.user.id))
        sort_by = "time"
        hidden_filters: set[str] = set()
        events = get_filtered_schedule_events(
            sort_by=sort_by,
            hidden_filters=hidden_filters,
        )
        selected_index = initial_selected_index(events)

        view = ScheduleView(
            owner_id=interaction.user.id,
            events=events,
            selected_index=selected_index,
            timezone_name=timezone_name,
            sort_by=sort_by,
            hidden_filters=hidden_filters,
        )

        await interaction.response.send_message(
            embed=build_scheduleview_embed(events, selected_index, timezone_name, sort_by, interaction.guild, hidden_filters),
            view=view,
            ephemeral=True,
        )
        await bind_view_to_original_response(interaction, view)


async def setup(bot: commands.Bot):
    await bot.add_cog(ScheduleViewCommands(bot))
