from __future__ import annotations

import asyncio
from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands
from services.permission_service import (
    require_mission_executer_command,
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
    from config import OP_FLIGHT_VOICE_CHANNEL_IDS
except ImportError:
    OP_FLIGHT_VOICE_CHANNEL_IDS = []

try:
    from config import OP_FLIGHT_START_FREQUENCY
except ImportError:
    OP_FLIGHT_START_FREQUENCY = 200.0

try:
    from config import OP_FLIGHT_FREQUENCY_INCREMENT
except ImportError:
    OP_FLIGHT_FREQUENCY_INCREMENT = 5.0

try:
    from config import OP_FLIGHT_VC_UPDATE_DELAY_SECONDS
except ImportError:
    OP_FLIGHT_VC_UPDATE_DELAY_SECONDS = 1.0

from services.op_lifecycle_service import (
    ActiveConflict,
    LifecycleOp,
    complete_op,
    complete_then_open,
    complete_then_start,
    flight_templates_for_event,
    format_timestamp_short,
    get_briefing_conflict,
    get_lifecycle_op,
    get_open_conflict,
    get_user_timezone,
    open_op_and_create_attendance,
    recent_completed_lifecycle_ops,
    option_description,
    option_label,
    search_lifecycle_ops,
    start_op,
    when_label,
)
from services.reward_service import queue_reward_reconciliation

from services.situation_room_service import queue_situation_room_refresh



@dataclass
class VoiceChannelSetupResult:
    flight_channels_updated: int = 0
    empty_channels_updated: int = 0
    skipped_flights: int = 0
    errors: list[str] | None = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


VOICE_CHANNEL_SETUP_TASKS: set[asyncio.Task] = set()
VOICE_CHANNEL_SETUP_LOCKS: set[str] = set()


def configured_flight_voice_channel_ids() -> list[int]:
    channel_ids: list[int] = []

    for value in OP_FLIGHT_VOICE_CHANNEL_IDS or []:
        try:
            channel_id = int(value or 0)
        except (TypeError, ValueError):
            channel_id = 0

        if channel_id:
            channel_ids.append(channel_id)

    return channel_ids


def voice_channel_setup_lock_key(guild_id: int, event_id: int) -> str:
    return f"{guild_id}:{event_id}"


def format_flight_voice_channel_name(
    *,
    flight: dict,
    frequency: float,
) -> str:
    letter = str(flight.get("flight_letter") or "?").strip().upper()[:1] or "?"
    flight_name = str(flight.get("flight_name") or "Unnamed").strip() or "Unnamed"

    return f"{letter} | {flight_name} - {frequency:.1f}"[:100]


async def configure_flight_voice_channels(
    *,
    bot: commands.Bot,
    guild: discord.Guild,
    event_id: int,
    op_name: str,
) -> VoiceChannelSetupResult:
    """Rename configured flight VCs slowly to avoid Discord rate-limit bursts."""
    result = VoiceChannelSetupResult()
    channel_ids = configured_flight_voice_channel_ids()
    flights = flight_templates_for_event(event_id)

    if not channel_ids:
        result.errors.append(
            "No flight voice channels are configured. Set OP_FLIGHT_VOICE_CHANNEL_IDS in config.py."
        )
        return result

    if not flights:
        result.errors.append("This op template has no flight templates.")
        return result

    try:
        delay = max(1.0, float(OP_FLIGHT_VC_UPDATE_DELAY_SECONDS))
    except (TypeError, ValueError):
        delay = 1.0

    try:
        start_frequency = float(OP_FLIGHT_START_FREQUENCY)
    except (TypeError, ValueError):
        start_frequency = 200.0

    try:
        frequency_increment = float(OP_FLIGHT_FREQUENCY_INCREMENT)
    except (TypeError, ValueError):
        frequency_increment = 5.0

    result.skipped_flights = max(0, len(flights) - len(channel_ids))

    for position, channel_id in enumerate(channel_ids):
        channel = guild.get_channel(channel_id)

        if channel is None:
            result.errors.append(f"Configured VC `{channel_id}` was not found in this server.")
        elif not isinstance(channel, discord.VoiceChannel):
            result.errors.append(f"Configured channel `{channel_id}` is not a standard voice channel.")
        else:
            if position < len(flights):
                flight = flights[position]

                try:
                    user_limit = max(0, int(flight.get("slot_count") or 0))
                except (TypeError, ValueError):
                    user_limit = 0

                desired_name = format_flight_voice_channel_name(
                    flight=flight,
                    frequency=start_frequency + (frequency_increment * position),
                )

                if channel.name != desired_name or channel.user_limit != user_limit:
                    try:
                        await channel.edit(
                            name=desired_name,
                            user_limit=user_limit,
                            reason=f"Starting #{event_id} {op_name} flight voice setup",
                        )
                        result.flight_channels_updated += 1
                    except discord.HTTPException as error:
                        result.errors.append(f"`{channel.name}`: {error}")
            else:
                if channel.name.lower() != "empty" or channel.user_limit != 0:
                    try:
                        await channel.edit(
                            name="empty",
                            user_limit=0,
                            reason=f"Clearing unused flight VC for #{event_id} {op_name}",
                        )
                        result.empty_channels_updated += 1
                    except discord.HTTPException as error:
                        result.errors.append(f"`{channel.name}`: {error}")

        # One configured channel per second, including errors/missing channels.
        if position < len(channel_ids) - 1:
            await asyncio.sleep(delay)

    return result


async def run_flight_voice_channel_setup(
    *,
    interaction: discord.Interaction,
    event_id: int,
    op_name: str,
) -> None:
    """Background wrapper that reports the final rename result to the command user."""
    guild = interaction.guild

    if guild is None:
        return

    lock_key = voice_channel_setup_lock_key(guild.id, event_id)

    try:
        result = await configure_flight_voice_channels(
            bot=interaction.client,
            guild=guild,
            event_id=event_id,
            op_name=op_name,
        )
    except Exception as error:
        result = VoiceChannelSetupResult(errors=[f"{type(error).__name__}: {error}"])
    finally:
        VOICE_CHANNEL_SETUP_LOCKS.discard(lock_key)

    parts = [
        "Flight VC setup finished.",
        f"Flights updated: `{result.flight_channels_updated}`",
        f"Empty VCs reset: `{result.empty_channels_updated}`",
    ]

    if result.skipped_flights:
        parts.append(
            f"⚠️ Flights without a configured VC: `{result.skipped_flights}`"
        )

    if result.errors:
        error_lines = "\n".join(f"- {error}" for error in result.errors[:8])
        parts.append(f"⚠️ Issues:\n{error_lines}")

        if len(result.errors) > 8:
            parts.append(f"...and {len(result.errors) - 8} more issue(s).")

    try:
        await interaction.followup.send("\n".join(parts)[:1900], ephemeral=True)
    except (discord.HTTPException, discord.NotFound):
        pass


def launch_flight_voice_channel_setup(
    *,
    interaction: discord.Interaction,
    event_id: int,
    op_name: str,
) -> bool:
    """Launch once per guild/op while the rename task is active."""
    if interaction.guild is None:
        return False

    lock_key = voice_channel_setup_lock_key(interaction.guild.id, event_id)

    if lock_key in VOICE_CHANNEL_SETUP_LOCKS:
        return False

    VOICE_CHANNEL_SETUP_LOCKS.add(lock_key)

    task = asyncio.create_task(
        run_flight_voice_channel_setup(
            interaction=interaction,
            event_id=event_id,
            op_name=op_name,
        )
    )
    VOICE_CHANNEL_SETUP_TASKS.add(task)
    task.add_done_callback(VOICE_CHANNEL_SETUP_TASKS.discard)

    return True


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


def has_lifecycle_permission(member: discord.Member) -> bool:
    if member_is_admin(member):
        return True
    role_ids = configured_role_ids()

    if not role_ids:
        return True

    return any(role.id in role_ids for role in member.roles)


def parse_event_id(value: str) -> int | None:
    text = str(value or "").strip()

    if text.startswith("#"):
        text = text[1:].strip()

    # Supports autocomplete values like "14" and manually typed "#14 Last Drop".
    first = text.split(" ", 1)[0].strip()

    if not first.isdigit():
        return None

    return int(first)


async def lifecycle_op_choices(
    interaction: discord.Interaction,
    current: str,
    *,
    statuses: list[str] | tuple[str, ...],
) -> list[app_commands.Choice[str]]:
    timezone_name = get_user_timezone(str(interaction.user.id))
    ops = search_lifecycle_ops(query=current, limit=25, statuses=statuses)

    return [
        app_commands.Choice(
            name=option_label(op, timezone_name),
            value=str(op.event_id),
        )
        for op in ops[:25]
    ]


async def start_op_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    return await lifecycle_op_choices(
        interaction,
        current,
        statuses=("Scheduled",),
    )


async def open_op_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    timezone_name = get_user_timezone(str(interaction.user.id))
    ops = search_lifecycle_ops(
        query=current,
        limit=25,
        statuses=("Scheduled", "Briefing"),
    )

    seen_event_ids = {int(op.event_id) for op in ops}
    completed_ops = recent_completed_lifecycle_ops(limit=3)

    for completed_op in completed_ops:
        if int(completed_op.event_id) in seen_event_ids:
            continue

        ops.append(completed_op)
        seen_event_ids.add(int(completed_op.event_id))

    return [
        app_commands.Choice(
            name=option_label(op, timezone_name),
            value=str(op.event_id),
        )
        for op in ops[:25]
    ]


async def complete_op_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    return await lifecycle_op_choices(
        interaction,
        current,
        statuses=("Open",),
    )


def conflict_text(conflict: ActiveConflict, timezone_name: str) -> str:
    label = when_label(
        scheduled_at=conflict.scheduled_at,
        timezone_name=timezone_name,
    )

    return (
        f"**Current active op:** #{conflict.event_id} {conflict.op_name}\n"
        f"**Status:** {conflict.status}\n"
        f"**{label}:** {format_timestamp_short(conflict.scheduled_at, timezone_name)} / <t:{conflict.scheduled_at}:R>"
    )


def op_text(op: LifecycleOp, timezone_name: str) -> str:
    label = when_label(
        scheduled_at=op.scheduled_at,
        timezone_name=timezone_name,
    )

    return (
        f"**Op:** #{op.event_id} {op.op_name}\n"
        f"**Type:** {op.op_type}\n"
        f"**Status:** {op.status}\n"
        f"**{label}:** {format_timestamp_short(op.scheduled_at, timezone_name)} / <t:{op.scheduled_at}:R>"
    )


class CancelLifecycleButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="Cancelled",
                description="No changes were made.",
            ),
            view=None,
        )


