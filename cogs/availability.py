from __future__ import annotations

from dataclasses import dataclass
import io

import discord
from discord import app_commands
from discord.ext import commands

from config import TIMEZONE_OPTIONS, TIME_OPTIONS
from services.availability_service import (
    DAY_NAMES,
    AvailabilityDay,
    build_availability_heatmap_report,
    get_availability_days,
    is_complete,
    minutes_to_time_label,
    normalize_windows,
    render_availability_overview_heatmap_png,
    TIMEZONE_REGIONS,
    reset_all_availability_to_pending,
    reset_user_availability,
    save_full_availability,
    window_to_text,
    windows_to_text,
)
from services.user_settings_service import get_user_settings, update_timezone, timezone_select_options
from services.permission_service import member_is_admin, mission_qualified_role_ids
from services.admin_log_service import log_admin_action


ANSI_RESET = "\u001b[0m"
ANSI_WHITE = "\u001b[37m"
ANSI_GREEN = "\u001b[1;32m"
ANSI_RED = "\u001b[1;31m"
ANSI_YELLOW = "\u001b[1;33m"


@dataclass
class WindowDraft:
    start: int | None = None
    end: int | None = None

    def complete_window(self) -> tuple[int, int] | None:
        if self.start is None or self.end is None:
            return None

        start = int(self.start)
        end = int(self.end)

        if end <= start:
            end += 1440

        if start < 0 or start > 1439 or end <= start or end > 2880:
            return None

        return (start, end)


def minute_from_time_value(value: str) -> int:
    hour_text, minute_text = value.split(":", 1)
    hour = int(hour_text)
    minute = int(minute_text)
    return hour * 60 + minute


def ampm_label(minutes: int) -> str:
    day_offset = minutes // 1440
    minute_of_day = minutes % 1440
    hour = minute_of_day // 60
    minute = minute_of_day % 60
    suffix = "AM" if hour < 12 else "PM"
    display_hour = hour % 12 or 12
    label = f"{display_hour}:{minute:02d} {suffix}"
    if day_offset:
        label += " +1 Day"
    return label


def military_label(minutes: int) -> str:
    minute_of_day = minutes % 1440
    hour = minute_of_day // 60
    minute = minute_of_day % 60
    return f"{hour:02d}:{minute:02d}"


def configured_time_minutes() -> list[int]:
    values: list[int] = []

    for _label, value in TIME_OPTIONS:
        minute = minute_from_time_value(value)
        if minute not in values:
            values.append(minute)

    return sorted(values)


def start_time_options(selected: int | None) -> list[discord.SelectOption]:
    options: list[discord.SelectOption] = []

    for minute in configured_time_minutes()[:25]:
        options.append(
            discord.SelectOption(
                label=military_label(minute),
                value=str(minute),
                description=ampm_label(minute),
                default=selected == minute,
            )
        )

    return options


def end_time_options(start: int | None, selected: int | None) -> list[discord.SelectOption]:
    if start is None:
        return [
            discord.SelectOption(
                label="Pick start time first",
                value="none",
                description="End time unlocks after start time is selected.",
            )
        ]

    start = int(start)
    base_times = configured_time_minutes()
    ordered: list[int] = []

    # Start the dropdown at one hour after the selected start time, then wrap
    # through the next day. Values are absolute minutes so +1 day is preserved.
    for offset in range(60, 1441, 60):
        candidate = start + offset
        candidate_of_day = candidate % 1440
        if candidate_of_day in base_times:
            ordered.append(candidate)

    # Fallback for non-hour TIME_OPTIONS, if ever configured later.
    if not ordered:
        for minute in base_times:
            candidate = minute if minute > start else minute + 1440
            ordered.append(candidate)
        ordered.sort()

    options: list[discord.SelectOption] = []
    seen: set[int] = set()

    for minute in ordered[:25]:
        if minute in seen:
            continue
        seen.add(minute)
        options.append(
            discord.SelectOption(
                label=military_label(minute),
                value=str(minute),
                description=ampm_label(minute),
                default=selected == minute,
            )
        )

    return options


def timezone_options_with_default(selected: str | None) -> list[discord.SelectOption]:
    return timezone_select_options(TIMEZONE_OPTIONS, selected)


def ansi_line(text: str, color: str = ANSI_WHITE) -> str:
    return f"{color}{text}{ANSI_RESET}"


def make_ansi_block(lines: list[str]) -> str:
    return "```ansi\n" + "\n".join(lines) + f"\n{ANSI_RESET}```"


def availability_line(day: AvailabilityDay) -> str:
    day_label = f"{DAY_NAMES[day.day_of_week]:<9} - "

    if day.windows:
        return f"{ANSI_WHITE}{day_label}{ANSI_GREEN}{windows_to_text(day.windows)}{ANSI_RESET}"

    return f"{ANSI_WHITE}{day_label}{ANSI_RED}Unavailable{ANSI_RESET}"


