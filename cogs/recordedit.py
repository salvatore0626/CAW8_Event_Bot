from __future__ import annotations

from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands
from services.permission_service import (
    require_admin_command,
    member_is_admin,
)

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

try:
    from config import INSTRUCTOR_ROLE
except ImportError:
    INSTRUCTOR_ROLE = 0

from services.recordedit_service import (
    AttendanceRecord,
    all_slot_options_for_event,
    delete_attendance_record,
    fmt_ts,
    format_timestamp_short,
    get_attendance_record,
    get_attendance_records,
    get_recordedit_op,
    get_user_timezone,
    max_seats_for_aircraft,
    parse_event_id,
    record_has_validation_error,
    record_validation_warnings,
    recordedit_op_option_label,
    recordedit_op_option_description,
    search_recordedit_ops,
    selected_slot_aircraft,
    update_attendance_record,
    validate_records,
)
from services.reward_service import queue_reward_reconciliation

from services.situation_room_service import queue_situation_room_refresh



ANSI_RESET = "\u001b[0m"
ANSI_RED = "\u001b[31m"
ANSI_BLUE = "\u001b[34m"


LANDING_TYPES = [
    "Arrested",
    "Vertical",
    "Airfield",
    "Non-Pilot",
    "DNF",
]

ATTEND_TYPES = [
    "normal",
    "late",
    "archive",
    "manual",
]


@dataclass

class EditDraft:
    entry_id: int
    event_id: int
    discord_id: str | None
    user_name: str | None
    slot: str | None
    aircraft: str | None
    combat_deaths: int | None
    landing_type: str | None
    wires: int | None
    bolters: int | None
    attend_type: str | None
    original_values: tuple | None = None

    def __post_init__(self):
        if self.original_values is None:
            self.original_values = self.snapshot()

    def snapshot(self) -> tuple:
        return (
            self.discord_id,
            self.slot,
            self.aircraft,
            self.combat_deaths,
            self.landing_type,
            self.wires,
            self.bolters,
            self.attend_type,
        )

    def has_changes(self) -> bool:
        return self.snapshot() != self.original_values


def configured_role_ids() -> set[int]:
    role_ids: set[int] = set()

    for value in [STAFF_ROLE, MISSION_EXECUTER_ROLE, INSTRUCTOR_ROLE]:
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


def has_recordedit_permission(member: discord.Member) -> bool:
    if member_is_admin(member):
        return True
    role_ids = configured_role_ids()

    if not role_ids:
        return True

    return any(role.id in role_ids for role in member.roles)


async def recordedit_op_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    timezone_name = get_user_timezone(str(interaction.user.id))
    ops = search_recordedit_ops(query=current, limit=25)

    return [
        app_commands.Choice(
            name=recordedit_op_option_label(op, timezone_name),
            value=str(op.event_id),
        )
        for op in ops[:25]
    ]


def safe_text(value, fallback: str = "—") -> str:
    text = str(value) if value is not None else ""
    text = text.strip()

    return text if text else fallback





def apply_nonpilot_rules(draft: EditDraft) -> None:
    if draft.landing_type == "Non-Pilot":
        draft.combat_deaths = 0
        draft.wires = None
        draft.bolters = None

def code_line_for_record(
    *,
    record: AttendanceRecord,
    selected: bool,
    errored: bool,
) -> str:
    marker = ">" if selected else " "
    index = record.entry_slot_index if record.entry_slot_index is not None else record.entry_id
    user_name = safe_text(record.user_name, "EMPTY")
    slot = safe_text(record.slot, "NO SLOT")
    landing_type = safe_text(record.landing_type, "—")

    line = f"{marker}#{index:<3} {user_name:<16.16} {slot:<18.18} {landing_type:<10.10}"

    if errored:
        return f"{ANSI_RED}{line}{ANSI_RESET}"

    if record.type == "manual":
        return f"{ANSI_BLUE}{line}{ANSI_RESET}"

    return line


