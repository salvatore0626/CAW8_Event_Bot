from __future__ import annotations

import json
import textwrap
from dataclasses import dataclass, field
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from services.admin_log_service import AdminLogRecord, list_admin_log_records
from services.display_name_service import prune_display_name
from services.permission_service import member_is_admin, require_admin_command
from services.private_view_service import (
    PrivateTimeoutView,
    bind_private_view,
    bind_view_to_original_response,
)
from services.user_settings_service import get_user_settings, safe_zoneinfo


WINDOW_SIZE = 9
ANSI_RESET = "\u001b[0m"
ANSI_GREEN = "\u001b[32m"
ANSI_RED = "\u001b[31m"
ANSI_YELLOW = "\u001b[33m"
ANSI_BLUE = "\u001b[34m"
ANSI_CYAN = "\u001b[36m"
ANSI_PURPLE = "\u001b[35m"

# Keys that are normally bookkeeping timestamps rather than the administrative
# change the viewer is trying to surface.
NOISY_DIFF_KEYS = {"created_at", "updated_at", "logged_at"}

ACTION_TABLE_LABELS = {
    "record_add": "/recordedit add",
    "record_edit": "/recordedit edit",
    "record_delete": "/recordedit delete",
    "request_denied": "/requests deny",
    "request_mia": "/requests mia",
    "request_cancelled": "/get qualified cancel",
    "promotion_override": "/promote",
    "event_cancelled": "scheduleview cancel",
    "event_uncancelled": "scheduleview un-cancel",
    "fax_qualifications": "/fax qualifications",
    "fax_operation_reviews": "/fax op reviews",
    "fax_flight_lead_reviews": "/fax FL reviews",
    "fax_attendance": "/fax attendance",
    "fax_database": "/fax database",
}

FILTER_DEFINITIONS = (
    ("record_edits", "Record Edits", "All /recordedit add, edit, and delete entries"),
    ("schedule", "Schedule Edits", "All /scheduleview cancel and un-cancel entries"),
    ("qualifications", "Qualifications", "Qualification and /requests audit entries"),
    ("fax", "Faxes", "All /fax audit entries"),
    ("promotions", "Promotions", "Promotion override entries"),
)
FILTER_KEYS = tuple(key for key, _label, _description in FILTER_DEFINITIONS)


@dataclass
class AdminLogState:
    owner_id: int
    timezone_name: str
    all_rows: list[AdminLogRecord]
    selected_filters: set[str] = field(default_factory=lambda: set(FILTER_KEYS))
    selected_index: int = 0

    def filtered_rows(self) -> list[AdminLogRecord]:
        return [
            row
            for row in self.all_rows
            if filter_key_for_action(row.action) in self.selected_filters
        ]


def selected_window(total: int, selected_index: int) -> tuple[int, int]:
    if total <= WINDOW_SIZE:
        return 0, total

    half = WINDOW_SIZE // 2
    start = max(0, selected_index - half)
    end = min(total, start + WINDOW_SIZE)
    start = max(0, end - WINDOW_SIZE)
    return start, end


def safe_code_text(value: Any, *, limit: int = 1000) -> str:
    text = str(value or "").replace("```", "''' ").strip()
    if not text:
        return "—"
    if len(text) > limit:
        return text[: max(1, limit - 1)].rstrip() + "…"
    return text


def compact_name(name: str | None, discord_id: str | None, *, width: int = 22) -> str:
    raw = str(name or discord_id or "System").strip() or "System"
    text = prune_display_name(raw, fallback=discord_id or "System")
    if len(text) > width:
        text = text[: max(1, width - 1)] + "…"
    return text


def parse_json_dict(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"raw": raw}
    return value if isinstance(value, dict) else {"value": value}


def json_value(value: Any, *, limit: int = 90) -> str:
    if value is None:
        text = "null"
    elif isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (dict, list)):
        text = json.dumps(value, sort_keys=True, default=str)
    else:
        text = str(value)

    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: max(1, limit - 1)].rstrip() + "…"
    return text


def extracted_command(row: AdminLogRecord) -> str | None:
    for payload in (parse_json_dict(row.before_json), parse_json_dict(row.after_json)):
        value = payload.get("command")
        if value:
            return str(value)
    return None


