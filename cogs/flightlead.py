from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands
from services.permission_service import (
    require_flight_lead_command,
    member_is_admin,
)

try:
    from config import FLIGHT_LEAD_ROLE
except ImportError:
    FLIGHT_LEAD_ROLE = 0

try:
    from config import STAFF_ROLE
except ImportError:
    STAFF_ROLE = 0

try:
    from config import MISSION_EXECUTER_ROLE
except ImportError:
    MISSION_EXECUTER_ROLE = 0

try:
    from config import MISSION_EXECUTER_ROLES
except ImportError:
    MISSION_EXECUTER_ROLES = []

from services.flightlead_service import (
    FlightLeadEventSummary,
    FlightLeadSlot,
    count_user_reserved_slots_in_next_days,
    ensure_user_record,
    flightlead_slots_for_event,
    format_timestamp_short,
    get_flightlead_event,
    get_upcoming_flightlead_events,
    get_user_reserved_slot,
    get_user_timezone,
    reserve_flightlead_slot,
    unreserve_all_user_slots_in_next_days,
    unreserve_flightlead_slot,
)
from services.situation_room_service import queue_situation_room_refresh



ANSI_RESET = "\u001b[0m"
ANSI_GREEN = "\u001b[32m"
ANSI_RED = "\u001b[31m"
ANSI_YELLOW = "\u001b[33m"
ANSI_WHITE = "\u001b[37m"
# Discord ANSI support is limited. This is the closest usable orange in an ansi block.
ANSI_ORANGE = "\u001b[33m"


def configured_role_ids() -> set[int]:
    role_ids: set[int] = set()

    for value in [FLIGHT_LEAD_ROLE, STAFF_ROLE, MISSION_EXECUTER_ROLE]:
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


def has_flightlead_permission(member: discord.Member) -> bool:
    if member_is_admin(member):
        return True
    role_ids = configured_role_ids()

    if not role_ids:
        return True

    return any(role.id in role_ids for role in member.roles)


def event_color(event: FlightLeadEventSummary) -> str:
    remaining = max(0, event.total_slots - event.taken_slots)

    # Green = you have a reservation in this event.
    if event.user_has_slot:
        return ANSI_GREEN

    # Yellow = event is full and you do not have a reservation.
    if event.total_slots > 0 and remaining == 0:
        return ANSI_YELLOW

    # White = normal/default event list color.
    return ANSI_WHITE


def event_line(
    event: FlightLeadEventSummary,
    timezone_name: str,
) -> str:
    check = " ✅" if event.user_has_slot else ""

    line = (
        f"#{event.event_id} "
        f"{format_timestamp_short(event.scheduled_at, timezone_name)} "
        f"{event.op_name} - {event.taken_slots}/{event.total_slots}{check}"
    )

    return f"{event_color(event)}{line}{ANSI_RESET}"


def build_event_list_block(
    events: list[FlightLeadEventSummary],
    timezone_name: str,
) -> str:
    if not events:
        return "```ansi\nNo ops in the next 7 days.\n```"

    lines = [
        event_line(event, timezone_name)
        for event in events
    ]

    return "```ansi\n" + "\n".join(lines) + "\n```"


def slot_status_text(
    slot: FlightLeadSlot,
    user_id: int,
) -> str:
    if slot.is_reserved_by(str(user_id)):
        return "RESERVED BY YOU"

    if slot.reserved_by:
        return "TAKEN"

    if slot.status == "open":
        return "OPEN"

    return slot.status.upper()


def slot_color(slot: FlightLeadSlot) -> str:
    if slot.is_taken:
        return ANSI_GREEN

    return ANSI_ORANGE