def build_attendance_block(
    *,
    event_id: int,
    records: list[AttendanceRecord],
    selected_index: int,
) -> str:
    if not records:
        return "```ansi\nNo attendance records found for this op.\n```"

    validation = validate_records(event_id)
    total = len(records)
    window = 10
    half = window // 2

    start = max(0, selected_index - half)
    end = min(total, start + window)
    start = max(0, end - window)

    lines: list[str] = []

    for idx in range(start, end):
        record = records[idx]
        errored = record_has_validation_error(record, validation, event_id)

        lines.append(
            code_line_for_record(
                record=record,
                selected=idx == selected_index,
                errored=errored,
            )
        )

    return "```ansi\n" + "\n".join(lines) + "\n```"


def build_selected_record_details(
    *,
    record: AttendanceRecord | None,
    timezone_name: str,
    warnings: list[str] | None = None,
) -> str:
    if record is None:
        return "No record selected."

    warning_text = ""

    if warnings:
        warning_text = "\n\n**⚠️ Warnings:**\n" + "\n".join(
            f"- {warning}"
            for warning in warnings
        )

    return (
        f"**Entry ID:** `{record.entry_id}`\n"
        f"**Combat deaths:** `{record.combat_deaths if record.combat_deaths is not None else 'N/A'}`\n"
        f"**Landing type:** `{record.landing_type or 'N/A'}`\n"
        f"**Wire:** `{record.wires if record.wires is not None else 'N/A'}`\n"
        f"**Bolters:** `{record.bolters if record.bolters is not None else 'N/A'}`\n"
        f"**Logged Time:** `{fmt_ts(record.logged_at, timezone_name)}`\n"
        f"**Last Updated:** `{fmt_ts(record.updated_at, timezone_name)}`\n"
        f"**Attend_Type:** `{record.attend_type or 'N/A'}`"
        f"{warning_text}"
    )


def build_recordedit_embed(
    *,
    event_id: int,
    selected_index: int,
    timezone_name: str,
) -> discord.Embed:
    op = get_recordedit_op(event_id)
    records = get_attendance_records(event_id)

    if op is None:
        return discord.Embed(
            title="Record Edit",
            description="That op was not found or is not Open/Complete.",
        )

    selected_record = records[selected_index] if records and 0 <= selected_index < len(records) else None
    validation = validate_records(event_id)

    embed = discord.Embed(
        title=f"Record Edit #{op.event_id} {op.op_name}",
        description=(
            f"**Status:** {op.status}\n"
            f"**When:** {format_timestamp_short(op.scheduled_at, timezone_name)} / <t:{op.scheduled_at}:R>\n\n"
            f"{build_attendance_block(event_id=event_id, records=records, selected_index=selected_index)}"
        ),
    )

    selected_warnings = (
        record_validation_warnings(selected_record, validation)
        if selected_record is not None
        else []
    )

    embed.add_field(
        name="Selected Attend",
        value=build_selected_record_details(
            record=selected_record,
            timezone_name=timezone_name,
            warnings=selected_warnings,
        ),
        inline=False,
    )

    warnings: list[str] = []

    if validation.overfull_flights:
        warnings.append("Overfull flights: " + ", ".join(sorted(validation.overfull_flights)))

    if validation.overfull_slots:
        warnings.append("Overfull slots: " + ", ".join(sorted(validation.overfull_slots)))

    if validation.duplicate_discord_ids:
        warnings.append("Duplicate Discord IDs: " + ", ".join(sorted(validation.duplicate_discord_ids)))

    total_record_warnings = sum(len(items) for items in validation.record_warnings.values())
    if total_record_warnings:
        warnings.append(f"Record-level warnings: {total_record_warnings}")

    if warnings:
        embed.add_field(
            name="⚠️ Validation Warnings",
            value="\n".join(warnings)[:1000],
            inline=False,
        )

    embed.set_footer(text=f"Displayed in your timezone: {timezone_name}")

    return embed