def current_availability_block(days: list[AvailabilityDay]) -> str:
    return make_ansi_block([availability_line(day) for day in days])


def selected_days_block(selected_days: set[int]) -> str:
    lines: list[str] = []

    for day in range(7):
        selected = day in selected_days
        color = ANSI_GREEN if selected else ANSI_RED
        mark = "Available" if selected else "Unavailable"
        lines.append(ansi_line(f"{DAY_NAMES[day]:<9} - {mark}", color))

    return make_ansi_block(lines)


def day_editor_block(
    *,
    day: int,
    drafts: list[WindowDraft],
    current_index: int,
) -> str:
    lines = [ansi_line(DAY_NAMES[day].upper(), ANSI_GREEN), ansi_line("", ANSI_WHITE)]

    if not drafts:
        drafts = [WindowDraft()]

    for index, draft in enumerate(drafts):
        prefix = ">" if index == current_index else " "
        completed = draft.complete_window()

        if completed is None:
            start = minutes_to_time_label(draft.start) if draft.start is not None else "--:--"
            end = minutes_to_time_label(draft.end) if draft.end is not None else "--:--"
            color = ANSI_YELLOW
            text = f"{prefix}Window {index + 1}: {start} -> {end}"
        else:
            color = ANSI_WHITE
            text = f"{prefix}Window {index + 1}: {window_to_text(completed)}"

        lines.append(ansi_line(text, color))

    valid_windows = valid_windows_from_drafts(drafts)

    if not valid_windows:
        lines.append(ansi_line("", ANSI_WHITE))
        lines.append(ansi_line("At least one start/end window is required.", ANSI_RED))

    return make_ansi_block(lines)


def review_block(
    *,
    timezone: str,
    windows_by_day: dict[int, list[tuple[int, int]]],
) -> str:
    lines = [ansi_line(f"Timezone: {timezone}", ANSI_WHITE), ansi_line("", ANSI_WHITE)]

    for day in range(7):
        windows = normalize_windows(windows_by_day.get(day, []))
        if windows:
            lines.append(
                f"{ANSI_WHITE}{DAY_NAMES[day]:<9} - {ANSI_GREEN}{windows_to_text(windows)}{ANSI_RESET}"
            )
        else:
            lines.append(
                f"{ANSI_WHITE}{DAY_NAMES[day]:<9} - {ANSI_RED}Unavailable{ANSI_RESET}"
            )

    return make_ansi_block(lines)


def valid_windows_from_drafts(drafts: list[WindowDraft]) -> list[tuple[int, int]]:
    windows: list[tuple[int, int]] = []

    for draft in drafts:
        completed = draft.complete_window()
        if completed is not None:
            windows.append(completed)

    return normalize_windows(windows)


def make_drafts_from_windows(windows: list[tuple[int, int]]) -> list[WindowDraft]:
    drafts: list[WindowDraft] = []

    for start, end in normalize_windows(windows):
        drafts.append(WindowDraft(start=start, end=end))

    return drafts or [WindowDraft()]


class AvailabilitySession:
    def __init__(self, member: discord.Member):
        self.member = member
        self.discord_id = str(member.id)
        self.settings = get_user_settings(member)
        self.days = get_availability_days(self.discord_id)

        self.selected_days: set[int] = {
            day.day_of_week
            for day in self.days
            if day.windows
        }
        self.windows_by_day: dict[int, list[tuple[int, int]]] = {
            day.day_of_week: list(day.windows)
            for day in self.days
        }
        self.day_order: list[int] = sorted(self.selected_days)
        self.day_index = 0
        self.current_window_index = 0
        self.drafts_by_day: dict[int, list[WindowDraft]] = {
            day: make_drafts_from_windows(windows)
            for day, windows in self.windows_by_day.items()
            if windows
        }
        self.admin_timezone_filter: set[str] | None = None
        self.admin_group_filters: set[str] = set()

    @property
    def timezone(self) -> str | None:
        return self.settings.timezone

    def refresh_settings(self) -> None:
        self.settings = get_user_settings(self.member)

    def refresh_days(self) -> None:
        self.days = get_availability_days(self.discord_id)
        self.windows_by_day = {day.day_of_week: list(day.windows) for day in self.days}
        self.selected_days = {day.day_of_week for day in self.days if day.windows}
        self.day_order = sorted(self.selected_days)
        self.drafts_by_day = {
            day: make_drafts_from_windows(windows)
            for day, windows in self.windows_by_day.items()
            if windows
        }
        self.day_index = 0
        self.current_window_index = 0

    def selected_day(self) -> int | None:
        if not self.day_order:
            return None
        self.day_index = max(0, min(self.day_index, len(self.day_order) - 1))
        return self.day_order[self.day_index]

    def current_drafts(self) -> list[WindowDraft]:
        day = self.selected_day()
        if day is None:
            return [WindowDraft()]
        self.drafts_by_day.setdefault(day, make_drafts_from_windows(self.windows_by_day.get(day, [])))
        drafts = self.drafts_by_day[day]
        if not drafts:
            drafts.append(WindowDraft())
        self.current_window_index = max(0, min(self.current_window_index, len(drafts) - 1))
        return drafts

    def current_draft(self) -> WindowDraft:
        return self.current_drafts()[self.current_window_index]

    def save_current_day_drafts(self) -> bool:
        day = self.selected_day()
        if day is None:
            return False

        windows = valid_windows_from_drafts(self.current_drafts())
        if not windows:
            return False

        self.windows_by_day[day] = windows
        self.drafts_by_day[day] = make_drafts_from_windows(windows)
        return True

    def apply_selected_days(self) -> None:
        self.day_order = sorted(self.selected_days)
        self.day_index = 0
        self.current_window_index = 0

        for day in range(7):
            if day not in self.selected_days:
                self.windows_by_day[day] = []
                self.drafts_by_day.pop(day, None)
                continue

            self.drafts_by_day.setdefault(
                day,
                make_drafts_from_windows(self.windows_by_day.get(day, [])),
            )

    def clear_all(self) -> None:
        self.days = get_availability_days(self.discord_id)
        self.selected_days = set()
        self.windows_by_day = {day: [] for day in range(7)}
        self.day_order = []
        self.day_index = 0
        self.current_window_index = 0
        self.drafts_by_day = {}