def format_action(action: str, *, width: int = 25) -> str:
    text = str(action or "unknown").strip()
    if len(text) > width:
        text = text[: max(1, width - 1)] + "…"
    return text


def command_label(row: AdminLogRecord) -> str:
    # Schedule rows share the same /scheduleview command payload, so the table
    # label needs the action suffix to distinguish cancel from un-cancel.
    if row.action in {"event_cancelled", "event_uncancelled"}:
        return ACTION_TABLE_LABELS[row.action]
    return extracted_command(row) or ACTION_TABLE_LABELS.get(row.action, row.action)


def filter_key_for_action(action: str) -> str:
    value = str(action or "").strip().lower()
    if value in {"record_add", "record_edit", "record_delete"}:
        return "record_edits"
    if value in {"event_cancelled", "event_uncancelled"} or value.startswith("schedule_"):
        return "schedule"
    if value.startswith("fax_"):
        return "fax"
    if (
        value in {"request_denied", "request_mia", "request_cancelled"}
        or value.startswith("requests_")
        or value.startswith("qualification_request_")
    ):
        return "qualifications"
    if value == "promotion_override" or value.startswith("promotion_"):
        return "promotions"
    # Keep unknown historical rows visible when all categories are selected by
    # treating them as qualification/admin-request activity rather than silently
    # dropping them from the viewer.
    return "qualifications"


def action_ansi_color(row: AdminLogRecord) -> str:
    if row.action == "record_add":
        return ANSI_GREEN
    if row.action == "record_delete":
        return ANSI_RED
    if row.action == "record_edit":
        return ANSI_YELLOW
    if filter_key_for_action(row.action) == "schedule":
        return ANSI_CYAN
    if filter_key_for_action(row.action) == "fax":
        return ANSI_BLUE
    if filter_key_for_action(row.action) == "qualifications":
        return ANSI_PURPLE
    return ""


def format_log_table_line(row: AdminLogRecord, *, selected: bool) -> str:
    marker = ">" if selected else " "
    row_id = f"#{row.id}"
    action = format_action(command_label(row))
    actor = compact_name(row.performed_by_display_name, row.performed_by_id)
    text = f"{marker}{row_id:<7}{action:<27}{actor:<22}"

    color = action_ansi_color(row)
    if color:
        return f"{color}{text}{ANSI_RESET}"
    return text