class ConfirmStartView(discord.ui.View):
    def __init__(
        self,
        *,
        owner_id: int,
        op: LifecycleOp,
        timezone_name: str,
    ):
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.op = op
        self.timezone_name = timezone_name

        self.add_item(CancelLifecycleButton())
        self.add_item(ConfirmStartButton())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the person who opened this command can use these controls.",
                ephemeral=True,
            )
            return False

        return True


class ConfirmStartButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Start",
            style=discord.ButtonStyle.success,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, ConfirmStartView)

        await interaction.response.defer()

        try:
            start_op(self.view.op.event_id)
        except Exception as error:
            await interaction.followup.send(
                f"Failed to start op: `{error}`",
                ephemeral=True,
            )
            return

        queue_situation_room_refresh(
            interaction.client,
            reason="op started",
        )

        vc_setup_started = launch_flight_voice_channel_setup(
            interaction=interaction,
            event_id=self.view.op.event_id,
            op_name=self.view.op.op_name,
        )

        vc_text = (
            "Flight VC setup started in the background. One channel updates per second."
            if vc_setup_started
            else "Flight VC setup is already running for this op."
        )

        await interaction.edit_original_response(
            embed=discord.Embed(
                title="Op Started",
                description=(
                    f"#{self.view.op.event_id} {self.view.op.op_name} is now `Briefing`.\n"
                    "Reservation slots for this op are now locked.\n"
                    f"{vc_text}"
                ),
            ),
            view=None,
        )


