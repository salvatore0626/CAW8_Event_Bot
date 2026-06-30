from __future__ import annotations

from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands
from services.permission_service import (
    require_mission_qualified_command,
    member_is_admin,
)

try:
    from config import MISSION_QUALIFIED_ROLE
except ImportError:
    MISSION_QUALIFIED_ROLE = 0

from services.attend_service import (
    AttendSubmission,
    ensure_user_record,
    find_flight_index_for_slot,
    flight_availabilities_for_open_op,
    format_timestamp_short,
    get_current_open_op,
    get_existing_user_attendance,
    get_flight_by_index,
    get_open_op_by_id,
    get_user_timezone,
    slot_availabilities_for_flight,
    submit_attendance,
)
from services.situation_room_service import queue_situation_room_refresh



LANDING_TYPES = [
    "Arrested",
    "Vertical",
    "Airfield",
    "Non-Pilot",
    "DNF",
]


@dataclass
class AttendDraft:
    event_id: int
    op_name: str
    op_type: str
    scheduled_at: int

    flight_index: int | None = None
    slot: str | None = None
    aircraft: str | None = None

    combat_deaths: int | None = None
    landing_type: str | None = None
    wires: int | None = None
    bolters: int | None = None

    flight_lead_rating: int | None = None
    op_remarks: str | None = None
    fl_remarks: str | None = None
    note_remarks: str | None = None


def has_mission_qualified_role(member: discord.Member) -> bool:
    if member_is_admin(member):
        return True
    if not MISSION_QUALIFIED_ROLE:
        return True

    try:
        role_id = int(MISSION_QUALIFIED_ROLE)
    except Exception:
        return True

    return any(role.id == role_id for role in member.roles)


def stars_for_rating(value: int) -> str:
    if value <= 0:
        return "☆☆☆☆☆"

    return "★" * value + "☆" * (5 - value)


ANSI_RESET = "\u001b[0m"
ANSI_GREEN = "\u001b[32m"
ANSI_RED = "\u001b[31m"


def ansi_line(label: str, value: str, valid: bool) -> str:
    color = ANSI_GREEN if valid else ANSI_RED
    return f"{color}{label}: {value}{ANSI_RESET}"


def ansi_block(lines: list[str]) -> str:
    return "```ansi\n" + "\n".join(lines) + "\n```"


def page_two_status_lines(draft: AttendDraft) -> list[str]:
    combat_valid = draft.combat_deaths is not None
    landing_valid = draft.landing_type is not None

    if draft.landing_type == "Arrested":
        wire_valid = draft.wires is not None
        bolter_valid = draft.bolters is not None
        wire_value = str(draft.wires) if wire_valid else "Select wire"
        bolter_value = str(draft.bolters) if bolter_valid else "Select bolters"
    elif draft.landing_type is None:
        wire_valid = False
        bolter_valid = False
        wire_value = "Select landing type first"
        bolter_value = "Select landing type first"
    else:
        wire_valid = True
        bolter_valid = True
        wire_value = "N/A"
        bolter_value = "N/A"

    return [
        ansi_line(
            "Combat Deaths",
            str(draft.combat_deaths) if combat_valid else "Select combat deaths",
            combat_valid,
        ),
        ansi_line(
            "Landing Type",
            draft.landing_type if landing_valid else "Select landing type",
            landing_valid,
        ),
        ansi_line("Wire", wire_value, wire_valid),
        ansi_line("Bolters", bolter_value, bolter_valid),
    ]


def is_page_two_complete(draft: AttendDraft) -> bool:
    if draft.combat_deaths is None:
        return False

    if draft.landing_type is None:
        return False

    if draft.landing_type == "Arrested":
        return draft.wires is not None and draft.bolters is not None

    return True





def apply_nonpilot_rules(draft: AttendDraft) -> None:
    if draft.landing_type == "Non-Pilot":
        draft.combat_deaths = 0
        draft.wires = None
        draft.bolters = None