def record_action_changed_lines(row: AdminLogRecord) -> list[str]:
    before = parse_json_dict(row.before_json)
    after = parse_json_dict(row.after_json)

    # Keep the operation identifiers at the top for every /recordedit action.
    context_fields = (
        ("entry_id", "entry_id"),
        ("scheduled_op_id", "scheduled_op_id"),
        ("op_template_name", "op_template_name"),
    )

    # Human-facing attendance fields are grouped by meaning instead of rendered
    # as a fixed-width before/after table. System timestamps/status/type changes
    # are intentionally omitted because they are implementation side effects of
    # recordedit rather than the attendance information the admin changed.
    categories = (
        (
            "Pilot",
            (
                ("user_name", "User"),
                ("discord_id", "Discord ID"),
            ),
        ),
        (
            "Assignment",
            (
                ("slot", "Slot"),
                ("aircraft", "Aircraft"),
            ),
        ),
        (
            "Attendance",
            (
                ("attend_type", "Attend Type"),
                ("combat_deaths", "Combat Deaths"),
            ),
        ),
        (
            "Recovery",
            (
                ("landing_type", "Landing"),
                ("wires", "Wire"),
                ("bolters", "Bolters"),
            ),
        ),
        (
            "Reviews",
            (
                ("flight_lead_rating", "FL Rating"),
                ("fl_remarks", "FL Remarks"),
                ("op_remarks", "Op Remarks"),
                ("note_remarks", "Note Remarks"),
            ),
        ),
    )

    detail_width = 76

    def value_text(payload: dict[str, Any], key: str) -> str:
        if key not in payload:
            return "—"
        value = payload.get(key)
        if value is None:
            return "—"
        return json_value(value, limit=700)

    def wrapped_label(label: str, value: str) -> list[str]:
        prefix = f"{label}: "
        available = max(16, detail_width - len(prefix))
        chunks = textwrap.wrap(
            value,
            width=available,
            break_long_words=True,
            break_on_hyphens=True,
        ) or ["—"]
        lines = [prefix + chunks[0]]
        continuation = " " * len(prefix)
        lines.extend(continuation + chunk for chunk in chunks[1:])
        return lines

    def change_lines(label: str, before_value: str, after_value: str) -> list[str]:
        inline = f"{label}: {before_value} → {after_value}"
        if len(inline) <= detail_width:
            return [inline]

        lines = [f"{label}:"]
        for side_label, value in (("Before", before_value), ("After", after_value)):
            prefix = f"  {side_label}: " if side_label == "Before" else "  After:  "
            available = max(16, detail_width - len(prefix))
            chunks = textwrap.wrap(
                value,
                width=available,
                break_long_words=True,
                break_on_hyphens=True,
            ) or ["—"]
            lines.append(prefix + chunks[0])
            continuation = " " * len(prefix)
            lines.extend(continuation + chunk for chunk in chunks[1:])
        return lines

    lines: list[str] = []
    for key, label in context_fields:
        if key in after or key in before:
            value = after.get(key) if key in after else before.get(key)
            lines.extend(wrapped_label(label, json_value(value, limit=700)))

    category_blocks: list[list[str]] = []
    for category_name, fields in categories:
        category_lines: list[str] = []
        for key, label in fields:
            before_value = before.get(key) if key in before else None
            after_value = after.get(key) if key in after else None
            before_present = key in before
            after_present = key in after

            if (
                before_present
                and after_present
                and before_value == after_value
            ):
                continue
            if not before_present and not after_present:
                continue

            category_lines.extend(
                change_lines(
                    label,
                    value_text(before, key),
                    value_text(after, key),
                )
            )

        if category_lines:
            category_blocks.append([category_name, *category_lines])

    for block in category_blocks:
        if lines:
            lines.append("")
        lines.extend(block)

    return lines or ["No field-level differences found."]


def changed_lines(row: AdminLogRecord) -> list[str]:
    if row.action in {"record_add", "record_edit", "record_delete"}:
        return record_action_changed_lines(row)

    before = parse_json_dict(row.before_json)
    after = parse_json_dict(row.after_json)

    if before and after:
        before_keys = set(before) - NOISY_DIFF_KEYS
        after_keys = set(after) - NOISY_DIFF_KEYS
        shared_keys = before_keys & after_keys

        # Some audit writers intentionally store a full "before" object and only
        # a partial "after" summary. Missing keys therefore mean "not repeated",
        # not "deleted". Only use an arrow when both payloads contain the key.
        changed = sorted(
            key for key in shared_keys if before.get(key) != after.get(key)
        )

        context_keys = (
            "command",
            "request_id",
            "event_id",
            "entry_id",
            "scheduled_op_id",
            "operation",
            "op_template_name",
        )
        lines: list[str] = []
        shown: set[str] = set()

        for key in context_keys:
            if key in before_keys or key in after_keys:
                value = after.get(key) if key in after else before.get(key)
                lines.append(f"{key}: {json_value(value)}")
                shown.add(key)

        for key in changed:
            if key in shown:
                continue
            lines.append(
                f"{key}: {json_value(before.get(key))} -> {json_value(after.get(key))}"
            )
            shown.add(key)

        # Preserve useful context that only one side stored, but do not claim it
        # changed to/from null. This is common for /scheduleview and /promote logs.
        for key in sorted((before_keys ^ after_keys) - shown):
            value = before.get(key) if key in before else after.get(key)
            lines.append(f"{key}: {json_value(value)}")

        return lines or ["No field-level differences found."]

    payload = after or before
    if payload:
        return [
            f"{key}: {json_value(value)}"
            for key, value in sorted(payload.items())
            if key not in NOISY_DIFF_KEYS
        ] or ["No displayable details found."]

    return ["No before/after details were stored for this action."]


def format_changes(row: AdminLogRecord) -> str:
    text = "\n".join(changed_lines(row))
    return safe_code_text(text, limit=1000)


def identity_text(display_name: str | None, discord_id: str | None) -> str:
    if not discord_id:
        return "—"
    return f"<@{discord_id}>"