class CompleteOpenThenStartView(discord.ui.View):
    def __init__(
        self,
        *,
        owner_id: int,
        target_op: LifecycleOp,
        open_conflict: ActiveConflict,
        timezone_name: str,
    ):
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.target_op = target_op
        self.open_conflict = open_conflict
        self.timezone_name = timezone_name

        self.add_item(CancelLifecycleButton())
        self.add_item(CompleteOpenThenStartButton(open_conflict))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the person who opened this command can use these controls.",
                ephemeral=True,
            )
            return False

        return True


class CompleteOpenThenStartButton(discord.ui.Button):
    def __init__(self, conflict: ActiveConflict):
        super().__init__(
            label=f"Complete OP #{conflict.event_id}",
            style=discord.ButtonStyle.danger,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, CompleteOpenThenStartView)

        await interaction.response.defer()

        try:
            complete_then_start(
                complete_event_id=self.view.open_conflict.event_id,
                start_event_id=self.view.target_op.event_id,
            )
        except Exception as error:
            await interaction.followup.send(
                f"Failed to complete/start ops: `{error}`",
                ephemeral=True,
            )
            return

        queue_situation_room_refresh(
            interaction.client,
            reason="op complete then start",
        )
        queue_reward_reconciliation(
            interaction.client,
            reason="op completed during complete/start",
        )

        vc_setup_started = launch_flight_voice_channel_setup(
            interaction=interaction,
            event_id=self.view.target_op.event_id,
            op_name=self.view.target_op.op_name,
        )

        vc_text = (
            "Flight VC setup started in the background. One channel updates per second."
            if vc_setup_started
            else "Flight VC setup is already running for this op."
        )

        await interaction.edit_original_response(
            embed=discord.Embed(
                title="Op Swapped",
                description=(
                    f"Completed #{self.view.open_conflict.event_id} {self.view.open_conflict.op_name}.\n"
                    f"Started #{self.view.target_op.event_id} {self.view.target_op.op_name} as `Briefing`.\n"
                    "Reservation slots for the started op are now locked.\n"
                    f"{vc_text}"
                ),
            ),
            view=None,
        )