async def safe_show_recordedit_view(
    *,
    interaction: discord.Interaction,
    owner_id: int,
    timezone_name: str,
    event_id: int,
    selected_index: int,
) -> None:
    try:
        embed = build_recordedit_embed(
            event_id=event_id,
            selected_index=selected_index,
            timezone_name=timezone_name,
        )
        view = RecordEditView(
            owner_id=owner_id,
            timezone_name=timezone_name,
            event_id=event_id,
            selected_index=selected_index,
        )
    except Exception as error:
        await interaction.edit_original_response(
            content=(
                "Failed to load that record edit view. "
                f"Error: `{type(error).__name__}: {error}`"
            ),
            embed=None,
            view=None,
        )
        return

    await interaction.edit_original_response(
        content=None,
        embed=embed,
        view=view,
    )


class RecordEditOpSelectView(discord.ui.View):
    def __init__(
        self,
        *,
        owner_id: int,
        timezone_name: str,
        selected_event_id: int | None = None,
    ):
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.timezone_name = timezone_name
        self.selected_event_id = selected_event_id

        self.add_item(OpSelect(timezone_name, selected_event_id))
        self.add_item(CancelButton(row=1))
        self.add_item(OpenSelectedOpButton(enabled=selected_event_id is not None, row=1))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the person who opened /recordedit can use these controls.",
                ephemeral=True,
            )
            return False

        return True


class OpSelect(discord.ui.Select):
    def __init__(self, timezone_name: str, selected_event_id: int | None):
        ops = search_recordedit_ops(limit=25)

        if ops:
            options = [
                discord.SelectOption(
                    label=recordedit_op_option_label(op, timezone_name),
                    value=str(op.event_id),
                    description=recordedit_op_option_description(op),
                    default=selected_event_id == op.event_id,
                )
                for op in ops[:25]
            ]
            disabled = False
        else:
            options = [
                discord.SelectOption(
                    label="No open/completed ops found",
                    value="0",
                    description="There is nothing to edit right now.",
                )
            ]
            disabled = True

        super().__init__(
            placeholder="Select Open/Completed Op",
            min_values=1,
            max_values=1,
            options=options,
            disabled=disabled,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, RecordEditOpSelectView)

        self.view.selected_event_id = int(self.values[0])

        await interaction.response.edit_message(
            embed=build_op_select_embed(self.view.timezone_name),
            view=RecordEditOpSelectView(
                owner_id=self.view.owner_id,
                timezone_name=self.view.timezone_name,
                selected_event_id=self.view.selected_event_id,
            ),
        )


class OpenSelectedOpButton(discord.ui.Button):
    def __init__(self, enabled: bool, row: int):
        super().__init__(
            label="Open",
            style=discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary,
            disabled=not enabled,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, RecordEditOpSelectView)

        if self.view.selected_event_id is None:
            await interaction.response.send_message(
                "Select an op first.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        await safe_show_recordedit_view(
            interaction=interaction,
            owner_id=self.view.owner_id,
            timezone_name=self.view.timezone_name,
            event_id=self.view.selected_event_id,
            selected_index=0,
        )


class RecordEditView(discord.ui.View):
    def __init__(
        self,
        *,
        owner_id: int,
        timezone_name: str,
        event_id: int,
        selected_index: int,
    ):
        super().__init__(timeout=1800)
        self.owner_id = owner_id
        self.timezone_name = timezone_name
        self.event_id = event_id
        self.selected_index = selected_index

        records = get_attendance_records(event_id)

        selected_record = records[selected_index] if records and 0 <= selected_index < len(records) else None
        selected_is_empty = bool(selected_record is not None and not selected_record.discord_id)

        self.add_item(PrevButton(disabled=selected_index <= 0, row=0))
        self.add_item(CancelButton(row=0))
        self.add_item(EditButton(disabled=not records, is_empty=selected_is_empty, row=0))
        self.add_item(DeleteButton(disabled=not records or selected_is_empty, is_empty=selected_is_empty, row=0))
        self.add_item(NextButton(disabled=selected_index >= len(records) - 1 or not records, row=0))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the person who opened /recordedit can use these controls.",
                ephemeral=True,
            )
            return False

        return True

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.defer()

        records = get_attendance_records(self.event_id)

        if not records:
            self.selected_index = 0
        else:
            self.selected_index = max(0, min(self.selected_index, len(records) - 1))

        await safe_show_recordedit_view(
            interaction=interaction,
            owner_id=self.owner_id,
            timezone_name=self.timezone_name,
            event_id=self.event_id,
            selected_index=self.selected_index,
        )