class AvailabilityBaseView(discord.ui.View):
    def __init__(self, session: AvailabilitySession):
        super().__init__(timeout=900)
        self.session = session

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.session.member.id:
            await interaction.response.send_message(
                "Only the user who opened this availability menu can use it.",
                ephemeral=True,
            )
            return False
        return True


class AvailabilityOverviewView(AvailabilityBaseView):
    def __init__(self, session: AvailabilitySession):
        super().__init__(session)
        self.add_item(AvailabilityTimezoneSelect(session))
        self.add_item(ExitAvailabilityButton(row=1, style=discord.ButtonStyle.secondary))
        self.add_item(EditAvailabilityButton(disabled=session.timezone is None))
        self.add_item(ResetAvailabilityButton())
        self.add_item(AdminPanelButton(disabled=not member_is_admin(session.member)))


def overview_embed(session: AvailabilitySession) -> discord.Embed:
    session.refresh_settings()
    session.days = get_availability_days(session.discord_id)

    embed = discord.Embed(
        title="Availability",
        description=(
            "Set your usual weekly availability for op planning. "
            "Times are saved in your own timezone."
        ),
    )
    embed.add_field(
        name="Timezone",
        value=session.timezone or "Not set - select one below before continuing.",
        inline=False,
    )
    embed.add_field(
        name="Current Availability",
        value=current_availability_block(session.days),
        inline=False,
    )
    embed.set_footer(text=f"Settings for {session.member.display_name}")
    return embed