class CompleteOpenThenOpenView(discord.ui.View):
    def __init__(
        self,
        *,
        owner_id: int,
        target_op: LifecycleOp,
        open_conflict: ActiveConflict,
        timezone_name: str,
    ):
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.target_op = target_op
        self.open_conflict = open_conflict
        self.timezone_name = timezone_name

        self.add_item(CancelLifecycleButton())
        self.add_item(CompleteOpenThenOpenButton(target_op, open_conflict))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the person who opened this command can use these controls.",
                ephemeral=True,
            )
            return False

        return True


class CompleteOpenThenOpenButton(discord.ui.Button):
    def __init__(self, target_op: LifecycleOp, conflict: ActiveConflict):
        super().__init__(
            label=f"Complete #{conflict.event_id} and Open #{target_op.event_id}",
            style=discord.ButtonStyle.danger,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, CompleteOpenThenOpenView)

        await interaction.response.defer()

        try:
            created_count = complete_then_open(
                complete_event_id=self.view.open_conflict.event_id,
                open_event_id=self.view.target_op.event_id,
            )
        except Exception as error:
            await interaction.followup.send(
                f"Failed to complete/open ops: `{error}`",
                ephemeral=True,
            )
            return

        queue_situation_room_refresh(
            interaction.client,
            reason="op complete then open",
        )
        queue_reward_reconciliation(
            interaction.client,
            reason="op completed during complete/open",
        )

        await interaction.edit_original_response(
            embed=discord.Embed(
                title="Op Swapped",
                description=(
                    f"Completed #{self.view.open_conflict.event_id} {self.view.open_conflict.op_name}.\n"
                    f"Opened #{self.view.target_op.event_id} {self.view.target_op.op_name}.\n"
                    f"Attendance slots created: `{created_count}`"
                ),
            ),
            view=None,
        )


class ConfirmOpenView(discord.ui.View):
    def __init__(
        self,
        *,
        owner_id: int,
        op: LifecycleOp,
        timezone_name: str,
    ):
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.op = op
        self.timezone_name = timezone_name

        self.add_item(CancelLifecycleButton())
        self.add_item(ConfirmOpenButton())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the person who opened this command can use these controls.",
                ephemeral=True,
            )
            return False

        return True


class ConfirmOpenButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Open",
            style=discord.ButtonStyle.success,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, ConfirmOpenView)

        await interaction.response.defer()

        try:
            created_count = open_op_and_create_attendance(self.view.op.event_id)
        except Exception as error:
            await interaction.followup.send(
                f"Failed to open op: `{error}`",
                ephemeral=True,
            )
            return

        queue_situation_room_refresh(
            interaction.client,
            reason="op opened",
        )

        await interaction.edit_original_response(
            embed=discord.Embed(
                title="Op Opened",
                description=(
                    f"#{self.view.op.event_id} {self.view.op.op_name} is now `Open`.\n"
                    f"Attendance slots created: `{created_count}`"
                ),
            ),
            view=None,
        )


class ConfirmCompleteView(discord.ui.View):
    def __init__(
        self,
        *,
        owner_id: int,
        op: LifecycleOp,
        timezone_name: str,
    ):
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.op = op
        self.timezone_name = timezone_name

        self.add_item(CancelLifecycleButton())
        self.add_item(ConfirmCompleteButton())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the person who opened this command can use these controls.",
                ephemeral=True,
            )
            return False

        return True


class ConfirmCompleteButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Complete",
            style=discord.ButtonStyle.success,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, ConfirmCompleteView)

        await interaction.response.defer()

        try:
            complete_op(self.view.op.event_id)
        except Exception as error:
            await interaction.followup.send(
                f"Failed to complete op: `{error}`",
                ephemeral=True,
            )
            return

        queue_situation_room_refresh(
            interaction.client,
            reason="op completed",
        )
        queue_reward_reconciliation(
            interaction.client,
            reason="op completed",
        )

        await interaction.edit_original_response(
            embed=discord.Embed(
                title="Op Completed",
                description=(
                    f"#{self.view.op.event_id} {self.view.op.op_name} is now `Complete`.\n"
                    "Open attendance slots were set to `complete`."
                ),
            ),
            view=None,
        )


class OpLifecycleCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def lifecycle_permission_check(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This command can only be used inside the server.",
                ephemeral=True,
            )
            return False

        if not has_lifecycle_permission(interaction.user):
            await interaction.response.send_message(
                "You do not have permission to manage op lifecycle status.",
                ephemeral=True,
            )
            return False

        return True

    @app_commands.command(
        name="start",
        description="Set a scheduled op to Briefing and lock reservations.",
    )
    @app_commands.guild_only()
    @app_commands.autocomplete(op=start_op_autocomplete)
    async def start_command(
        self,
        interaction: discord.Interaction,
        op: str,
    ):
        if not await require_mission_executer_command(interaction):
            return
        if not await self.lifecycle_permission_check(interaction):
            return

        event_id = parse_event_id(op)

        if event_id is None:
            await interaction.response.send_message(
                "Pick a valid scheduled op.",
                ephemeral=True,
            )
            return

        target_op = get_lifecycle_op(event_id)

        if target_op is None:
            await interaction.response.send_message(
                "That scheduled op was not found.",
                ephemeral=True,
            )
            return

        if target_op.status != "Scheduled":
            await interaction.response.send_message(
                "Only `Scheduled` ops can be started.",
                ephemeral=True,
            )
            return

        timezone_name = get_user_timezone(str(interaction.user.id))

        briefing_conflict = get_briefing_conflict(exclude_event_id=target_op.event_id)

        if briefing_conflict is not None:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="Cannot Start Op",
                    description=(
                        "There is already an op in `Briefing`.\n\n"
                        f"{conflict_text(briefing_conflict, timezone_name)}\n\n"
                        "Complete or resolve that op first."
                    ),
                ),
                ephemeral=True,
            )
            return

        open_conflict = get_open_conflict(exclude_event_id=target_op.event_id)

        if open_conflict is not None:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="Open Op Already Active",
                    description=(
                        "There is already an op in `Open`.\n\n"
                        f"{conflict_text(open_conflict, timezone_name)}\n\n"
                        "You can complete that op and then start the selected op."
                    ),
                ),
                view=CompleteOpenThenStartView(
                    owner_id=interaction.user.id,
                    target_op=target_op,
                    open_conflict=open_conflict,
                    timezone_name=timezone_name,
                ),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=discord.Embed(
                title="Start Scheduled Op?",
                description=(
                    f"{op_text(target_op, timezone_name)}\n\n"
                    "This will set the op to `Briefing` and lock all reservation slots for this op."
                ),
            ),
            view=ConfirmStartView(
                owner_id=interaction.user.id,
                op=target_op,
                timezone_name=timezone_name,
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="open",
        description="Set an op to Open and create attendance slots.",
    )
    @app_commands.guild_only()
    @app_commands.autocomplete(op=open_op_autocomplete)
    async def open_command(
        self,
        interaction: discord.Interaction,
        op: str,
    ):
        if not await require_mission_executer_command(interaction):
            return
        if not await self.lifecycle_permission_check(interaction):
            return

        event_id = parse_event_id(op)

        if event_id is None:
            await interaction.response.send_message(
                "Pick a valid scheduled op.",
                ephemeral=True,
            )
            return

        target_op = get_lifecycle_op(event_id)

        if target_op is None:
            await interaction.response.send_message(
                "That scheduled op was not found.",
                ephemeral=True,
            )
            return

        if target_op.status not in {"Scheduled", "Briefing", "Complete"}:
            await interaction.response.send_message(
                "Only `Scheduled`, `Briefing`, or `Complete` ops can be opened.",
                ephemeral=True,
            )
            return

        conflict = get_open_conflict(exclude_event_id=target_op.event_id)

        if conflict is not None:
            timezone_name = get_user_timezone(str(interaction.user.id))
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="Open Op Already Active",
                    description=(
                        "There is already an op in `Open`.\n\n"
                        f"{conflict_text(conflict, timezone_name)}\n\n"
                        "You can complete that op and immediately open the selected op."
                    ),
                ),
                view=CompleteOpenThenOpenView(
                    owner_id=interaction.user.id,
                    target_op=target_op,
                    open_conflict=conflict,
                    timezone_name=timezone_name,
                ),
                ephemeral=True,
            )
            return

        timezone_name = get_user_timezone(str(interaction.user.id))

        reopen_note = (
            "This completed op will be reopened. Previously empty completed attendance slots will be changed back to open."
            if target_op.status == "Complete"
            else "This will set the op to `Open` and create attendance slots from the flight template."
        )

        await interaction.response.send_message(
            embed=discord.Embed(
                title="Open Completed Op?" if target_op.status == "Complete" else "Open Scheduled Op?",
                description=(
                    f"{op_text(target_op, timezone_name)}\n\n"
                    f"{reopen_note}"
                ),
            ),
            view=ConfirmOpenView(
                owner_id=interaction.user.id,
                op=target_op,
                timezone_name=timezone_name,
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="complete",
        description="Set an op to Complete.",
    )
    @app_commands.guild_only()
    @app_commands.autocomplete(op=complete_op_autocomplete)
    async def complete_command(
        self,
        interaction: discord.Interaction,
        op: str,
    ):
        if not await require_mission_executer_command(interaction):
            return
        if not await self.lifecycle_permission_check(interaction):
            return

        event_id = parse_event_id(op)

        if event_id is None:
            await interaction.response.send_message(
                "Pick a valid scheduled op.",
                ephemeral=True,
            )
            return

        target_op = get_lifecycle_op(event_id)

        if target_op is None:
            await interaction.response.send_message(
                "That scheduled op was not found.",
                ephemeral=True,
            )
            return

        if target_op.status != "Open":
            await interaction.response.send_message(
                "Only `Open` ops can be completed.",
                ephemeral=True,
            )
            return

        timezone_name = get_user_timezone(str(interaction.user.id))

        await interaction.response.send_message(
            embed=discord.Embed(
                title="Complete Open Op?",
                description=(
                    f"{op_text(target_op, timezone_name)}\n\n"
                    "This will set the op to `Complete` and change open attendance slots to `complete`."
                ),
            ),
            view=ConfirmCompleteView(
                owner_id=interaction.user.id,
                op=target_op,
                timezone_name=timezone_name,
            ),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(OpLifecycleCog(bot))