def build_flight_list_block(
    slots: list[FlightLeadSlot],
    user_id: int,
) -> str:
    if not slots:
        return "```ansi\nNo flight lead slots found for this op.\n```"

    lines: list[str] = []

    for slot in slots:
        aircraft = slot.aircraft or "Unknown"
        status = slot_status_text(slot, user_id)
        line = f"{slot.flight_letter} | {slot.flight_name} — {aircraft}: {status}"

        lines.append(f"{slot_color(slot)}{line}{ANSI_RESET}")

    return "```ansi\n" + "\n".join(lines)[:3800] + "\n```"


def build_selected_flight_details(
    selected_slot: FlightLeadSlot | None,
) -> str:
    if selected_slot is None:
        return "Select a flight to see details."

    aircraft = selected_slot.aircraft or "Unknown"
    aircraft_count = selected_slot.aircraft_count if selected_slot.aircraft_count is not None else 0
    description = selected_slot.description or "None"

    return (
        f"**{selected_slot.flight_name}**\n"
        f"Airframe: {aircraft_count}x {aircraft}\n"
        f"Slots: {selected_slot.slot_count}x players\n"
        f"Description: {description}"
    )


def flight_option_label(slot: FlightLeadSlot) -> str:
    aircraft = slot.aircraft or "Unknown"

    return f"{slot.flight_letter} | {slot.flight_name} — {aircraft}"[:100]


def selected_slot_or_user_slot(
    *,
    event_id: int,
    selected_reservation_id: int | None,
    discord_id: str,
) -> FlightLeadSlot | None:
    slots = flightlead_slots_for_event(event_id)

    if selected_reservation_id is not None:
        for slot in slots:
            if slot.reservation_id == int(selected_reservation_id):
                return slot

    user_slot = get_user_reserved_slot(
        event_id=event_id,
        discord_id=discord_id,
    )

    if user_slot is not None:
        return user_slot

    return None


class FlightLeadEventPage(discord.ui.View):
    def __init__(
        self,
        *,
        owner_id: int,
        timezone_name: str,
        events: list[FlightLeadEventSummary],
        selected_event_id: int | None = None,
    ):
        super().__init__(timeout=1800)

        self.owner_id = owner_id
        self.timezone_name = timezone_name
        self.events = events
        self.selected_event_id = selected_event_id
        self.user_reserved_count = count_user_reserved_slots_in_next_days(
            discord_id=str(owner_id),
            days=7,
        )

        self.add_item(EventSelect(events, selected_event_id, timezone_name))
        self.add_item(ExitFlightLeadButton(row=1))
        self.add_item(UnreserveAllButton(enabled=self.user_reserved_count > 0, row=1))
        self.add_item(NextButton(enabled=selected_event_id is not None, row=1))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the person who opened /flightlead can use these controls.",
                ephemeral=True,
            )
            return False

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This command can only be used inside the server.",
                ephemeral=True,
            )
            return False

        if not has_flightlead_permission(interaction.user):
            await interaction.response.send_message(
                "You do not have permission to reserve flight lead slots.",
                ephemeral=True,
            )
            return False

        return True

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=build_event_page_embed(self.events, self.timezone_name),
            view=FlightLeadEventPage(
                owner_id=self.owner_id,
                timezone_name=self.timezone_name,
                events=self.events,
                selected_event_id=self.selected_event_id,
            ),
        )


class EventSelect(discord.ui.Select):
    def __init__(
        self,
        events: list[FlightLeadEventSummary],
        selected_event_id: int | None,
        timezone_name: str,
    ):
        if events:
            options = []

            for event in events[:25]:
                check = " ✅" if event.user_has_slot else ""
                options.append(
                    discord.SelectOption(
                        label=f"#{event.event_id} {event.op_name}"[:100],
                        description=(
                            f"{format_timestamp_short(event.scheduled_at, timezone_name)} "
                            f"- {event.taken_slots}/{event.total_slots}{check}"
                        )[:100],
                        value=str(event.event_id),
                        default=selected_event_id == event.event_id,
                    )
                )
        else:
            options = [
                discord.SelectOption(
                    label="No ops in the next 7 days",
                    value="0",
                    description="There is nothing to select right now.",
                )
            ]

        super().__init__(
            placeholder="Select Event",
            min_values=1,
            max_values=1,
            options=options,
            disabled=not events,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, FlightLeadEventPage)

        self.view.selected_event_id = int(self.values[0])

        await self.view.refresh(interaction)


class ExitFlightLeadButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(
            label="Exit",
            style=discord.ButtonStyle.secondary,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="Flight Lead Closed",
                description="No more changes will be made from this message.",
            ),
            view=None,
        )


class UnreserveAllButton(discord.ui.Button):
    def __init__(self, enabled: bool, row: int):
        super().__init__(
            label="Unreserve All",
            style=discord.ButtonStyle.danger if enabled else discord.ButtonStyle.secondary,
            disabled=not enabled,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, FlightLeadEventPage)

        try:
            removed_count = unreserve_all_user_slots_in_next_days(
                discord_id=str(interaction.user.id),
                days=7,
            )
        except Exception as error:
            await interaction.response.send_message(
                f"Could not unreserve all slots: `{error}`",
                ephemeral=True,
            )
            return

        if removed_count:
            queue_situation_room_refresh(
                interaction.client,
                reason="flight lead slots unreserved",
            )

        events = get_upcoming_flightlead_events(
            discord_id=str(interaction.user.id),
            days=7,
        )

        embed = build_event_page_embed(events, self.view.timezone_name)

        if removed_count:
            embed.add_field(
                name="Unreserved",
                value=f"Released {removed_count} flight lead slot(s).",
                inline=False,
            )

        await interaction.response.edit_message(
            embed=embed,
            view=FlightLeadEventPage(
                owner_id=self.view.owner_id,
                timezone_name=self.view.timezone_name,
                events=events,
                selected_event_id=None,
            ),
        )


class NextButton(discord.ui.Button):
    def __init__(self, enabled: bool, row: int):
        super().__init__(
            label="Reserve Slot",
            style=discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary,
            disabled=not enabled,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, FlightLeadEventPage)

        if self.view.selected_event_id is None:
            await interaction.response.send_message(
                "Select an event first.",
                ephemeral=True,
            )
            return

        selected_event = get_flightlead_event(
            self.view.selected_event_id,
            str(interaction.user.id),
        )

        if selected_event is None:
            await interaction.response.send_message(
                "That event no longer exists.",
                ephemeral=True,
            )
            return

        user_slot = get_user_reserved_slot(
            event_id=selected_event.event_id,
            discord_id=str(interaction.user.id),
        )

        selected_reservation_id = user_slot.reservation_id if user_slot else None

        await interaction.response.edit_message(
            embed=build_details_embed(
                event=selected_event,
                timezone_name=self.view.timezone_name,
                user_id=interaction.user.id,
                selected_reservation_id=selected_reservation_id,
            ),
            view=FlightLeadDetailsPage(
                owner_id=self.view.owner_id,
                timezone_name=self.view.timezone_name,
                event=selected_event,
                selected_reservation_id=selected_reservation_id,
            ),
        )