def filter_summary(state: AdminLogState) -> str:
    if state.selected_filters == set(FILTER_KEYS):
        return "All"
    if not state.selected_filters:
        return "None"

    labels_by_key = {key: label for key, label, _description in FILTER_DEFINITIONS}
    labels = [labels_by_key[key] for key in FILTER_KEYS if key in state.selected_filters]
    return ", ".join(labels)


def build_admin_log_embed(state: AdminLogState) -> discord.Embed:
    rows = state.filtered_rows()
    total = len(rows)

    if total:
        state.selected_index = max(0, min(state.selected_index, total - 1))
    else:
        state.selected_index = 0

    start, end = selected_window(total, state.selected_index)
    lines: list[str] = []

    if not rows:
        lines.append("No admin log records match the selected filters.")
    else:
        lines.append(" ID     Action                     User")
        for index in range(start, end):
            lines.append(
                format_log_table_line(
                    rows[index],
                    selected=index == state.selected_index,
                )
            )

    embed = discord.Embed(
        title="Admin Log",
        description="```ansi\n" + "\n".join(lines)[:3900] + "\n```",
    )

    if rows:
        row = rows[state.selected_index]
        command = extracted_command(row)

        details = [
            f"**Log ID:** `#{row.id}`",
            f"**Action:** `{row.action}`",
        ]
        if command:
            details.append(f"**Command:** `{command}`")
        else:
            details.append(f"**Command:** `{command_label(row)}`")
        details.extend(
            [
                f"**Performed by:** {identity_text(row.performed_by_display_name, row.performed_by_id)}",
                f"**Target:** {identity_text(row.user_display_name, row.user_discord_id)}",
                f"**Time:** <t:{row.created_at}:F> • <t:{row.created_at}:R>",
            ]
        )
        embed.add_field(name="Selected Entry", value="\n".join(details)[:1024], inline=False)

        embed.add_field(
            name="What Changed",
            value=f"```\n{format_changes(row)}\n```",
            inline=False,
        )

        if row.reason:
            embed.add_field(
                name="Reason / Remarks",
                value=f"```\n{safe_code_text(row.reason, limit=990)}\n```",
                inline=False,
            )

        embed.set_footer(
            text=(
                f"Entry {state.selected_index + 1}/{total} • Newest first • "
                f"Filter: {filter_summary(state)}"
            )[:2048]
        )
    else:
        embed.set_footer(text=f"Filter: {filter_summary(state)}")

    return embed