class PrevButton(discord.ui.Button):
    def __init__(self, disabled: bool, row: int):
        super().__init__(
            label="Prev",
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, RecordEditView)
        self.view.selected_index -= 1
        await self.view.refresh(interaction)


class NextButton(discord.ui.Button):
    def __init__(self, disabled: bool, row: int):
        super().__init__(
            label="Next",
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, RecordEditView)
        self.view.selected_index += 1
        await self.view.refresh(interaction)


class EditButton(discord.ui.Button):
    def __init__(self, disabled: bool, is_empty: bool, row: int):
        super().__init__(
            label="Add" if is_empty else "Edit",
            style=discord.ButtonStyle.success if is_empty else discord.ButtonStyle.primary,
            disabled=disabled,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, RecordEditView)

        records = get_attendance_records(self.view.event_id)

        if not records:
            await interaction.response.send_message(
                "There are no attendance records to edit.",
                ephemeral=True,
            )
            return

        record = records[self.view.selected_index]

        draft = EditDraft(
            entry_id=record.entry_id,
            event_id=self.view.event_id,
            discord_id=record.discord_id,
            user_name=record.user_name,
            slot=record.slot,
            aircraft=record.aircraft,
            combat_deaths=record.combat_deaths,
            landing_type=record.landing_type,
            wires=record.wires,
            bolters=record.bolters,
            attend_type=record.attend_type or "manual",
        )

        await interaction.response.edit_message(
            embed=build_edit_page_one_embed(draft),
            view=RecordEditEditPageOne(
                owner_id=self.view.owner_id,
                timezone_name=self.view.timezone_name,
                event_id=self.view.event_id,
                selected_index=self.view.selected_index,
                draft=draft,
            ),
        )


class DeleteButton(discord.ui.Button):
    def __init__(self, disabled: bool, is_empty: bool, row: int):
        super().__init__(
            label="Delete",
            style=discord.ButtonStyle.secondary if is_empty else discord.ButtonStyle.danger,
            disabled=disabled,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, RecordEditView)

        records = get_attendance_records(self.view.event_id)

        if not records:
            await interaction.response.send_message(
                "There are no attendance records to delete.",
                ephemeral=True,
            )
            return

        record = records[self.view.selected_index]

        await interaction.response.edit_message(
            embed=discord.Embed(
                title="Clear Attendance Row?",
                description=(
                    f"Clear entry `{record.entry_id}` back to a blank reusable row?\n\n"
                    f"User: `{record.user_name or 'EMPTY'}`\n"
                    f"Slot: `{record.slot or 'NO SLOT'}`\n"
                    f"Landing: `{record.landing_type or 'N/A'}`"
                ),
            ),
            view=ConfirmDeleteView(
                owner_id=self.view.owner_id,
                timezone_name=self.view.timezone_name,
                event_id=self.view.event_id,
                selected_index=self.view.selected_index,
                entry_id=record.entry_id,
            ),
        )


class ConfirmDeleteView(discord.ui.View):
    def __init__(
        self,
        *,
        owner_id: int,
        timezone_name: str,
        event_id: int,
        selected_index: int,
        entry_id: int,
    ):
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.timezone_name = timezone_name
        self.event_id = event_id
        self.selected_index = selected_index
        self.entry_id = entry_id

        self.add_item(DeleteCancelButton(row=0))
        self.add_item(ConfirmDeleteButton(row=0))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the person who opened /recordedit can use these controls.",
                ephemeral=True,
            )
            return False

        return True


class DeleteCancelButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, ConfirmDeleteView)

        await interaction.response.defer()

        await safe_show_recordedit_view(
            interaction=interaction,
            owner_id=self.view.owner_id,
            timezone_name=self.view.timezone_name,
            event_id=self.view.event_id,
            selected_index=self.view.selected_index,
        )


class ConfirmDeleteButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(
            label="Delete Attend",
            style=discord.ButtonStyle.danger,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, ConfirmDeleteView)

        await interaction.response.defer()

        delete_attendance_record(
            self.view.entry_id,
            performed_by_id=str(interaction.user.id),
        )

        queue_situation_room_refresh(
            interaction.client,
            reason="attendance record deleted",
        )
        queue_reward_reconciliation(
            interaction.client,
            reason="attendance record deleted",
        )

        await safe_show_recordedit_view(
            interaction=interaction,
            owner_id=self.view.owner_id,
            timezone_name=self.view.timezone_name,
            event_id=self.view.event_id,
            selected_index=self.view.selected_index,
        )


class RecordEditEditPageOne(discord.ui.View):
    def __init__(
        self,
        *,
        owner_id: int,
        timezone_name: str,
        event_id: int,
        selected_index: int,
        draft: EditDraft,
    ):
        super().__init__(timeout=1800)
        self.owner_id = owner_id
        self.timezone_name = timezone_name
        self.event_id = event_id
        self.selected_index = selected_index
        self.draft = draft

        self.add_item(EditDiscordIdButton(row=0))
        self.add_item(EditSlotSelect(draft))
        self.add_item(EditCancelButton(draft, row=2))
        self.add_item(EditBackToListButton(row=2))
        self.add_item(EditNextToPageTwoButton(row=2))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the person who opened /recordedit can use these controls.",
                ephemeral=True,
            )
            return False

        return True

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=build_edit_page_one_embed(self.draft),
            view=RecordEditEditPageOne(
                owner_id=self.owner_id,
                timezone_name=self.timezone_name,
                event_id=self.event_id,
                selected_index=self.selected_index,
                draft=self.draft,
            ),
        )


class EditDiscordIdButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(
            label="Discord ID",
            style=discord.ButtonStyle.primary,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, RecordEditEditPageOne)
        await interaction.response.send_modal(EditDiscordIdModal(self.view))


class EditDiscordIdModal(discord.ui.Modal):
    def __init__(self, parent_view: RecordEditEditPageOne):
        super().__init__(title="Edit Attendance Discord ID")
        self.parent_view = parent_view
        self.discord_id_input = discord.ui.TextInput(
            label="Discord ID",
            required=False,
            max_length=32,
            default=parent_view.draft.discord_id or "",
        )
        self.add_item(self.discord_id_input)

    async def on_submit(self, interaction: discord.Interaction):
        value = str(self.discord_id_input.value or "").strip()
        self.parent_view.draft.discord_id = value or None
        await self.parent_view.refresh(interaction)


class EditSlotSelect(discord.ui.Select):
    def __init__(self, draft: EditDraft):
        options_raw = all_slot_options_for_event(draft.event_id)

        if options_raw:
            options = [
                discord.SelectOption(
                    label=slot[:100],
                    value=slot,
                    description=(aircraft or flight_name or "Unknown")[:100],
                    default=draft.slot == slot,
                )
                for slot, aircraft, flight_name in options_raw[:25]
            ]
            disabled = False
        else:
            options = [
                discord.SelectOption(
                    label="No slots found",
                    value="none",
                    description="This op has no flight template slots.",
                )
            ]
            disabled = True

        super().__init__(
            placeholder="Slot",
            min_values=1,
            max_values=1,
            options=options,
            disabled=disabled,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, RecordEditEditPageOne)

        self.view.draft.slot = self.values[0]
        self.view.draft.aircraft = selected_slot_aircraft(
            self.view.draft.event_id,
            self.view.draft.slot,
        )

        await self.view.refresh(interaction)


class EditNextToPageTwoButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(
            label="Next",
            style=discord.ButtonStyle.success,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, RecordEditEditPageOne)

        errors = required_page_one_errors(self.view.draft)
        if errors:
            await interaction.response.send_message(
                "Finish the required fields first:\n" + "\n".join(f"- {error}" for error in errors),
                ephemeral=True,
            )
            return

        await interaction.response.edit_message(
            embed=build_edit_page_two_embed(self.view.draft),
            view=RecordEditEditPageTwo(
                owner_id=self.view.owner_id,
                timezone_name=self.view.timezone_name,
                event_id=self.view.event_id,
                selected_index=self.view.selected_index,
                draft=self.view.draft,
            ),
        )


