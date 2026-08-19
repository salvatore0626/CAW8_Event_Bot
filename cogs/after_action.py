from __future__ import annotations

import traceback

import discord
from discord import app_commands

from services.after_action_report_service import (
    AfterActionAttendance,
    AfterActionEvent,
    AfterActionReport,
    autocomplete_op_template_names,
    format_event_datetime,
    format_event_select_datetime,
    get_after_action_report,
    get_events_by_op_name,
    get_recent_events,
    relative_time,
)
from services.permission_service import (
    require_mission_qualified_command,
)
from services.user_settings_service import get_user_settings, safe_timezone_name


MAX_SELECT_OPTIONS = 25


def truncate(value: str, limit: int) -> str:
    value = str(value or "")
    if len(value) <= limit:
        return value

    return value[: max(0, limit - 1)] + "…"


def codeblock(text: str, language: str = "text") -> str:
    if not text:
        text = "None"

    return f"```{language}\n{text[:3800]}\n```"


def landing_summary(row: AfterActionAttendance) -> str:
    landing = row.landing_type or row.attend_type or row.status or "Unknown"
    parts: list[str] = [landing]

    if landing.lower() in {"arrested", "ftr"} and row.wires is not None:
        parts.append(f"{row.wires} Wire")

    if row.bolters > 0:
        bolter_word = "Bolter" if row.bolters == 1 else "Bolters"
        parts.append(f"{row.bolters} {bolter_word}")

    if row.combat_deaths > 0:
        death_word = "death" if row.combat_deaths == 1 else "deaths"
        parts.append(f"{row.combat_deaths} {death_word}")

    return " - ".join(parts)


def display_slot(slot: str | None) -> str:
    if not slot:
        return "-"

    text = str(slot).strip()

    if not text:
        return "-"

    # Examples:
    # Cowboy 1-1 -> C 1-1
    # cowboy 1-1 -> C 1-1
    # B1-1      -> B 1-1
    # B 1-1     -> B 1-1
    compact_match = __import__("re").match(r"^([A-Za-z])\s*(\d+(?:-\d+)?)$", text)

    if compact_match:
        return f"{compact_match.group(1).upper()} {compact_match.group(2)}"

    word_match = __import__("re").match(
        r"^([A-Za-z][A-Za-z0-9_-]*)\s+(\d+(?:-\d+)?)$",
        text,
    )

    if word_match:
        return f"{word_match.group(1)[0].upper()} {word_match.group(2)}"

    return text


def attendance_code(report: AfterActionReport) -> str:
    lines = [
        f"{'Slot':<9} {'Name':<16} Type",
    ]

    if not report.attendance:
        lines.append("No attendance records found.")
        return "\n".join(lines)

    for row in report.attendance:
        slot = truncate(display_slot(row.slot), 8)
        name = truncate(row.name, 16)
        summary = landing_summary(row)
        lines.append(f"{slot:<9} {name:<16} {summary}")

    return "\n".join(lines)


def awards_code(report: AfterActionReport) -> str:
    if not report.awards:
        return "None"

    lines = []

    for award in report.awards:
        award_name = award.award_type.replace("_", " ").title()
        lines.append(f"{award_name:<16} {truncate(award.name, 24)}")

    return "\n".join(lines)


def report_embed(
    report: AfterActionReport,
    timezone_name: str = "America/Chicago",
) -> discord.Embed:
    event = report.event

    gpa = "N/A"
    if report.operation_gpa is not None:
        gpa = f"{report.operation_gpa:.3f}"

    embed = discord.Embed(
        title="After Action Report",
        description=(
            f"**OP {event.event_id} - {event.op_name}**\n"
            f"**Date:** {format_event_datetime(event.scheduled_at, timezone_name)} / {relative_time(event.scheduled_at)}\n\n"
            f"{codeblock(attendance_code(report))}\n"
            f"**Operation GPA:** {gpa}\n\n"
            f"**Awards:**\n{codeblock(awards_code(report))}"
        ),
    )

    embed.set_footer(
        text=f'Timezone: {timezone_name} || Use "/user settings" to change timezone'
    )

    return embed


def selection_embed(
    events: list[AfterActionEvent],
    *,
    op_name: str | None = None,
    timezone_name: str = "America/Chicago",
) -> discord.Embed:
    if op_name:
        description = f"Latest completed ops matching **{op_name}**."
    else:
        description = "Select one of the latest 25 completed ops."

    if events:
        lines = []
        for event in events[:25]:
            lines.append(
                f"OP {event.event_id:<5} {format_event_select_datetime(event.scheduled_at, timezone_name):<20} {event.op_name}"
            )

        # Keep this in the embed description, not a field, because fields max at 1024 chars.
        description += "\n\n" + codeblock("\n".join(lines))[:3900]

    embed = discord.Embed(
        title="Select After Action Report",
        description=description[:4096],
    )
    embed.set_footer(
        text=f'Timezone: {timezone_name} || Use "/user settings" to change timezone'
    )
    return embed