class AvailabilityTimezoneSelect(discord.ui.Select):
    def __init__(self, session: AvailabilitySession):
        self.session = session
        super().__init__(
            placeholder="Select timezone",
            min_values=1,
            max_values=1,
            options=timezone_options_with_default(session.timezone),
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        update_timezone(str(self.session.member.id), self.values[0])
        self.session.refresh_settings()
        await interaction.response.edit_message(
            content=None,
            embed=overview_embed(self.session),
            view=AvailabilityOverviewView(self.session),
        )


class EditAvailabilityButton(discord.ui.Button):
    def __init__(self, *, disabled: bool):
        super().__init__(
            label="Edit Availability",
            style=discord.ButtonStyle.primary,
            disabled=disabled,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        if not isinstance(self.view, AvailabilityOverviewView):
            await interaction.response.send_message("Could not read this menu.", ephemeral=True)
            return

        await interaction.response.edit_message(
            content=None,
            embed=day_select_embed(self.view.session),
            view=AvailabilityDaySelectView(self.view.session),
        )


class ResetAvailabilityButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Reset Availability", style=discord.ButtonStyle.danger, row=1)

    async def callback(self, interaction: discord.Interaction):
        if not isinstance(self.view, AvailabilityOverviewView):
            await interaction.response.send_message("Could not read this menu.", ephemeral=True)
            return

        reset_user_availability(self.view.session.discord_id)
        self.view.session.clear_all()
        await interaction.response.edit_message(
            content="Availability reset. Edit and submit when ready.",
            embed=overview_embed(self.view.session),
            view=AvailabilityOverviewView(self.view.session),
        )


class AdminPanelButton(discord.ui.Button):
    def __init__(self, *, disabled: bool):
        super().__init__(
            label="Admin Panel",
            style=discord.ButtonStyle.primary,
            disabled=disabled,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction):
        if not isinstance(self.view, AvailabilityOverviewView):
            await interaction.response.send_message("Could not read this menu.", ephemeral=True)
            return

        if not isinstance(interaction.user, discord.Member) or not member_is_admin(interaction.user):
            await interaction.response.send_message("Sorry, that is for Admin only.", ephemeral=True)
            return

        await interaction.response.defer(thinking=False)
        await edit_admin_panel_message(interaction, self.view.session, guild=interaction.guild)


class ExitAvailabilityButton(discord.ui.Button):
    def __init__(self, *, row: int, style: discord.ButtonStyle = discord.ButtonStyle.danger):
        super().__init__(label="Exit", style=style, row=row)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content="Availability menu closed.",
            embed=None,
            view=None,
        )


class AvailabilityDaySelectView(AvailabilityBaseView):
    def __init__(self, session: AvailabilitySession):
        super().__init__(session)
        self.add_item(AvailableDaysSelect(session))
        self.add_item(DaySelectBackButton())
        self.add_item(ExitAvailabilityButton(row=1))
        self.add_item(DaySelectNextButton(disabled=not bool(session.selected_days)))


def day_select_embed(session: AvailabilitySession) -> discord.Embed:
    embed = discord.Embed(
        title="Availability - Select Days",
        description="Select the days you are usually available. Red days will be saved as unavailable.",
    )
    embed.add_field(
        name="Days",
        value=selected_days_block(session.selected_days),
        inline=False,
    )
    embed.set_footer(text="Select at least one available day to continue.")
    return embed


class AvailableDaysSelect(discord.ui.Select):
    def __init__(self, session: AvailabilitySession):
        self.session = session
        options = [
            discord.SelectOption(
                label=DAY_NAMES[day],
                value=str(day),
                default=day in session.selected_days,
            )
            for day in range(7)
        ]
        super().__init__(
            placeholder="Select available days",
            min_values=1,
            max_values=7,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        self.session.selected_days = {int(value) for value in self.values}
        await interaction.response.edit_message(
            embed=day_select_embed(self.session),
            view=AvailabilityDaySelectView(self.session),
        )


class DaySelectBackButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Back", style=discord.ButtonStyle.primary, row=1)

    async def callback(self, interaction: discord.Interaction):
        if not isinstance(self.view, AvailabilityDaySelectView):
            await interaction.response.send_message("Could not read this menu.", ephemeral=True)
            return

        await interaction.response.edit_message(
            embed=overview_embed(self.view.session),
            view=AvailabilityOverviewView(self.view.session),
        )


class DaySelectNextButton(discord.ui.Button):
    def __init__(self, *, disabled: bool):
        super().__init__(label="Next", style=discord.ButtonStyle.success, disabled=disabled, row=1)

    async def callback(self, interaction: discord.Interaction):
        if not isinstance(self.view, AvailabilityDaySelectView):
            await interaction.response.send_message("Could not read this menu.", ephemeral=True)
            return

        self.view.session.apply_selected_days()
        await interaction.response.edit_message(
            embed=day_editor_embed(self.view.session),
            view=AvailabilityDayEditorView(self.view.session),
        )


class AvailabilityDayEditorView(AvailabilityBaseView):
    def __init__(self, session: AvailabilitySession):
        super().__init__(session)
        drafts = session.current_drafts()
        draft = session.current_draft()
        valid_windows = valid_windows_from_drafts(drafts)

        self.add_item(StartTimeSelect(session, draft.start))
        self.add_item(EndTimeSelect(session, draft.start, draft.end))
        self.add_item(WindowUpButton(disabled=session.current_window_index <= 0))
        self.add_item(AddWindowButton())
        self.add_item(RemoveWindowButton(disabled=len(drafts) <= 1))
        self.add_item(WindowDownButton(disabled=session.current_window_index >= len(drafts) - 1))
        self.add_item(DayEditorBackButton(session))
        self.add_item(ExitAvailabilityButton(row=3))
        self.add_item(NextDayButton(disabled=not bool(valid_windows)))


def day_editor_embed(session: AvailabilitySession) -> discord.Embed:
    day = session.selected_day()
    if day is None:
        return discord.Embed(
            title="Availability",
            description="No available days selected.",
        )

    embed = discord.Embed(
        title=f"Availability - {DAY_NAMES[day]}",
        description=(
            "Pick a start time first, then pick an end time. "
            "End times that wrap past midnight are saved as the next day."
        ),
    )
    embed.add_field(
        name="Windows",
        value=day_editor_block(
            day=day,
            drafts=session.current_drafts(),
            current_index=session.current_window_index,
        ),
        inline=False,
    )
    embed.set_footer(text=f"Day {session.day_index + 1} of {len(session.day_order)}")
    return embed


class StartTimeSelect(discord.ui.Select):
    def __init__(self, session: AvailabilitySession, selected: int | None):
        self.session = session
        super().__init__(
            placeholder="Start time",
            min_values=1,
            max_values=1,
            options=start_time_options(selected),
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        draft = self.session.current_draft()
        draft.start = int(self.values[0])
        draft.end = None
        await interaction.response.edit_message(
            embed=day_editor_embed(self.session),
            view=AvailabilityDayEditorView(self.session),
        )


class EndTimeSelect(discord.ui.Select):
    def __init__(self, session: AvailabilitySession, start: int | None, selected: int | None):
        self.session = session
        super().__init__(
            placeholder="End time" if start is not None else "Pick start time first",
            min_values=1,
            max_values=1,
            options=end_time_options(start, selected),
            disabled=start is None,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            await interaction.response.send_message("Pick a start time first.", ephemeral=True)
            return

        self.session.current_draft().end = int(self.values[0])
        await interaction.response.edit_message(
            embed=day_editor_embed(self.session),
            view=AvailabilityDayEditorView(self.session),
        )


class WindowUpButton(discord.ui.Button):
    def __init__(self, *, disabled: bool):
        super().__init__(label="Up", style=discord.ButtonStyle.primary, disabled=disabled, row=2)

    async def callback(self, interaction: discord.Interaction):
        if not isinstance(self.view, AvailabilityDayEditorView):
            await interaction.response.send_message("Could not read this menu.", ephemeral=True)
            return

        self.view.session.current_window_index = max(0, self.view.session.current_window_index - 1)
        await interaction.response.edit_message(
            embed=day_editor_embed(self.view.session),
            view=AvailabilityDayEditorView(self.view.session),
        )


class AddWindowButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Add Available Window", style=discord.ButtonStyle.success, row=2)

    async def callback(self, interaction: discord.Interaction):
        if not isinstance(self.view, AvailabilityDayEditorView):
            await interaction.response.send_message("Could not read this menu.", ephemeral=True)
            return

        drafts = self.view.session.current_drafts()
        drafts.append(WindowDraft())
        self.view.session.current_window_index = len(drafts) - 1
        await interaction.response.edit_message(
            embed=day_editor_embed(self.view.session),
            view=AvailabilityDayEditorView(self.view.session),
        )


class RemoveWindowButton(discord.ui.Button):
    def __init__(self, *, disabled: bool):
        super().__init__(label="Remove Available Window", style=discord.ButtonStyle.danger, disabled=disabled, row=2)

    async def callback(self, interaction: discord.Interaction):
        if not isinstance(self.view, AvailabilityDayEditorView):
            await interaction.response.send_message("Could not read this menu.", ephemeral=True)
            return

        drafts = self.view.session.current_drafts()
        if len(drafts) > 1:
            drafts.pop(self.view.session.current_window_index)
        if not drafts:
            drafts.append(WindowDraft())
        self.view.session.current_window_index = max(
            0,
            min(self.view.session.current_window_index, len(drafts) - 1),
        )
        await interaction.response.edit_message(
            embed=day_editor_embed(self.view.session),
            view=AvailabilityDayEditorView(self.view.session),
        )


class WindowDownButton(discord.ui.Button):
    def __init__(self, *, disabled: bool):
        super().__init__(label="Down", style=discord.ButtonStyle.primary, disabled=disabled, row=2)

    async def callback(self, interaction: discord.Interaction):
        if not isinstance(self.view, AvailabilityDayEditorView):
            await interaction.response.send_message("Could not read this menu.", ephemeral=True)
            return

        drafts = self.view.session.current_drafts()
        self.view.session.current_window_index = min(
            len(drafts) - 1,
            self.view.session.current_window_index + 1,
        )
        await interaction.response.edit_message(
            embed=day_editor_embed(self.view.session),
            view=AvailabilityDayEditorView(self.view.session),
        )


class DayEditorBackButton(discord.ui.Button):
    def __init__(self, session: AvailabilitySession):
        # First page goes back to day selection. Later pages go to the previous day.
        label = "Back" if session.day_index <= 0 else "Prev Day"
        super().__init__(label=label, style=discord.ButtonStyle.primary, row=3)

    async def callback(self, interaction: discord.Interaction):
        if not isinstance(self.view, AvailabilityDayEditorView):
            await interaction.response.send_message("Could not read this menu.", ephemeral=True)
            return

        session = self.view.session
        if valid_windows_from_drafts(session.current_drafts()):
            session.save_current_day_drafts()

        if session.day_index <= 0:
            await interaction.response.edit_message(
                embed=day_select_embed(session),
                view=AvailabilityDaySelectView(session),
            )
            return

        session.day_index -= 1
        session.current_window_index = 0
        await interaction.response.edit_message(
            embed=day_editor_embed(session),
            view=AvailabilityDayEditorView(session),
        )


class NextDayButton(discord.ui.Button):
    def __init__(self, *, disabled: bool):
        super().__init__(label="Next Day", style=discord.ButtonStyle.success, disabled=disabled, row=3)

    async def callback(self, interaction: discord.Interaction):
        if not isinstance(self.view, AvailabilityDayEditorView):
            await interaction.response.send_message("Could not read this menu.", ephemeral=True)
            return

        if not self.view.session.save_current_day_drafts():
            await interaction.response.send_message(
                "Finish at least one start/end window for this day first.",
                ephemeral=True,
            )
            return

        self.view.session.day_index += 1
        self.view.session.current_window_index = 0

        if self.view.session.day_index >= len(self.view.session.day_order):
            await interaction.response.edit_message(
                embed=review_embed(self.view.session),
                view=AvailabilityReviewView(self.view.session),
            )
            return

        await interaction.response.edit_message(
            embed=day_editor_embed(self.view.session),
            view=AvailabilityDayEditorView(self.view.session),
        )


class AvailabilityReviewView(AvailabilityBaseView):
    def __init__(self, session: AvailabilitySession):
        super().__init__(session)
        self.add_item(ReviewBackButton())
        self.add_item(ExitAvailabilityButton(row=0))
        self.add_item(SubmitAvailabilityButton())


def review_embed(session: AvailabilitySession) -> discord.Embed:
    timezone = session.timezone or "Not set"
    embed = discord.Embed(
        title="Availability - Review",
        description="Review your weekly availability before saving.",
    )
    embed.add_field(
        name="Review",
        value=review_block(timezone=timezone, windows_by_day=session.windows_by_day),
        inline=False,
    )
    return embed


class ReviewBackButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Back", style=discord.ButtonStyle.primary, row=0)

    async def callback(self, interaction: discord.Interaction):
        if not isinstance(self.view, AvailabilityReviewView):
            await interaction.response.send_message("Could not read this menu.", ephemeral=True)
            return

        self.view.session.day_index = max(0, len(self.view.session.day_order) - 1)
        self.view.session.current_window_index = 0
        await interaction.response.edit_message(
            embed=day_editor_embed(self.view.session),
            view=AvailabilityDayEditorView(self.view.session),
        )


class SubmitAvailabilityButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Submit", style=discord.ButtonStyle.success, row=0)

    async def callback(self, interaction: discord.Interaction):
        if not isinstance(self.view, AvailabilityReviewView):
            await interaction.response.send_message("Could not read this menu.", ephemeral=True)
            return

        session = self.view.session
        session.refresh_settings()

        if not session.timezone:
            await interaction.response.send_message(
                "Set your timezone before submitting availability.",
                ephemeral=True,
            )
            return

        save_full_availability(
            session.discord_id,
            windows_by_day=session.windows_by_day,
        )
        session.refresh_days()

        await interaction.response.edit_message(
            content="✅ Availability saved.",
            embed=overview_embed(session),
            view=None,
        )




ADMIN_FILTER_OPTIONS: list[tuple[str, str, str]] = [
    ("mission_qualified", "Mission Qualified only", "Only users with the Mission Qualified role."),
]


def _trim_select_label(value: str, *, limit: int = 100) -> str:
    value = str(value)
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "…"


def mission_qualified_discord_ids(guild: discord.Guild | None) -> set[str]:
    role_ids = mission_qualified_role_ids()
    if guild is None or not role_ids:
        return set()

    output: set[str] = set()
    for member in guild.members:
        if any(int(role.id) in role_ids for role in member.roles):
            output.add(str(member.id))
    return output


def admin_filter_parts(
    session: AvailabilitySession,
    guild: discord.Guild | None,
) -> set[str] | None:
    if "mission_qualified" in session.admin_group_filters:
        return mission_qualified_discord_ids(guild)
    return None


def admin_report(
    session: AvailabilitySession,
    guild: discord.Guild | None,
    *,
    include_timezone_filter: bool = True,
):
    # Display the admin panel in the timezone saved for the admin who opened it.
    # If they do not have one saved, the service falls back to config defaults.
    session.refresh_settings()
    display_timezone = session.timezone

    discord_id_filter = admin_filter_parts(session, guild)
    return build_availability_heatmap_report(
        display_timezone=display_timezone,
        timezone_filter=session.admin_timezone_filter if include_timezone_filter else None,
        discord_id_filter=discord_id_filter,
    )


def timezone_filter_options(
    session: AvailabilitySession,
    guild: discord.Guild | None,
) -> list[discord.SelectOption]:
    # Region counts are derived only from users with all seven availability
    # days marked complete. The unfiltered report keeps every region visible.
    report = admin_report(session, guild, include_timezone_filter=False)
    counts = report.timezone_counts
    total = sum(counts.values())

    options: list[discord.SelectOption] = [
        discord.SelectOption(
            label="All timezones",
            value="__all__",
            description=f"Default view - {total} submitted forms",
            default=session.admin_timezone_filter is None,
        )
    ]

    # Keep a stable geographic order. Regions with no submissions are omitted
    # so the menu reflects the completed forms that actually exist.
    for region_name in TIMEZONE_REGIONS:
        count = int(counts.get(region_name, 0))
        if count <= 0:
            continue
        percent = round((count / total) * 100) if total else 0
        selected = (
            session.admin_timezone_filter is not None
            and region_name in session.admin_timezone_filter
        )
        options.append(
            discord.SelectOption(
                label=_trim_select_label(f"{region_name} ({count})"),
                value=region_name,
                description=_trim_select_label(
                    f"{count} completed forms - {percent}%"
                ),
                default=selected,
            )
        )

    return options


def admin_group_filter_options(session: AvailabilitySession) -> list[discord.SelectOption]:
    return [
        discord.SelectOption(
            label=label,
            value=value,
            description=description,
            default=value in session.admin_group_filters,
        )
        for value, label, description in ADMIN_FILTER_OPTIONS
    ]



def admin_filter_summary(session: AvailabilitySession) -> str:
    timezone_text = "All regions" if session.admin_timezone_filter is None else ", ".join(
        region for region in TIMEZONE_REGIONS if region in session.admin_timezone_filter
    )
    if not session.admin_group_filters:
        group_text = "All submitted forms"
    else:
        label_by_value = {value: label for value, label, _description in ADMIN_FILTER_OPTIONS}
        group_text = ", ".join(label_by_value.get(value, value) for value in sorted(session.admin_group_filters))

    return (
        f"**Timezone filter:** {timezone_text}\n"
        f"**Form filter:** {group_text}"
    )


def admin_panel_summary_text(
    report,
    session: AvailabilitySession,
    guild: discord.Guild | None,
) -> str:
    submitted_ids = {str(discord_id) for discord_id in report.submitted_discord_ids}
    mission_ids = mission_qualified_discord_ids(guild)
    mission_count = len(submitted_ids & mission_ids)
    non_mission_count = max(0, report.completed_users - mission_count)

    timezone_rows = report.timezone_ratios(limit=12)
    if not timezone_rows:
        timezone_text = "No timezone data yet."
    else:
        timezone_text = "\n".join(
            f"- `{timezone_name}` — **{count}** forms, **{percent}%**"
            for timezone_name, count, percent in timezone_rows
        )

    return (
        "**Filter details**\n"
        f"Forms submitted: **{report.completed_users}**\n"
        f"Timezone shown: `{report.display_timezone}`\n"
        f"{admin_filter_summary(session)}\n\n"
        f"Mission Qualified forms: **{mission_count}**\n"
        f"Non-Mission Qualified forms: **{non_mission_count}**\n\n"
        f"**Submission timezones**\n{timezone_text}"
    )


def admin_panel_embed(
    report,
    session: AvailabilitySession,
    guild: discord.Guild | None,
) -> discord.Embed:
    embed = discord.Embed(
        title="Availability Admin Panel",
        description=admin_panel_summary_text(report, session, guild),
    )
    if report.skipped_users:
        embed.set_footer(
            text=f"Skipped {report.skipped_users} users with incomplete or invalid timezone data."
        )
    return embed


async def edit_admin_panel_message(
    interaction: discord.Interaction,
    session: AvailabilitySession,
    *,
    guild: discord.Guild | None = None,
    content: str | None = None,
) -> None:
    report = admin_report(session, guild)
    view = AvailabilityAdminPanelView(session, guild)

    if report.completed_users <= 0:
        embed = discord.Embed(
            title="Availability Admin Panel",
            description=(
                "No completed availability responses with saved timezones found yet.\n\n"
                + admin_panel_summary_text(report, session, guild)
            ),
        )
        if report.skipped_users:
            embed.set_footer(
                text=f"Skipped {report.skipped_users} users with incomplete or invalid timezone data."
            )
        await interaction.edit_original_response(
            content=content,
            embed=embed,
            attachments=[],
            view=view,
        )
        return

    png_bytes = render_availability_overview_heatmap_png(report)
    file = discord.File(
        fp=io.BytesIO(png_bytes),
        filename="availability_heatmap_weekly.png",
    )
    embed = admin_panel_embed(report, session, guild)
    embed.set_image(url="attachment://availability_heatmap_weekly.png")

    await interaction.edit_original_response(
        content=content,
        embed=embed,
        attachments=[file],
        view=view,
    )


class AvailabilityAdminPanelView(AvailabilityBaseView):
    def __init__(self, session: AvailabilitySession, guild: discord.Guild | None):
        super().__init__(session)
        self.guild = guild
        self.add_item(AdminTimezoneFilterSelect(session, guild))
        self.add_item(AdminFormFilterSelect(session))
        self.add_item(AdminPanelBackButton())
        self.add_item(AdminResetSurveyButton())


class AdminTimezoneFilterSelect(discord.ui.Select):
    def __init__(self, session: AvailabilitySession, guild: discord.Guild | None):
        self.session = session
        self.guild = guild
        options = timezone_filter_options(session, guild)
        super().__init__(
            placeholder="Filter by timezone region",
            min_values=1,
            max_values=min(25, len(options)),
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        if not isinstance(self.view, AvailabilityAdminPanelView):
            await interaction.response.send_message("Could not read this menu.", ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member) or not member_is_admin(interaction.user):
            await interaction.response.send_message("Sorry, that is for Admin only.", ephemeral=True)
            return

        if "__all__" in self.values:
            self.session.admin_timezone_filter = None
        else:
            self.session.admin_timezone_filter = {str(value) for value in self.values}

        await interaction.response.defer(thinking=False)
        await edit_admin_panel_message(interaction, self.session, guild=self.guild)


class AdminFormFilterSelect(discord.ui.Select):
    def __init__(self, session: AvailabilitySession):
        self.session = session
        options = admin_group_filter_options(session)
        super().__init__(
            placeholder="Filter submitted forms",
            min_values=0,
            max_values=len(options),
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        if not isinstance(self.view, AvailabilityAdminPanelView):
            await interaction.response.send_message("Could not read this menu.", ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member) or not member_is_admin(interaction.user):
            await interaction.response.send_message("Sorry, that is for Admin only.", ephemeral=True)
            return

        self.session.admin_group_filters = {str(value) for value in self.values}
        await interaction.response.defer(thinking=False)
        await edit_admin_panel_message(interaction, self.session, guild=self.view.guild)



class AdminPanelBackButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Back", style=discord.ButtonStyle.primary, row=3)

    async def callback(self, interaction: discord.Interaction):
        if not isinstance(self.view, AvailabilityAdminPanelView):
            await interaction.response.send_message("Could not read this menu.", ephemeral=True)
            return

        await interaction.response.edit_message(
            content=None,
            embed=overview_embed(self.view.session),
            attachments=[],
            view=AvailabilityOverviewView(self.view.session),
        )


class AvailabilityResetConfirmView(AvailabilityBaseView):
    def __init__(self, session: AvailabilitySession, guild: discord.Guild | None):
        super().__init__(session)
        self.guild = guild
        self.add_item(AvailabilityResetCancelButton())
        self.add_item(AvailabilityResetConfirmButton())


class AvailabilityResetCancelButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Cancel", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        if not isinstance(self.view, AvailabilityResetConfirmView):
            await interaction.response.send_message("Could not read this menu.", ephemeral=True)
            return

        if not isinstance(interaction.user, discord.Member) or not member_is_admin(interaction.user):
            await interaction.response.send_message("Sorry, that is for Admin only.", ephemeral=True)
            return

        await interaction.response.defer(thinking=False)
        await edit_admin_panel_message(
            interaction,
            self.view.session,
            guild=self.view.guild,
        )


class AvailabilityResetConfirmButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Reset Survey", style=discord.ButtonStyle.danger)

    async def callback(self, interaction: discord.Interaction):
        if not isinstance(self.view, AvailabilityResetConfirmView):
            await interaction.response.send_message("Could not read this menu.", ephemeral=True)
            return

        if not isinstance(interaction.user, discord.Member) or not member_is_admin(interaction.user):
            await interaction.response.send_message("Sorry, that is for Admin only.", ephemeral=True)
            return

        # Acknowledge the click before running the existing reset and rebuilding
        # the admin panel. This also prevents a second click from being handled
        # while the response is being refreshed.
        await interaction.response.defer(thinking=False)
        changed = reset_all_availability_to_pending()
        await edit_admin_panel_message(
            interaction,
            self.view.session,
            guild=self.view.guild,
            content=(
                f"✅ Availability survey reset. {changed} day rows were marked pending. "
                "Saved windows were kept for quick reconfirmation."
            ),
        )


class AdminResetSurveyButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Reset Survey", style=discord.ButtonStyle.danger, row=3)

    async def callback(self, interaction: discord.Interaction):
        if not isinstance(self.view, AvailabilityAdminPanelView):
            await interaction.response.send_message("Could not read this menu.", ephemeral=True)
            return

        if not isinstance(interaction.user, discord.Member) or not member_is_admin(interaction.user):
            await interaction.response.send_message("Sorry, that is for Admin only.", ephemeral=True)
            return

        embed = discord.Embed(
            title="Reset Availability Survey",
            description=(
                "⚠️ **Are you sure you want to reset the availability survey?**\n\n"
                "All submitted forms will be marked pending, and members will need "
                "to reconfirm their availability. Saved availability windows will be kept.\n\n"
                "**This action cannot be undone.**"
            ),
            color=discord.Color.red(),
        )

        await interaction.response.edit_message(
            content=None,
            embed=embed,
            attachments=[],
            view=AvailabilityResetConfirmView(self.view.session, self.view.guild),
        )


class AvailabilityCommands(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="availability",
        description="Set your usual weekly availability for op planning.",
    )
    @app_commands.guild_only()
    async def availability_command(
        self,
        interaction: discord.Interaction,
    ):
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This command can only be used inside the server.",
                ephemeral=True,
            )
            return

        session = AvailabilitySession(interaction.user)
        content = None

        if not session.timezone:
            content = "Select your timezone before editing availability."
        elif is_complete(session.days):
            content = "You can edit and resubmit your availability anytime."

        await interaction.response.send_message(
            content=content,
            embed=overview_embed(session),
            view=AvailabilityOverviewView(session),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(AvailabilityCommands(bot))