class AdminLogFilterSelect(discord.ui.Select):
    def __init__(self, state: AdminLogState, *, row: int):
        options = [
            discord.SelectOption(
                label=label,
                value=key,
                description=description,
                default=key in state.selected_filters,
            )
            for key, label, description in FILTER_DEFINITIONS
        ]
        super().__init__(
            placeholder="Filter admin log entries",
            min_values=0,
            max_values=len(options),
            options=options,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        assert isinstance(self.view, AdminLogView)
        state = self.view.state
        old_rows = state.filtered_rows()
        selected_id = None
        if old_rows and 0 <= state.selected_index < len(old_rows):
            selected_id = old_rows[state.selected_index].id

        state.selected_filters = set(self.values)
        new_rows = state.filtered_rows()

        if selected_id is not None:
            new_index = next(
                (index for index, row in enumerate(new_rows) if row.id == selected_id),
                None,
            )
            state.selected_index = new_index if new_index is not None else 0
        else:
            state.selected_index = 0

        await self.view.refresh(interaction)


class AdminLogView(PrivateTimeoutView):
    def __init__(self, state: AdminLogState):
        super().__init__()
        self.state = state
        count = len(state.filtered_rows())
        if count:
            state.selected_index = max(0, min(state.selected_index, count - 1))
        else:
            state.selected_index = 0

        self.add_item(AdminLogFilterSelect(state, row=0))
        self.add_item(
            AdminLogPrevButton(
                disabled=count <= 1 or state.selected_index <= 0,
                row=1,
            )
        )
        self.add_item(
            AdminLogPageUpButton(
                disabled=count <= 1 or state.selected_index <= 0,
                row=1,
            )
        )
        self.add_item(AdminLogQuitButton(row=1))
        self.add_item(
            AdminLogPageDownButton(
                disabled=count <= 1 or state.selected_index >= count - 1,
                row=1,
            )
        )
        self.add_item(
            AdminLogNextButton(
                disabled=count <= 1 or state.selected_index >= count - 1,
                row=1,
            )
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.state.owner_id:
            await interaction.response.send_message(
                "This admin log view belongs to someone else.",
                ephemeral=True,
            )
            return False

        if not isinstance(interaction.user, discord.Member) or not member_is_admin(interaction.user):
            await interaction.response.send_message(
                "You no longer have permission to view the admin log.",
                ephemeral=True,
            )
            return False

        return True

    async def refresh(self, interaction: discord.Interaction) -> None:
        view = AdminLogView(self.state)
        await interaction.response.edit_message(
            embed=build_admin_log_embed(self.state),
            view=bind_private_view(view, interaction.message),
        )


class AdminLogPrevButton(discord.ui.Button):
    def __init__(self, *, disabled: bool, row: int):
        super().__init__(label="Prev", style=discord.ButtonStyle.secondary, disabled=disabled, row=row)

    async def callback(self, interaction: discord.Interaction) -> None:
        assert isinstance(self.view, AdminLogView)
        count = len(self.view.state.filtered_rows())
        if count <= 0:
            await interaction.response.defer()
            return
        self.view.state.selected_index = max(0, self.view.state.selected_index - 1)
        await self.view.refresh(interaction)


class AdminLogPageUpButton(discord.ui.Button):
    def __init__(self, *, disabled: bool, row: int):
        super().__init__(label="Pg Up", style=discord.ButtonStyle.secondary, disabled=disabled, row=row)

    async def callback(self, interaction: discord.Interaction) -> None:
        assert isinstance(self.view, AdminLogView)
        count = len(self.view.state.filtered_rows())
        if count <= 0:
            await interaction.response.defer()
            return
        self.view.state.selected_index = max(
            0,
            self.view.state.selected_index - WINDOW_SIZE,
        )
        await self.view.refresh(interaction)


class AdminLogQuitButton(discord.ui.Button):
    def __init__(self, *, row: int):
        super().__init__(label="Quit", style=discord.ButtonStyle.secondary, row=row)

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            content="Admin log dismissed.",
            embed=None,
            view=None,
        )


class AdminLogPageDownButton(discord.ui.Button):
    def __init__(self, *, disabled: bool, row: int):
        super().__init__(label="Pg Down", style=discord.ButtonStyle.secondary, disabled=disabled, row=row)

    async def callback(self, interaction: discord.Interaction) -> None:
        assert isinstance(self.view, AdminLogView)
        count = len(self.view.state.filtered_rows())
        if count <= 0:
            await interaction.response.defer()
            return
        self.view.state.selected_index = min(
            count - 1,
            self.view.state.selected_index + WINDOW_SIZE,
        )
        await self.view.refresh(interaction)


class AdminLogNextButton(discord.ui.Button):
    def __init__(self, *, disabled: bool, row: int):
        super().__init__(label="Next", style=discord.ButtonStyle.secondary, disabled=disabled, row=row)

    async def callback(self, interaction: discord.Interaction) -> None:
        assert isinstance(self.view, AdminLogView)
        count = len(self.view.state.filtered_rows())
        if count <= 0:
            await interaction.response.defer()
            return
        self.view.state.selected_index = min(
            count - 1,
            self.view.state.selected_index + 1,
        )
        await self.view.refresh(interaction)


class AdminLogCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="adminlog",
        description="Browse administrative audit-log entries.",
    )
    @app_commands.guild_only()
    async def adminlog_command(self, interaction: discord.Interaction) -> None:
        if not await require_admin_command(interaction):
            return
        if not isinstance(interaction.user, discord.Member):
            return

        settings = get_user_settings(interaction.user)
        timezone_name = safe_zoneinfo(settings.timezone).key
        rows = list_admin_log_records()
        state = AdminLogState(
            owner_id=interaction.user.id,
            timezone_name=timezone_name,
            all_rows=rows,
        )
        view = AdminLogView(state)

        await interaction.response.send_message(
            embed=build_admin_log_embed(state),
            view=view,
            ephemeral=True,
        )
        await bind_view_to_original_response(interaction, view)


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminLogCog(bot))