class EventSelect(discord.ui.Select):
    def __init__(
        self,
        events: list[AfterActionEvent],
        timezone_name: str = "America/Chicago",
    ):
        options = []

        for event in events[:MAX_SELECT_OPTIONS]:
            options.append(
                discord.SelectOption(
                    label=truncate(f"OP {event.event_id} - {event.op_name}", 100),
                    value=str(event.event_id),
                    description=truncate(
                        f"{format_event_select_datetime(event.scheduled_at, timezone_name)} / {relative_time(event.scheduled_at)}",
                        100,
                    ),
                )
            )

        super().__init__(
            placeholder="Select an op",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, AfterActionSelectView)

        await interaction.response.defer()

        try:
            event_id = int(self.values[0])
            selected_index = self.view.index_for_event_id(event_id)
            report = get_after_action_report(event_id)

            if report is None:
                await interaction.followup.send(
                    f"Could not find OP {event_id}.",
                    ephemeral=True,
                )
                return

            await interaction.edit_original_response(
                embed=report_embed(report, self.view.timezone_name),
                view=AfterActionReportView(
                    owner_id=self.view.owner_id,
                    events=self.view.events,
                    selected_index=selected_index,
                    timezone_name=self.view.timezone_name,
                ),
            )
        except Exception as error:
            traceback.print_exc()
            await interaction.followup.send(
                f"After action report failed: `{type(error).__name__}: {error}`",
                ephemeral=True,
            )


class AfterActionSelectView(discord.ui.View):
    def __init__(
        self,
        owner_id: int,
        events: list[AfterActionEvent],
        timezone_name: str = "America/Chicago",
    ):
        super().__init__(timeout=900)
        self.owner_id = int(owner_id)
        self.events = events
        self.timezone_name = safe_timezone_name(timezone_name)

        self.add_item(EventSelect(events, self.timezone_name))
        self.add_item(QuitButton(row=1))

    def index_for_event_id(self, event_id: int) -> int:
        for index, event in enumerate(self.events):
            if event.event_id == int(event_id):
                return index

        return 0

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the user who opened this report can use these controls.",
                ephemeral=True,
            )
            return False

        return True


class PrevReportButton(discord.ui.Button):
    def __init__(self, disabled: bool):
        super().__init__(
            label="Prev",
            style=discord.ButtonStyle.primary,
            row=0,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, AfterActionReportView)
        await self.view.move_by_event_id(interaction, direction=-1)


class NextReportButton(discord.ui.Button):
    def __init__(self, disabled: bool):
        super().__init__(
            label="Next",
            style=discord.ButtonStyle.primary,
            row=0,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, AfterActionReportView)
        await self.view.move_by_event_id(interaction, direction=1)