def draft_selected_flight(draft: AttendDraft):
    if draft.flight_index is None:
        return None

    return get_flight_by_index(
        event_id=draft.event_id,
        flight_index=draft.flight_index,
    )


def build_page_one_embed(
    *,
    draft: AttendDraft,
    timezone_name: str,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"Attend #{draft.event_id} {draft.op_name}",
        description=(
            f"**When:** {format_timestamp_short(draft.scheduled_at, timezone_name)} / <t:{draft.scheduled_at}:R>\n\n"
            "Select the flight and slot you were in."
        ),
    )

    selected_flight = draft_selected_flight(draft)

    if selected_flight is not None:
        embed.add_field(
            name="Selected Flight",
            value=(
                f"{selected_flight.flight_name}\n"
                f"Airframe: {selected_flight.aircraft_count}x {selected_flight.aircraft or 'Unknown'}\n"
                f"Slots: {selected_flight.slot_count}x players"
            ),
            inline=False,
        )

    embed.set_footer(text=f"Displayed in your timezone: {timezone_name}")

    return embed


def build_page_two_embed(
    *,
    draft: AttendDraft,
    timezone_name: str,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"Attend #{draft.event_id} {draft.op_name}",
        description=(
            f"**Flight Slot:** {draft.slot or 'Not selected'}\n"
            f"**Aircraft:** {draft.aircraft or 'Unknown'}\n\n"
            f"{ansi_block(page_two_status_lines(draft))}"
        ),
    )

    embed.set_footer(text=f"Displayed in your timezone: {timezone_name}")

    return embed


def build_page_three_embed(
    *,
    draft: AttendDraft,
    timezone_name: str,
) -> discord.Embed:
    rating = "No rating"

    if draft.flight_lead_rating is not None:
        rating = f"{draft.flight_lead_rating}/5 {stars_for_rating(draft.flight_lead_rating)}"

    remarks_status = [
        "Op review: set" if draft.op_remarks else "Op review: blank",
        "Flight lead review: set" if draft.fl_remarks else "Flight lead review: blank",
        "Notes: set" if draft.note_remarks else "Notes: blank",
    ]

    embed = discord.Embed(
        title=f"Attend #{draft.event_id} {draft.op_name}",
        description=(
            f"**Flight Slot:** {draft.slot or 'Not selected'}\n"
            f"**Aircraft:** {draft.aircraft or 'Unknown'}\n\n"
            f"Flight Lead Rating: `{rating}`\n"
            + ("⚠️ 0-2 star ratings require a Flight lead review remark.\n" if draft.flight_lead_rating is not None and draft.flight_lead_rating <= 2 and not draft.fl_remarks else "")
            + "\n".join(remarks_status)
        ),
    )

    embed.set_footer(text=f"Displayed in your timezone: {timezone_name}")

    return embed


class AttendBaseView(discord.ui.View):
    def __init__(self, owner_id: int, timezone_name: str, draft: AttendDraft):
        super().__init__(timeout=1800)
        self.owner_id = owner_id
        self.timezone_name = timezone_name
        self.draft = draft

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the person who opened /attend can use these controls.",
                ephemeral=True,
            )
            return False

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This command can only be used inside the server.",
                ephemeral=True,
            )
            return False

        if not has_mission_qualified_role(interaction.user):
            await interaction.response.send_message(
                "You must be mission qualified to attend an op.",
                ephemeral=True,
            )
            return False

        return True


class ExistingAttendanceView(discord.ui.View):
    def __init__(self, owner_id: int, timezone_name: str, draft: AttendDraft):
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.timezone_name = timezone_name
        self.draft = draft

        self.add_item(CancelButton(row=0))
        self.add_item(EditAttendanceButton(row=0))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the person who opened /attend can use these controls.",
                ephemeral=True,
            )
            return False

        return True


class EditAttendanceButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(
            label="Edit",
            style=discord.ButtonStyle.success,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, ExistingAttendanceView)

        await interaction.response.edit_message(
            embed=build_page_one_embed(
                draft=self.view.draft,
                timezone_name=self.view.timezone_name,
            ),
            view=AttendPageOneView(
                owner_id=self.view.owner_id,
                timezone_name=self.view.timezone_name,
                draft=self.view.draft,
            ),
        )