class RecordEditEditPageTwo(discord.ui.View):
    def __init__(
        self,
        *,
        owner_id: int,
        timezone_name: str,
        event_id: int,
        selected_index: int,
        draft: EditDraft,
    ):
        super().__init__(timeout=1800)
        self.owner_id = owner_id
        self.timezone_name = timezone_name
        self.event_id = event_id
        self.selected_index = selected_index
        self.draft = draft
        apply_nonpilot_rules(self.draft)

        self.add_item(EditCombatDeathsSelect(draft))
        self.add_item(EditLandingTypeSelect(draft))
        self.add_item(EditWireSelect(draft))
        self.add_item(EditBoltersSelect(draft))
        self.add_item(EditCancelButton(draft, row=4))
        self.add_item(EditBackToPageOneButton(row=4))
        self.add_item(EditSaveButton(row=4))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the person who opened /recordedit can use these controls.",
                ephemeral=True,
            )
            return False

        return True

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=build_edit_page_two_embed(self.draft),
            view=RecordEditEditPageTwo(
                owner_id=self.owner_id,
                timezone_name=self.timezone_name,
                event_id=self.event_id,
                selected_index=self.selected_index,
                draft=self.draft,
            ),
        )



class NumberSelect(discord.ui.Select):
    def make_options(
        self,
        selected: int | None,
        start: int,
        end: int,
        label_builder=None,
    ) -> list[discord.SelectOption]:
        if label_builder is None:
            label_builder = lambda number: str(number)

        return [
            discord.SelectOption(
                label=str(label_builder(number))[:100],
                value=str(number),
                default=selected == number,
            )
            for number in range(start, end + 1)
        ]



class EditCombatDeathsSelect(NumberSelect):
    def __init__(self, draft: EditDraft):
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
            options = self.make_options(
                draft.combat_deaths,
                0,
                24,
                label_builder=lambda number: f"Combat Deaths: {number}",
            )
            placeholder = "Combat Deaths"

        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
            disabled=locked,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, RecordEditEditPageTwo)

        if self.view.draft.landing_type == "Non-Pilot":
            self.view.draft.combat_deaths = 0
            await self.view.refresh(interaction)
            return

        self.view.draft.combat_deaths = int(self.values[0])
        await self.view.refresh(interaction)



class EditLandingTypeSelect(discord.ui.Select):
    def __init__(self, draft: EditDraft):
        options = [
            discord.SelectOption(
                label=value,
                value=value,
                default=draft.landing_type == value,
            )
            for value in LANDING_TYPES
        ]

        super().__init__(
            placeholder="Landing Type",
            min_values=1,
            max_values=1,
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, RecordEditEditPageTwo)
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
            self.view.draft.wires = None
            self.view.draft.bolters = None

        await self.view.refresh(interaction)



class EditWireSelect(NumberSelect):
    def __init__(self, draft: EditDraft):
        disabled = draft.landing_type != "Arrested"

        if disabled:
            options = [
                discord.SelectOption(
                    label="N/A",
                    value="none",
                    default=True,
                )
            ]
        else:
            options = self.make_options(
                draft.wires,
                1,
                4,
                label_builder=lambda number: f"{number} Wire",
            )

        super().__init__(
            placeholder="Wire",
            min_values=1,
            max_values=1,
            options=options,
            disabled=disabled,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, RecordEditEditPageTwo)
        if self.values[0] == "none":
            return
        self.view.draft.wires = int(self.values[0])
        await self.view.refresh(interaction)