class QuitButton(discord.ui.Button):
    def __init__(self, row: int = 0):
        super().__init__(
            label="Quit",
            style=discord.ButtonStyle.secondary,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer()
            await interaction.delete_original_response()
        except Exception:
            try:
                await interaction.edit_original_response(
                    content="After action report closed.",
                    embed=None,
                    view=None,
                )
            except Exception:
                pass


class AfterActionReportView(discord.ui.View):
    def __init__(
        self,
        owner_id: int,
        events: list[AfterActionEvent],
        selected_index: int,
        timezone_name: str = "America/Chicago",
    ):
        super().__init__(timeout=900)
        self.owner_id = int(owner_id)
        self.events = events
        self.selected_index = selected_index
        self.timezone_name = safe_timezone_name(timezone_name)

        disabled = len(events) <= 1

        self.add_item(PrevReportButton(disabled=disabled))
        self.add_item(QuitButton())
        self.add_item(NextReportButton(disabled=disabled))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the user who opened this report can use these controls.",
                ephemeral=True,
            )
            return False

        return True

    async def move_by_event_id(
        self,
        interaction: discord.Interaction,
        *,
        direction: int,
    ):
        """Move by operation ID, independent of the list's date sort order.

        direction < 0 selects the closest lower event ID (Prev).
        direction > 0 selects the closest higher event ID (Next).
        Navigation wraps at the lowest/highest ID to preserve the existing UI behavior.
        """
        await interaction.response.defer()

        try:
            if not self.events:
                await interaction.edit_original_response(
                    content="No ops are available.",
                    embed=None,
                    view=None,
                )
                return

            current_event = self.events[self.selected_index]
            current_id = int(current_event.event_id)

            if direction < 0:
                candidates = [
                    (index, event)
                    for index, event in enumerate(self.events)
                    if int(event.event_id) < current_id
                ]
                if candidates:
                    target_index, event = max(
                        candidates,
                        key=lambda item: int(item[1].event_id),
                    )
                else:
                    target_index, event = max(
                        enumerate(self.events),
                        key=lambda item: int(item[1].event_id),
                    )
            else:
                candidates = [
                    (index, event)
                    for index, event in enumerate(self.events)
                    if int(event.event_id) > current_id
                ]
                if candidates:
                    target_index, event = min(
                        candidates,
                        key=lambda item: int(item[1].event_id),
                    )
                else:
                    target_index, event = min(
                        enumerate(self.events),
                        key=lambda item: int(item[1].event_id),
                    )

            self.selected_index = target_index
            report = get_after_action_report(event.event_id)

            if report is None:
                await interaction.followup.send(
                    f"Could not find OP {event.event_id}.",
                    ephemeral=True,
                )
                return

            await interaction.edit_original_response(
                embed=report_embed(report, self.timezone_name),
                view=AfterActionReportView(
                    owner_id=self.owner_id,
                    events=self.events,
                    selected_index=self.selected_index,
                    timezone_name=self.timezone_name,
                ),
            )
        except Exception as error:
            traceback.print_exc()
            await interaction.followup.send(
                f"After action report failed: `{type(error).__name__}: {error}`",
                ephemeral=True,
            )


after_group = app_commands.Group(
    name="after",
    description="After action commands.",
)

action_group = app_commands.Group(
    name="action",
    description="Action report commands.",
)

after_group.add_command(action_group)


@action_group.command(
    name="report",
    description="View an after action report for an operation.",
)
@app_commands.describe(
    opid="Optional operation/event ID.",
    opname="Optional operation template name filter.",
)
async def report(
    interaction: discord.Interaction,
    opid: int | None = None,
    opname: str | None = None,
):
    if not await require_mission_qualified_command(interaction):
        return
    await interaction.response.defer(thinking=True, ephemeral=True)

    try:
        timezone_name = "America/Chicago"
        if isinstance(interaction.user, discord.Member):
            settings = get_user_settings(interaction.user)
            timezone_name = safe_timezone_name(settings.timezone)

        if opid is not None and opname:
            await interaction.followup.send(
                "Use either `opid` or `opname`, not both.",
                ephemeral=True,
            )
            return

        if opid is not None:
            report_data = get_after_action_report(opid)

            if report_data is None:
                await interaction.followup.send(
                    f"Could not find OP ID `{opid}`.",
                    ephemeral=True,
                )
                return

            events = [report_data.event]

            if opname:
                same_name_events = get_events_by_op_name(opname, limit=25)
                if same_name_events:
                    events = same_name_events

            selected_index = 0
            for index, event in enumerate(events):
                if event.event_id == report_data.event.event_id:
                    selected_index = index
                    break

            await interaction.followup.send(
                embed=report_embed(report_data, timezone_name),
                view=AfterActionReportView(
                    owner_id=interaction.user.id,
                    events=events,
                    selected_index=selected_index,
                    timezone_name=timezone_name,
                ),
            )
            return

        if opname:
            events = get_events_by_op_name(opname, limit=25)
        else:
            events = get_recent_events(limit=25)

        if not events:
            await interaction.followup.send(
                "No matching completed ops found.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            embed=selection_embed(
                events,
                op_name=opname,
                timezone_name=timezone_name,
            ),
            view=AfterActionSelectView(
                owner_id=interaction.user.id,
                events=events,
                timezone_name=timezone_name,
            ),
        )
    except Exception as error:
        traceback.print_exc()
        await interaction.followup.send(
            f"After action report failed: `{type(error).__name__}: {error}`",
            ephemeral=True,
        )


@report.autocomplete("opname")
async def opname_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    try:
        names = autocomplete_op_template_names(current, limit=25)

        return [
            app_commands.Choice(
                name=truncate(name, 100),
                value=truncate(name, 100),
            )
            for name in names[:25]
        ]
    except Exception:
        traceback.print_exc()
        return []


async def setup(bot):
    # Makes hot-reload safer if this extension is reloaded.
    try:
        bot.tree.remove_command(
            "after",
            type=discord.AppCommandType.chat_input,
        )
    except Exception:
        pass

    bot.tree.add_command(after_group)