class AttendPageOneView(AttendBaseView):
    def __init__(self, owner_id: int, timezone_name: str, draft: AttendDraft):
        super().__init__(owner_id, timezone_name, draft)

        self.add_item(FlightSelect(draft, owner_id))
        self.add_item(SlotSelect(draft, owner_id))
        self.add_item(CancelButton(row=2))
        self.add_item(NextToPageTwoButton(draft, row=2))

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=build_page_one_embed(
                draft=self.draft,
                timezone_name=self.timezone_name,
            ),
            view=AttendPageOneView(
                owner_id=self.owner_id,
                timezone_name=self.timezone_name,
                draft=self.draft,
            ),
        )


class FlightSelect(discord.ui.Select):
    def __init__(self, draft: AttendDraft, owner_id: int):
        availability_rows = flight_availabilities_for_open_op(
            event_id=draft.event_id,
            discord_id=str(owner_id),
        )

        if availability_rows:
            options = [
                discord.SelectOption(
                    label=row.label[:100],
                    value=str(row.flight.flight_index),
                    description=(
                        f"{row.flight.aircraft_count}x {row.flight.aircraft or 'Unknown'} | "
                        f"{row.flight.slot_count} player slots"
                    )[:100],
                    default=draft.flight_index == row.flight.flight_index,
                )
                for row in availability_rows[:25]
            ]
            disabled = False
        else:
            options = [
                discord.SelectOption(
                    label="No flights available",
                    value="-1",
                    description="All flights are full.",
                )
            ]
            disabled = True

        super().__init__(
            placeholder="Flight Select",
            min_values=1,
            max_values=1,
            options=options,
            disabled=disabled,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, AttendPageOneView)

        self.view.draft.flight_index = int(self.values[0])
        flight = draft_selected_flight(self.view.draft)

        self.view.draft.slot = None
        self.view.draft.aircraft = flight.aircraft if flight else None

        await self.view.refresh(interaction)


class SlotSelect(discord.ui.Select):
    def __init__(self, draft: AttendDraft, owner_id: int):
        flight = draft_selected_flight(draft)

        if flight is None:
            options = [
                discord.SelectOption(
                    label="Select a flight first",
                    value="none",
                    description="Pick your flight before selecting a slot.",
                )
            ]
            disabled = True
        else:
            availability_rows = slot_availabilities_for_flight(
                event_id=draft.event_id,
                flight=flight,
                discord_id=str(owner_id),
            )

            if draft.slot and all(row.slot != draft.slot for row in availability_rows):
                availability_rows.append(
                    type("ManualSlotAvailability", (), {
                        "slot": draft.slot,
                        "aircraft": draft.aircraft,
                        "label": f"{draft.slot} (current)",
                    })()
                )

            if availability_rows:
                options = [
                    discord.SelectOption(
                        label=row.label[:100],
                        value=row.slot,
                        description=(row.aircraft or flight.aircraft or "Unknown")[:100],
                        default=draft.slot == row.slot,
                    )
                    for row in availability_rows[:25]
                ]
                disabled = False
            else:
                options = [
                    discord.SelectOption(
                        label="No slots available",
                        value="none",
                        description="Every slot in this flight is full.",
                    )
                ]
                disabled = True

        super().__init__(
            placeholder="Slot Select",
            min_values=1,
            max_values=1,
            options=options,
            disabled=disabled,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, AttendPageOneView)

        self.view.draft.slot = self.values[0]
        flight = draft_selected_flight(self.view.draft)

        if flight is not None:
            self.view.draft.aircraft = flight.aircraft

        await self.view.refresh(interaction)