class EditBoltersSelect(NumberSelect):
    def __init__(self, draft: EditDraft):
        disabled = draft.landing_type != "Arrested"

        if disabled:
            options = [
                discord.SelectOption(
                    label="N/A",
                    value="none",
                    default=True,
                )
            ]
        else:
            options = self.make_options(
                draft.bolters,
                0,
                24,
                label_builder=lambda number: f"Bolters: {number}",
            )

        super().__init__(
            placeholder="Bolters",
            min_values=1,
            max_values=1,
            options=options,
            disabled=disabled,
            row=3,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, RecordEditEditPageTwo)
        if self.values[0] == "none":
            return
        self.view.draft.bolters = int(self.values[0])
        await self.view.refresh(interaction)


class EditBackToPageOneButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(
            label="Back",
            style=discord.ButtonStyle.secondary,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, RecordEditEditPageTwo)

        await interaction.response.edit_message(
            embed=build_edit_page_one_embed(self.view.draft),
            view=RecordEditEditPageOne(
                owner_id=self.view.owner_id,
                timezone_name=self.view.timezone_name,
                event_id=self.view.event_id,
                selected_index=self.view.selected_index,
                draft=self.view.draft,
            ),
        )


class EditBackToListButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(
            label="Back",
            style=discord.ButtonStyle.secondary,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, RecordEditEditPageOne)

        await interaction.response.edit_message(
            embed=build_recordedit_embed(
                event_id=self.view.event_id,
                selected_index=self.view.selected_index,
                timezone_name=self.view.timezone_name,
            ),
            view=RecordEditView(
                owner_id=self.view.owner_id,
                timezone_name=self.view.timezone_name,
                event_id=self.view.event_id,
                selected_index=self.view.selected_index,
            ),
        )


class EditSaveButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(
            label="Save",
            style=discord.ButtonStyle.success,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, RecordEditEditPageTwo)

        errors = required_save_errors(self.view.draft)
        if errors:
            await interaction.response.send_message(
                "Cannot save yet:\n" + "\n".join(f"- {error}" for error in errors),
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        if self.view.draft.landing_type == "Non-Pilot":
            apply_nonpilot_rules(self.view.draft)
        elif self.view.draft.landing_type != "Arrested":
            self.view.draft.wires = None
            self.view.draft.bolters = None

        update_attendance_record(
            entry_id=self.view.draft.entry_id,
            discord_id=self.view.draft.discord_id,
            slot=self.view.draft.slot,
            aircraft=self.view.draft.aircraft,
            combat_deaths=self.view.draft.combat_deaths,
            landing_type=self.view.draft.landing_type,
            wires=self.view.draft.wires,
            bolters=self.view.draft.bolters,
            attend_type=self.view.draft.attend_type,
            performed_by_id=str(interaction.user.id),
        )

        queue_situation_room_refresh(
            interaction.client,
            reason="attendance record edited",
        )
        queue_reward_reconciliation(
            interaction.client,
            reason="attendance record edited",
        )

        await safe_show_recordedit_view(
            interaction=interaction,
            owner_id=self.view.owner_id,
            timezone_name=self.view.timezone_name,
            event_id=self.view.event_id,
            selected_index=self.view.selected_index,
        )



class EditCancelButton(discord.ui.Button):
    def __init__(self, draft: EditDraft, row: int):
        has_changes = draft.has_changes()

        super().__init__(
            label="Cancel",
            style=discord.ButtonStyle.danger if has_changes else discord.ButtonStyle.secondary,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        view = self.view

        if not isinstance(view, (RecordEditEditPageOne, RecordEditEditPageTwo)):
            return

        await interaction.response.edit_message(
            embed=build_recordedit_embed(
                event_id=view.event_id,
                selected_index=view.selected_index,
                timezone_name=view.timezone_name,
            ),
            view=RecordEditView(
                owner_id=view.owner_id,
                timezone_name=view.timezone_name,
                event_id=view.event_id,
                selected_index=view.selected_index,
            ),
        )


class CancelButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(
            label="Exit",
            style=discord.ButtonStyle.secondary,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="Record Edit Closed",
                description="No more changes will be made from this message.",
            ),
            view=None,
        )


def build_op_select_embed(timezone_name: str) -> discord.Embed:
    return discord.Embed(
        title="Record Edit",
        description="Select an Open or Complete op to view/edit attendance records.",
    )


def required_page_one_errors(draft: EditDraft) -> list[str]:
    errors: list[str] = []

    if not draft.discord_id:
        errors.append("Discord ID is required.")

    if not draft.slot:
        errors.append("Slot is required.")

    return errors


def required_save_errors(draft: EditDraft) -> list[str]:
    errors = required_page_one_errors(draft)

    if draft.combat_deaths is None:
        errors.append("Combat deaths is required.")

    if not draft.landing_type:
        errors.append("Landing type is required.")

    if draft.landing_type == "Arrested":
        if draft.wires is None:
            errors.append("Wire is required for Arrested landings.")

        if draft.bolters is None:
            errors.append("Bolters is required for Arrested landings.")

    return errors


def build_edit_page_one_embed(draft: EditDraft) -> discord.Embed:
    return discord.Embed(
        title="Edit Attendance - Page 1",
        description=(
            f"**Entry ID:** `{draft.entry_id}`\n"
            f"**Discord ID:** `{draft.discord_id or 'NONE'}`\n"
            f"**Stored Username:** `{draft.user_name or 'EMPTY'}`\n"
            f"**Slot:** `{draft.slot or 'NO SLOT'}`\n"
            f"**Aircraft:** `{draft.aircraft or 'Unknown'}`\n\n"
            "Use **Discord ID** to change who this attendance belongs to. "
            "On save, the bot will look up that Discord ID in the `users` table and store the matching Discord username."
        ),
    )


def build_edit_page_two_embed(draft: EditDraft) -> discord.Embed:
    return discord.Embed(
        title="Edit Attendance - Page 2",
        description=(
            f"**Entry ID:** `{draft.entry_id}`\n"
            f"**Combat Deaths:** `{draft.combat_deaths if draft.combat_deaths is not None else 'N/A'}`\n"
            f"**Landing Type:** `{draft.landing_type or 'N/A'}`\n"
            f"**Wire:** `{draft.wires if draft.wires is not None else 'N/A'}`\n"
            f"**Bolters:** `{draft.bolters if draft.bolters is not None else 'N/A'}`\n\n"
            "Make changes, then press **Save**."
        ),
    )


class RecordEditCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def permission_check(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This command can only be used inside the server.",
                ephemeral=True,
            )
            return False

        if not has_recordedit_permission(interaction.user):
            await interaction.response.send_message(
                "You do not have permission to edit attendance records.",
                ephemeral=True,
            )
            return False

        return True

    @app_commands.command(
        name="recordedit",
        description="View, edit, or delete attendance records for open/completed ops.",
    )
    @app_commands.guild_only()
    @app_commands.autocomplete(opid=recordedit_op_autocomplete)
    async def recordedit_command(
        self,
        interaction: discord.Interaction,
        opid: str | None = None,
    ):
        if not await require_admin_command(interaction):
            return
        if not await self.permission_check(interaction):
            return

        timezone_name = get_user_timezone(str(interaction.user.id))
        event_id = parse_event_id(opid)

        if event_id is not None:
            op = get_recordedit_op(event_id)

            if op is None:
                await interaction.response.send_message(
                    "That op was not found, or it is not Open/Complete.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True)

            try:
                embed = build_recordedit_embed(
                    event_id=event_id,
                    selected_index=0,
                    timezone_name=timezone_name,
                )
                view = RecordEditView(
                    owner_id=interaction.user.id,
                    timezone_name=timezone_name,
                    event_id=event_id,
                    selected_index=0,
                )
            except Exception as error:
                await interaction.followup.send(
                    content=(
                        "Failed to load that record edit view. "
                        f"Error: `{type(error).__name__}: {error}`"
                    ),
                    ephemeral=True,
                )
                return

            await interaction.followup.send(
                embed=embed,
                view=view,
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=build_op_select_embed(timezone_name),
            view=RecordEditOpSelectView(
                owner_id=interaction.user.id,
                timezone_name=timezone_name,
            ),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(RecordEditCog(bot))