class FlightLeadDetailsPage(discord.ui.View):
    def __init__(
        self,
        *,
        owner_id: int,
        timezone_name: str,
        event: FlightLeadEventSummary,
        selected_reservation_id: int | None = None,
    ):
        super().__init__(timeout=1800)

        self.owner_id = owner_id
        self.timezone_name = timezone_name
        self.event = event
        self.selected_reservation_id = selected_reservation_id

        selected_slot = selected_slot_or_user_slot(
            event_id=event.event_id,
            selected_reservation_id=selected_reservation_id,
            discord_id=str(owner_id),
        )

        if selected_slot is not None:
            self.selected_reservation_id = selected_slot.reservation_id

        self.add_item(FlightSelect(event.event_id, self.selected_reservation_id, str(owner_id)))
        self.add_item(BackButton(row=1))
        self.add_item(CancelButton(row=1))
        self.add_item(ReserveUnreserveButton(selected_slot, owner_id, row=1))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the person who opened /flightlead can use these controls.",
                ephemeral=True,
            )
            return False

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This command can only be used inside the server.",
                ephemeral=True,
            )
            return False

        if not has_flightlead_permission(interaction.user):
            await interaction.response.send_message(
                "You do not have permission to reserve flight lead slots.",
                ephemeral=True,
            )
            return False

        return True

    async def refresh(self, interaction: discord.Interaction):
        fresh_event = get_flightlead_event(
            self.event.event_id,
            str(self.owner_id),
        )

        if fresh_event is None:
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title="Flight Lead Reservation",
                    description="That event no longer exists.",
                ),
                view=None,
            )
            return

        await interaction.response.edit_message(
            embed=build_details_embed(
                event=fresh_event,
                timezone_name=self.timezone_name,
                user_id=self.owner_id,
                selected_reservation_id=self.selected_reservation_id,
            ),
            view=FlightLeadDetailsPage(
                owner_id=self.owner_id,
                timezone_name=self.timezone_name,
                event=fresh_event,
                selected_reservation_id=self.selected_reservation_id,
            ),
        )


class FlightSelect(discord.ui.Select):
    def __init__(
        self,
        event_id: int,
        selected_reservation_id: int | None,
        discord_id: str,
    ):
        slots = flightlead_slots_for_event(event_id)

        if slots:
            options = []

            for slot in slots[:25]:
                status = "Yours" if slot.is_reserved_by(discord_id) else ("Taken" if slot.is_taken else "Open")

                options.append(
                    discord.SelectOption(
                        label=flight_option_label(slot),
                        description=(
                            f"{status} | Aircraft: {slot.aircraft_count or 0} | Slots: {slot.slot_count}"
                        )[:100],
                        value=str(slot.reservation_id),
                        default=selected_reservation_id == slot.reservation_id,
                    )
                )
        else:
            options = [
                discord.SelectOption(
                    label="No flight slots",
                    value="0",
                    description="This op has no reservation slots.",
                )
            ]

        super().__init__(
            placeholder="Select Flight",
            min_values=1,
            max_values=1,
            options=options,
            disabled=not slots,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, FlightLeadDetailsPage)

        self.view.selected_reservation_id = int(self.values[0])

        await self.view.refresh(interaction)


class ReserveUnreserveButton(discord.ui.Button):
    def __init__(
        self,
        selected_slot: FlightLeadSlot | None,
        owner_id: int,
        row: int,
    ):
        if selected_slot is None:
            label = "Reserve"
            style = discord.ButtonStyle.secondary
            disabled = True
        elif selected_slot.is_reserved_by(str(owner_id)):
            label = "UnReserve"
            style = discord.ButtonStyle.danger
            disabled = False
        elif selected_slot.is_open:
            label = "Reserve"
            style = discord.ButtonStyle.success
            disabled = False
        else:
            label = "Reserve"
            style = discord.ButtonStyle.secondary
            disabled = True

        super().__init__(
            label=label,
            style=style,
            disabled=disabled,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, FlightLeadDetailsPage)

        if self.view.selected_reservation_id is None:
            await interaction.response.send_message(
                "Select a flight first.",
                ephemeral=True,
            )
            return

        selected_slot = selected_slot_or_user_slot(
            event_id=self.view.event.event_id,
            selected_reservation_id=self.view.selected_reservation_id,
            discord_id=str(interaction.user.id),
        )

        if selected_slot is None:
            await interaction.response.send_message(
                "That flight lead slot no longer exists.",
                ephemeral=True,
            )
            return

        try:
            if selected_slot.is_reserved_by(str(interaction.user.id)):
                unreserve_flightlead_slot(
                    event_id=self.view.event.event_id,
                    reservation_id=selected_slot.reservation_id,
                    discord_id=str(interaction.user.id),
                )
                self.view.selected_reservation_id = None
            else:
                reserve_flightlead_slot(
                    event_id=self.view.event.event_id,
                    reservation_id=selected_slot.reservation_id,
                    discord_id=str(interaction.user.id),
                )
                self.view.selected_reservation_id = selected_slot.reservation_id
        except Exception as error:
            await interaction.response.send_message(
                f"Could not update reservation: `{error}`",
                ephemeral=True,
            )
            return

        queue_situation_room_refresh(
            interaction.client,
            reason="flight lead reservation changed",
        )

        await self.view.refresh(interaction)


class BackButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(
            label="Back",
            style=discord.ButtonStyle.secondary,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, FlightLeadDetailsPage)

        events = get_upcoming_flightlead_events(
            discord_id=str(interaction.user.id),
            days=7,
        )

        await interaction.response.edit_message(
            embed=build_event_page_embed(events, self.view.timezone_name),
            view=FlightLeadEventPage(
                owner_id=self.view.owner_id,
                timezone_name=self.view.timezone_name,
                events=events,
                selected_event_id=self.view.event.event_id,
            ),
        )


class CancelButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="Flight Lead Reservation Closed",
                description="No changes are being made from this message.",
            ),
            view=None,
        )


def build_event_page_embed(
    events: list[FlightLeadEventSummary],
    timezone_name: str,
) -> discord.Embed:
    embed = discord.Embed(
        title="Flight Lead Reservations",
        description=(
            "Select an op event from the next 7 days.\n\n"
            f"{build_event_list_block(events, timezone_name)}"
        ),
    )

    embed.set_footer(text=f"Displayed in your timezone: {timezone_name}")

    return embed


def build_details_embed(
    *,
    event: FlightLeadEventSummary,
    timezone_name: str,
    user_id: int,
    selected_reservation_id: int | None,
) -> discord.Embed:
    slots = flightlead_slots_for_event(event.event_id)

    selected_slot = selected_slot_or_user_slot(
        event_id=event.event_id,
        selected_reservation_id=selected_reservation_id,
        discord_id=str(user_id),
    )

    title = (
        f"#{event.event_id} "
        f"{format_timestamp_short(event.scheduled_at, timezone_name)} "
        f"{event.op_name} - {event.taken_slots}/{event.total_slots}"
    )

    embed = discord.Embed(
        title=title[:256],
        description=(
            f"**Type:** {event.op_type}\n"
            f"**When:** {format_timestamp_short(event.scheduled_at, timezone_name)} / <t:{event.scheduled_at}:R>\n"
            f"**Status:** {event.status}\n"
            f"**Flights:**\n"
            f"{build_flight_list_block(slots, user_id)}"
        ),
    )

    embed.add_field(
        name="Flight Details",
        value=build_selected_flight_details(selected_slot),
        inline=False,
    )

    embed.set_footer(text=f"Displayed in your timezone: {timezone_name}")

    return embed


class FlightLeadCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="flightlead",
        description="Reserve or unreserve a flight lead slot.",
    )
    @app_commands.guild_only()
    async def flightlead_command(
        self,
        interaction: discord.Interaction,
    ):
        if not await require_flight_lead_command(interaction):
            return
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This command can only be used inside the server.",
                ephemeral=True,
            )
            return

        if not has_flightlead_permission(interaction.user):
            await interaction.response.send_message(
                "You do not have permission to reserve flight lead slots.",
                ephemeral=True,
            )
            return

        ensure_user_record(
            discord_id=str(interaction.user.id),
            discord_username=str(interaction.user),
            display_name=interaction.user.display_name,
        )

        timezone_name = get_user_timezone(str(interaction.user.id))

        events = get_upcoming_flightlead_events(
            discord_id=str(interaction.user.id),
            days=7,
        )

        await interaction.response.send_message(
            embed=build_event_page_embed(events, timezone_name),
            view=FlightLeadEventPage(
                owner_id=interaction.user.id,
                timezone_name=timezone_name,
                events=events,
                selected_event_id=None,
            ),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(FlightLeadCog(bot))