class NextToPageTwoButton(discord.ui.Button):
    def __init__(self, draft: AttendDraft, row: int):
        enabled = bool(draft.flight_index is not None and draft.slot)

        super().__init__(
            label="Next",
            style=discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary,
            disabled=not enabled,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, AttendPageOneView)

        await interaction.response.edit_message(
            embed=build_page_two_embed(
                draft=self.view.draft,
                timezone_name=self.view.timezone_name,
            ),
            view=AttendPageTwoView(
                owner_id=self.view.owner_id,
                timezone_name=self.view.timezone_name,
                draft=self.view.draft,
            ),
        )


class AttendPageTwoView(AttendBaseView):
    def __init__(self, owner_id: int, timezone_name: str, draft: AttendDraft):
        super().__init__(owner_id, timezone_name, draft)
        apply_nonpilot_rules(self.draft)

        self.add_item(CombatDeathsSelect(draft))
        self.add_item(LandingTypeSelect(draft))
        self.add_item(WireSelect(draft))
        self.add_item(BoltersSelect(draft))
        self.add_item(BackToPageOneButton(row=4))
        self.add_item(CancelButton(row=4))
        self.add_item(NextToPageThreeButton(row=4))

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=build_page_two_embed(
                draft=self.draft,
                timezone_name=self.timezone_name,
            ),
            view=AttendPageTwoView(
                owner_id=self.owner_id,
                timezone_name=self.timezone_name,
                draft=self.draft,
            ),
        )


class NumberSelect(discord.ui.Select):
    def make_number_options(
        self,
        *,
        selected: int | None,
        start: int,
        end: int,
        label_builder,
    ) -> list[discord.SelectOption]:
        return [
            discord.SelectOption(
                label=label_builder(number),
                value=str(number),
                default=selected == number,
            )
            for number in range(start, end + 1)
        ]



class CombatDeathsSelect(NumberSelect):
    def __init__(self, draft: AttendDraft):
        locked = draft.landing_type == "Non-Pilot"

        if locked:
            options = [
                discord.SelectOption(
                    label="Combat Deaths: 0",
                    value="0",
                    description="Locked to 0 for Non-Pilot attendance.",
                    default=True,
                )
            ]
            placeholder = "Combat deaths locked to 0"
        else:
            options = self.make_number_options(
                selected=draft.combat_deaths,
                start=0,
                end=24,
                label_builder=lambda number: f"Combat Deaths: {number}",
            )
            placeholder = "Select combat deaths"

        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
            disabled=locked,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, AttendPageTwoView)

        if self.view.draft.landing_type == "Non-Pilot":
            self.view.draft.combat_deaths = 0
            await self.view.refresh(interaction)
            return

        self.view.draft.combat_deaths = int(self.values[0])
        await self.view.refresh(interaction)



class LandingTypeSelect(discord.ui.Select):
    def __init__(self, draft: AttendDraft):
        options = [
            discord.SelectOption(
                label=value,
                value=value,
                default=draft.landing_type == value,
            )
            for value in LANDING_TYPES
        ]

        super().__init__(
            placeholder="Select landing type",
            min_values=1,
            max_values=1,
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, AttendPageTwoView)

        previous_landing_type = self.view.draft.landing_type
        self.view.draft.landing_type = self.values[0]

        if self.view.draft.landing_type == "Non-Pilot":
            apply_nonpilot_rules(self.view.draft)
        elif previous_landing_type == "Non-Pilot":
            self.view.draft.combat_deaths = None
            self.view.draft.wires = None
            self.view.draft.bolters = None
        elif self.view.draft.landing_type != "Arrested":
            self.view.draft.wires = None
            self.view.draft.bolters = None
        elif previous_landing_type != "Arrested":
            # Do not auto-fill. User must intentionally pick wire and bolters.
            self.view.draft.wires = None
            self.view.draft.bolters = None

        await self.view.refresh(interaction)


class WireSelect(NumberSelect):
    def __init__(self, draft: AttendDraft):
        disabled = draft.landing_type != "Arrested"

        if disabled:
            if draft.landing_type is None:
                label = "Select Arrested landing first"
            else:
                label = "N/A - not required"

            options = [
                discord.SelectOption(
                    label=label,
                    value="none",
                    description="Wire is only used for Arrested landings.",
                    default=True,
                )
            ]
        else:
            options = self.make_number_options(
                selected=draft.wires,
                start=1,
                end=4,
                label_builder=lambda number: f"{number} wire",
            )

        super().__init__(
            placeholder="Select wire",
            min_values=1,
            max_values=1,
            options=options,
            disabled=disabled,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, AttendPageTwoView)

        if self.values[0] == "none":
            return

        self.view.draft.wires = int(self.values[0])
        await self.view.refresh(interaction)


class BoltersSelect(NumberSelect):
    def __init__(self, draft: AttendDraft):
        disabled = draft.landing_type != "Arrested"

        if disabled:
            if draft.landing_type is None:
                label = "Select Arrested landing first"
            else:
                label = "N/A - not required"

            options = [
                discord.SelectOption(
                    label=label,
                    value="none",
                    description="Bolters are only used for Arrested landings.",
                    default=True,
                )
            ]
        else:
            options = self.make_number_options(
                selected=draft.bolters,
                start=0,
                end=24,
                label_builder=lambda number: f"Bolters: {number}",
            )

        super().__init__(
            placeholder="Select bolters",
            min_values=1,
            max_values=1,
            options=options,
            disabled=disabled,
            row=3,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, AttendPageTwoView)

        if self.values[0] == "none":
            return

        self.view.draft.bolters = int(self.values[0])
        await self.view.refresh(interaction)


class BackToPageOneButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(
            label="Back",
            style=discord.ButtonStyle.secondary,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, AttendPageTwoView)

        await interaction.response.edit_message(
            embed=build_page_one_embed(
                draft=self.view.draft,
                timezone_name=self.view.timezone_name,
            ),
            view=AttendPageOneView(
                owner_id=self.view.owner_id,
                timezone_name=self.view.timezone_name,
                draft=self.view.draft,
            ),
        )


class NextToPageThreeButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(
            label="Next",
            style=discord.ButtonStyle.success,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, AttendPageTwoView)

        if not is_page_two_complete(self.view.draft):
            await interaction.response.send_message(
                "Finish Combat Deaths and Landing Type first. "
                "If Landing Type is Arrested, also select Wire and Bolters.",
                ephemeral=True,
            )
            return

        await interaction.response.edit_message(
            embed=build_page_three_embed(
                draft=self.view.draft,
                timezone_name=self.view.timezone_name,
            ),
            view=AttendPageThreeView(
                owner_id=self.view.owner_id,
                timezone_name=self.view.timezone_name,
                draft=self.view.draft,
            ),
        )


class AttendPageThreeView(AttendBaseView):
    def __init__(self, owner_id: int, timezone_name: str, draft: AttendDraft):
        super().__init__(owner_id, timezone_name, draft)

        self.add_item(FlightLeadRatingSelect(draft))
        self.add_item(RemarksButton(row=1))
        self.add_item(BackToPageTwoButton(row=2))
        self.add_item(CancelButton(row=2))
        self.add_item(SubmitAttendanceButton(row=2))

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=build_page_three_embed(
                draft=self.draft,
                timezone_name=self.timezone_name,
            ),
            view=AttendPageThreeView(
                owner_id=self.owner_id,
                timezone_name=self.timezone_name,
                draft=self.draft,
            ),
        )


class FlightLeadRatingSelect(discord.ui.Select):
    def __init__(self, draft: AttendDraft):
        options = [
            discord.SelectOption(
                label="No rating",
                value="none",
                description="Leave flight lead rating blank",
                default=draft.flight_lead_rating is None,
            )
        ]

        for value in range(0, 6):
            options.append(
                discord.SelectOption(
                    label=f"{value}/5 {stars_for_rating(value)}",
                    value=str(value),
                    default=draft.flight_lead_rating == value,
                )
            )

        super().__init__(
            placeholder="Flight Lead Rating",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, AttendPageThreeView)

        if self.values[0] == "none":
            self.view.draft.flight_lead_rating = None
        else:
            self.view.draft.flight_lead_rating = int(self.values[0])

        await self.view.refresh(interaction)


class RemarksButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(
            label="Remarks",
            style=discord.ButtonStyle.primary,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, AttendPageThreeView)

        await interaction.response.send_modal(
            RemarksModal(self.view)
        )


class RemarksModal(discord.ui.Modal):
    def __init__(self, parent_view: AttendPageThreeView):
        super().__init__(title="Attendance Remarks")
        self.parent_view = parent_view

        self.op_review = discord.ui.TextInput(
            label="Op review",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=1000,
            default=parent_view.draft.op_remarks or "",
        )

        self.flight_lead_review = discord.ui.TextInput(
            label="Flight lead review",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=1000,
            default=parent_view.draft.fl_remarks or "",
        )

        self.notes = discord.ui.TextInput(
            label="Notes",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=1000,
            default=parent_view.draft.note_remarks or "",
        )

        self.add_item(self.op_review)
        self.add_item(self.flight_lead_review)
        self.add_item(self.notes)

    async def on_submit(self, interaction: discord.Interaction):
        self.parent_view.draft.op_remarks = str(self.op_review.value or "").strip() or None
        self.parent_view.draft.fl_remarks = str(self.flight_lead_review.value or "").strip() or None
        self.parent_view.draft.note_remarks = str(self.notes.value or "").strip() or None

        await interaction.response.edit_message(
            embed=build_page_three_embed(
                draft=self.parent_view.draft,
                timezone_name=self.parent_view.timezone_name,
            ),
            view=AttendPageThreeView(
                owner_id=self.parent_view.owner_id,
                timezone_name=self.parent_view.timezone_name,
                draft=self.parent_view.draft,
            ),
        )


class BackToPageTwoButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(
            label="Back",
            style=discord.ButtonStyle.secondary,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, AttendPageThreeView)

        await interaction.response.edit_message(
            embed=build_page_two_embed(
                draft=self.view.draft,
                timezone_name=self.view.timezone_name,
            ),
            view=AttendPageTwoView(
                owner_id=self.view.owner_id,
                timezone_name=self.view.timezone_name,
                draft=self.view.draft,
            ),
        )


class SubmitAttendanceButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(
            label="Submit",
            style=discord.ButtonStyle.success,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, AttendPageThreeView)

        open_op = get_open_op_by_id(self.view.draft.event_id)

        if open_op is None:
            await interaction.response.send_message(
                "That op is no longer open.",
                ephemeral=True,
            )
            return

        if self.view.draft.flight_index is None or not self.view.draft.slot:
            await interaction.response.send_message(
                "Go back and select your flight/slot first.",
                ephemeral=True,
            )
            return

        if not is_page_two_complete(self.view.draft):
            await interaction.response.send_message(
                "Go back and finish Combat Deaths and Landing Type first. "
                "If Landing Type is Arrested, also select Wire and Bolters.",
                ephemeral=True,
            )
            return

        if self.view.draft.landing_type == "Non-Pilot":
            apply_nonpilot_rules(self.view.draft)
        elif self.view.draft.landing_type != "Arrested":
            self.view.draft.wires = None
            self.view.draft.bolters = None

        if (
            self.view.draft.flight_lead_rating is not None
            and self.view.draft.flight_lead_rating <= 2
            and not self.view.draft.fl_remarks
        ):
            await interaction.response.send_message(
                "A 0-2 star flight lead rating requires a Flight lead review remark. "
                "Press **Remarks** and fill in the Flight lead review box.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        try:
            entry_id = submit_attendance(
                submission=AttendSubmission(
                    scheduled_op_id=self.view.draft.event_id,
                    op_template_name=self.view.draft.op_name,
                    discord_id=str(interaction.user.id),
                    user_name=str(interaction.user),
                    slot=self.view.draft.slot,
                    aircraft=self.view.draft.aircraft,
                    combat_deaths=self.view.draft.combat_deaths,
                    landing_type=self.view.draft.landing_type,
                    wires=self.view.draft.wires,
                    bolters=self.view.draft.bolters,
                    flight_lead_rating=self.view.draft.flight_lead_rating,
                    op_remarks=self.view.draft.op_remarks,
                    fl_remarks=self.view.draft.fl_remarks,
                    note_remarks=self.view.draft.note_remarks,
                ),
                flight_index=self.view.draft.flight_index,
            )
        except Exception as error:
            await interaction.followup.send(
                f"Failed to submit attendance: `{error}`",
                ephemeral=True,
            )
            return

        queue_situation_room_refresh(
            interaction.client,
            reason="attendance submitted",
        )

        await interaction.edit_original_response(
            embed=discord.Embed(
                title="Attendance Submitted",
                description=(
                    f"Your attendance was submitted for **{self.view.draft.op_name}**.\n"
                    f"Attendance Entry ID: `{entry_id}`"
                ),
            ),
            view=None,
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
                title="Attendance Closed",
                description="No attendance was submitted from this message.",
            ),
            view=None,
        )


def draft_from_existing(open_op, existing: dict) -> AttendDraft:
    draft = AttendDraft(
        event_id=open_op.event_id,
        op_name=open_op.op_name,
        op_type=open_op.op_type,
        scheduled_at=open_op.scheduled_at,
    )

    draft.slot = existing.get("slot")
    draft.aircraft = existing.get("aircraft")
    draft.flight_index = find_flight_index_for_slot(
        event_id=open_op.event_id,
        slot=draft.slot,
    )
    combat_deaths = existing.get("combat_deaths")
    draft.combat_deaths = int(combat_deaths) if combat_deaths is not None else None
    draft.landing_type = existing.get("landing_type") or None

    if draft.landing_type == "Arrested":
        wires = existing.get("wires")
        bolters = existing.get("bolters")
        draft.wires = int(wires) if wires is not None else None
        draft.bolters = int(bolters) if bolters is not None else None
    else:
        draft.wires = None
        draft.bolters = None

    draft.flight_lead_rating = existing.get("flight_lead_rating")
    draft.op_remarks = existing.get("op_remarks")
    draft.fl_remarks = existing.get("fl_remarks")
    draft.note_remarks = existing.get("note_remarks")

    return draft


class AttendCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="attend",
        description="Submit attendance for the currently open op.",
    )
    @app_commands.guild_only()
    async def attend_command(
        self,
        interaction: discord.Interaction,
    ):
        if not await require_mission_qualified_command(interaction):
            return
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This command can only be used inside the server.",
                ephemeral=True,
            )
            return

        if not has_mission_qualified_role(interaction.user):
            await interaction.response.send_message(
                "You must be mission qualified to attend an op.",
                ephemeral=True,
            )
            return

        open_op = get_current_open_op()

        if open_op is None:
            await interaction.response.send_message(
                "There is no op currently open for attendance.",
                ephemeral=True,
            )
            return

        ensure_user_record(
            discord_id=str(interaction.user.id),
            discord_username=str(interaction.user),
            display_name=interaction.user.display_name,
        )

        timezone_name = get_user_timezone(str(interaction.user.id))

        existing = get_existing_user_attendance(
            event_id=open_op.event_id,
            discord_id=str(interaction.user.id),
        )

        if existing:
            draft = draft_from_existing(open_op, existing)

            await interaction.response.send_message(
                content=f"You already attend #{open_op.event_id} {open_op.op_name}",
                view=ExistingAttendanceView(
                    owner_id=interaction.user.id,
                    timezone_name=timezone_name,
                    draft=draft,
                ),
                ephemeral=True,
            )
            return

        draft = AttendDraft(
            event_id=open_op.event_id,
            op_name=open_op.op_name,
            op_type=open_op.op_type,
            scheduled_at=open_op.scheduled_at,
        )

        await interaction.response.send_message(
            embed=build_page_one_embed(
                draft=draft,
                timezone_name=timezone_name,
            ),
            view=AttendPageOneView(
                owner_id=interaction.user.id,
                timezone_name=timezone_name,
                draft=draft,
            ),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(AttendCog(bot))
