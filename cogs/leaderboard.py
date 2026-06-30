from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database import get_connection
from services.display_name_service import prune_display_name
from services.leaderboard_service import (
    is_tracked_leaderboard_message,
    queue_leaderboard_refresh,
    queue_leaderboard_refresh_now,
    queue_leaderboard_startup_rebuild,
)
from services.permission_service import (
    require_mission_qualified_command,
)
from services.wire_gpa_service import (
    bolter_score,
    gpa_scale_footer_text,
    wire_score,
)


ANSI_RESET = "\u001b[0m"
ANSI_GREEN = "\u001b[32m"
ANSI_RED = "\u001b[31m"

EVENT_TYPE_OPTIONS = [
    ("Normal", "Normal"),
    ("Mini", "Mini"),
    ("Arcade", "Arcade"),
    ("Tournament", "Tournament"),
    ("Training", "Training"),
]

AIRFRAME_OPTIONS = [
    ("F/A-26B GPA", "airframe:fa26b", "F/A-26B"),
    ("AV-42C GPA", "airframe:av42c", "AV-42C"),
    ("F-45A GPA", "airframe:f45a", "F-45A"),
    ("EF-24G GPA", "airframe:ef24g", "EF-24G"),
    ("T-55 GPA", "airframe:t55", "T-55"),
    ("AH-94 GPA", "airframe:ah94", "AH-94"),
]

LEADERBOARD_TYPES = [
    ("GPA", "gpa", None),
    *AIRFRAME_OPTIONS,
    ("Combat Deaths", "combat_deaths", None),
    ("Unique Ops Attended", "unique_ops", None),
    ("Ops Attended", "ops_attended", None),
    ("Survival Rate", "survival_rate", None),
]

LEADERBOARD_TYPE_LABELS = {
    value: label
    for label, value, _airframe in LEADERBOARD_TYPES
}

AIRFRAME_BY_TYPE = {
    value: airframe
    for _label, value, airframe in AIRFRAME_OPTIONS
}

VISIBILITY_LABELS = {
    "show": "Show",
    "private": "Private",
}

EXPORT_LABELS = {
    "yes": "Yes",
    "no": "No",
}


def today_utc_date() -> date:
    return datetime.now(timezone.utc).date()


ALL_TIME_START_DATE = date(1970, 1, 1)


def default_start_date() -> date:
    return ALL_TIME_START_DATE


def date_label(value: date, *, is_start: bool = False, is_end: bool = False) -> str:
    if is_start and value <= ALL_TIME_START_DATE:
        return "All Time"

    if is_end and value == today_utc_date():
        return "Now"

    return value.isoformat()


def date_to_start_ts(value: date) -> int:
    return int(datetime.combine(value, time.min, tzinfo=timezone.utc).timestamp())


def date_to_end_ts(value: date) -> int:
    return int(datetime.combine(value, time.max, tzinfo=timezone.utc).timestamp())


def parse_date_input(value: str, *, field: str = "start") -> date:
    text = str(value or "").strip().casefold()

    if text in {"all", "alltime", "all-time", "all time"}:
        return ALL_TIME_START_DATE if field == "start" else today_utc_date()

    if text in {"now", "today"}:
        return today_utc_date()

    return datetime.strptime(text, "%Y-%m-%d").date()


def clean_text(value: Any, fallback: str = "") -> str:
    text = str(value or "").strip()
    return text or fallback


def short_name(value: Any, max_len: int = 24) -> str:
    text = clean_text(value, "Unknown")
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def normalize_aircraft_key(value: Any) -> str:
    text = str(value or "").casefold()
    text = re.sub(r"[^a-z0-9]+", "", text)

    aliases = {
        "fa26": "fa26b",
        "fa26b": "fa26b",
        "f26": "fa26b",
        "f26b": "fa26b",
        "av42": "av42c",
        "av42c": "av42c",
        "f45": "f45a",
        "f45a": "f45a",
        "ef24": "ef24g",
        "ef24g": "ef24g",
        "t55": "t55",
        "ah94": "ah94",
    }

    return aliases.get(text, text)


@dataclass
class LeaderboardRow:
    discord_id: str
    player_name: str
    value: float | int
    detail: str



def apply_live_guild_display_names(
    guild: discord.Guild | None,
    rows: list[LeaderboardRow],
) -> list[LeaderboardRow]:
    """Prefer cached live server display names without blocking the Create button.

    This intentionally does not call guild.fetch_member() because doing one HTTP
    fetch per leaderboard row can make the /leaderboard Create button appear to
    do nothing while Discord rate-limits or waits on member fetches.
    """
    resolved_rows: list[LeaderboardRow] = []

    for row in rows:
        member = None

        if guild is not None:
            try:
                member = guild.get_member(int(row.discord_id))
            except (TypeError, ValueError):
                member = None

        raw_name = getattr(member, "display_name", None) if member is not None else row.player_name

        resolved_rows.append(
            LeaderboardRow(
                discord_id=row.discord_id,
                player_name=prune_display_name(raw_name, fallback=row.player_name or row.discord_id),
                value=row.value,
                detail=row.detail,
            )
        )

    return resolved_rows


@dataclass
class LeaderboardDraft:
    owner_id: int
    leaderboard_type: str | None = None
    event_types: list[str] = field(default_factory=list)
    visibility: str | None = "private"
    export: str | None = "no"
    start_date: date = field(default_factory=default_start_date)
    end_date: date = field(default_factory=today_utc_date)
    length: int = 25
    min_wires: int = 8

    def is_ready(self) -> bool:
        return bool(
            self.leaderboard_type
            and self.event_types
            and self.visibility
            and self.export
            and self.start_date
            and self.end_date
            and self.length > 0
            and self.min_wires >= 0
            and self.start_date <= self.end_date
        )

    def visibility_label(self) -> str:
        return VISIBILITY_LABELS.get(str(self.visibility or ""), "Missing")

    def export_label(self) -> str:
        return EXPORT_LABELS.get(str(self.export or ""), "Missing")

    def leaderboard_label(self) -> str:
        return LEADERBOARD_TYPE_LABELS.get(str(self.leaderboard_type or ""), "Missing")

    def event_type_label(self) -> str:
        return ", ".join(self.event_types) if self.event_types else "Missing"


def ansi_status(label: str, value: str, valid: bool) -> str:
    color = ANSI_GREEN if valid else ANSI_RED
    return f"{color}{label}: {value}{ANSI_RESET}"


def wizard_status_block(draft: LeaderboardDraft) -> str:
    lines = [
        ansi_status("Leaderboard Type", draft.leaderboard_label(), bool(draft.leaderboard_type)),
        ansi_status("Event Types", draft.event_type_label(), bool(draft.event_types)),
        ansi_status("Visibility", draft.visibility_label(), bool(draft.visibility)),
        ansi_status("Export", draft.export_label(), bool(draft.export)),
        ansi_status("Start Date", date_label(draft.start_date, is_start=True), True),
        ansi_status("End Date", date_label(draft.end_date, is_end=True), draft.start_date <= draft.end_date),
        ansi_status("Leaderboard Length", str(draft.length), draft.length > 0),
        ansi_status("Min Wires", str(draft.min_wires), draft.min_wires >= 0),
    ]
    return "```ansi\n" + "\n".join(lines) + "\n```"


def build_wizard_embed(draft: LeaderboardDraft) -> discord.Embed:
    embed = discord.Embed(
        title="Leaderboard Creator",
        description=(
            wizard_status_block(draft)
            + "\nUse the dropdowns below, then press **Create**.\n"
            + "Settings are optional. Defaults are all time through now, length 25, min wires 8."
        ),
    )
    embed.set_footer(text="Public Show leaderboards use a gold/yellow sidebar.")
    return embed


def player_name_expression() -> str:
    return (
        "COALESCE("
        "NULLIF(u.display_name, ''), "
        "NULLIF(a.user_name, ''), "
        "NULLIF(u.discord_username, ''), "
        "a.discord_id"
        ")"
    )


def fetch_leaderboard_attendance_rows(
    *,
    start_ts: int,
    end_ts: int,
    event_types: list[str],
) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in event_types)
    params: list[Any] = [int(start_ts), int(end_ts), *event_types]

    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT
                a.discord_id,
                {player_name_expression()} AS player_name,
                a.combat_deaths,
                a.landing_type,
                a.wires,
                a.bolters,
                a.aircraft,
                oe.event_id,
                oe.scheduled_at,
                ot.id AS op_template_id,
                ot.name AS op_template_name,
                ot.type AS op_type
            FROM attendance a
            JOIN op_events oe
                ON oe.event_id = a.scheduled_op_id
            JOIN op_templates ot
                ON ot.id = oe.op_template_id
            LEFT JOIN users u
                ON u.discord_id = a.discord_id
            WHERE oe.status = 'Complete'
              AND oe.scheduled_at >= ?
              AND oe.scheduled_at <= ?
              AND ot.type IN ({placeholders})
              AND a.status IN ('submitted', 'complete')
              AND NULLIF(TRIM(a.discord_id), '') IS NOT NULL
            """,
            params,
        ).fetchall()

    return [dict(row) for row in rows]


def gpa_attempt_score(row: dict[str, Any]) -> tuple[int, float]:
    if str(row.get("landing_type") or "") != "Arrested":
        return 0, 0.0

    attempts = 0
    score = 0.0

    try:
        wires = int(row.get("wires")) if row.get("wires") is not None else None
    except (TypeError, ValueError):
        wires = None

    try:
        bolters = int(row.get("bolters") or 0)
    except (TypeError, ValueError):
        bolters = 0

    if wires in {1, 2, 3, 4}:
        attempts += 1
        score += float(wire_score(wires) or 0.0)

    if bolters > 0:
        attempts += bolters
        score += float(bolters) * float(bolter_score())

    return attempts, score


def build_leaderboard_rows(
    *,
    draft: LeaderboardDraft,
) -> list[LeaderboardRow]:
    start_ts = date_to_start_ts(draft.start_date)
    end_ts = date_to_end_ts(draft.end_date)
    rows = fetch_leaderboard_attendance_rows(
        start_ts=start_ts,
        end_ts=end_ts,
        event_types=draft.event_types,
    )

    leaderboard_type = str(draft.leaderboard_type or "")
    limit = max(1, min(500, int(draft.length or 25)))

    if leaderboard_type in {"gpa", *AIRFRAME_BY_TYPE.keys()}:
        wanted_airframe = AIRFRAME_BY_TYPE.get(leaderboard_type)
        wanted_key = normalize_aircraft_key(wanted_airframe) if wanted_airframe else None
        totals: dict[str, dict[str, Any]] = {}

        for row in rows:
            if wanted_key and normalize_aircraft_key(row.get("aircraft")) != wanted_key:
                continue

            attempts, score = gpa_attempt_score(row)
            if attempts <= 0:
                continue

            discord_id = str(row["discord_id"])
            value = totals.setdefault(
                discord_id,
                {
                    "discord_id": discord_id,
                    "player_name": prune_display_name(clean_text(row.get("player_name"), discord_id), fallback=discord_id),
                    "attempts": 0,
                    "score": 0.0,
                },
            )
            value["attempts"] += attempts
            value["score"] += score

        leaders = [
            LeaderboardRow(
                discord_id=str(value["discord_id"]),
                player_name=prune_display_name(value["player_name"], fallback=value.get("discord_id")),
                value=float(value["score"]) / float(value["attempts"]),
                detail=f"{int(value['attempts'])} attempts",
            )
            for value in totals.values()
            if int(value["attempts"]) >= int(draft.min_wires)
        ]

        return sorted(
            leaders,
            key=lambda row: (-float(row.value), -int(row.detail.split()[0]), row.player_name.casefold()),
        )[:limit]

    if leaderboard_type == "combat_deaths":
        totals: dict[str, dict[str, Any]] = {}

        for row in rows:
            discord_id = str(row["discord_id"])
            value = totals.setdefault(
                discord_id,
                {
                    "discord_id": discord_id,
                    "player_name": prune_display_name(clean_text(row.get("player_name"), discord_id), fallback=discord_id),
                    "deaths": 0,
                    "ops": set(),
                },
            )
            value["deaths"] += int(row.get("combat_deaths") or 0)
            value["ops"].add(int(row["event_id"]))

        leaders = [
            LeaderboardRow(
                discord_id=str(value["discord_id"]),
                player_name=prune_display_name(value["player_name"], fallback=value.get("discord_id")),
                value=int(value["deaths"]),
                detail=f"{len(value['ops'])} ops",
            )
            for value in totals.values()
            if int(value["deaths"]) > 0
        ]

        return sorted(
            leaders,
            key=lambda row: (-int(row.value), row.player_name.casefold()),
        )[:limit]

    if leaderboard_type == "unique_ops":
        totals: dict[str, dict[str, Any]] = {}

        for row in rows:
            discord_id = str(row["discord_id"])
            value = totals.setdefault(
                discord_id,
                {
                    "discord_id": discord_id,
                    "player_name": prune_display_name(clean_text(row.get("player_name"), discord_id), fallback=discord_id),
                    "templates": set(),
                    "events": set(),
                },
            )
            value["templates"].add(int(row["op_template_id"]))
            value["events"].add(int(row["event_id"]))

        leaders = [
            LeaderboardRow(
                discord_id=str(value["discord_id"]),
                player_name=prune_display_name(value["player_name"], fallback=value.get("discord_id")),
                value=len(value["templates"]),
                detail=f"{len(value['events'])} events",
            )
            for value in totals.values()
        ]

        return sorted(
            leaders,
            key=lambda row: (-int(row.value), row.player_name.casefold()),
        )[:limit]

    if leaderboard_type == "ops_attended":
        totals: dict[str, dict[str, Any]] = {}

        for row in rows:
            discord_id = str(row["discord_id"])
            value = totals.setdefault(
                discord_id,
                {
                    "discord_id": discord_id,
                    "player_name": prune_display_name(clean_text(row.get("player_name"), discord_id), fallback=discord_id),
                    "events": set(),
                },
            )
            value["events"].add(int(row["event_id"]))

        leaders = [
            LeaderboardRow(
                discord_id=str(value["discord_id"]),
                player_name=prune_display_name(value["player_name"], fallback=value.get("discord_id")),
                value=len(value["events"]),
                detail="ops",
            )
            for value in totals.values()
        ]

        return sorted(
            leaders,
            key=lambda row: (-int(row.value), row.player_name.casefold()),
        )[:limit]

    if leaderboard_type == "survival_rate":
        per_user_event: dict[tuple[str, int], dict[str, Any]] = {}

        for row in rows:
            discord_id = str(row["discord_id"])
            event_id = int(row["event_id"])
            key = (discord_id, event_id)
            value = per_user_event.setdefault(
                key,
                {
                    "discord_id": discord_id,
                    "player_name": prune_display_name(clean_text(row.get("player_name"), discord_id), fallback=discord_id),
                    "has_death": False,
                },
            )

            if int(row.get("combat_deaths") or 0) > 0:
                value["has_death"] = True

        totals: dict[str, dict[str, Any]] = {}

        for value in per_user_event.values():
            discord_id = str(value["discord_id"])
            total = totals.setdefault(
                discord_id,
                {
                    "discord_id": discord_id,
                    "player_name": prune_display_name(value["player_name"], fallback=value.get("discord_id")),
                    "ops": 0,
                    "survived": 0,
                },
            )
            total["ops"] += 1
            if not bool(value["has_death"]):
                total["survived"] += 1

        leaders = [
            LeaderboardRow(
                discord_id=str(value["discord_id"]),
                player_name=prune_display_name(value["player_name"], fallback=value.get("discord_id")),
                value=(float(value["survived"]) / float(value["ops"])) if int(value["ops"]) else 0.0,
                detail=f"{int(value['survived'])}/{int(value['ops'])} survived",
            )
            for value in totals.values()
            if int(value["ops"]) > 0
        ]

        return sorted(
            leaders,
            key=lambda row: (-float(row.value), row.player_name.casefold()),
        )[:limit]

    raise ValueError("Unknown leaderboard type.")


def format_row_value(draft: LeaderboardDraft, row: LeaderboardRow) -> str:
    leaderboard_type = str(draft.leaderboard_type or "")

    if leaderboard_type in {"gpa", *AIRFRAME_BY_TYPE.keys()}:
        return f"{float(row.value):.3f} GPA ({row.detail})"

    if leaderboard_type == "survival_rate":
        return f"{float(row.value) * 100:.1f}% ({row.detail})"

    if leaderboard_type == "combat_deaths":
        return f"{int(row.value)} deaths ({row.detail})"

    if leaderboard_type == "unique_ops":
        return f"{int(row.value)} unique ops ({row.detail})"

    if leaderboard_type == "ops_attended":
        return f"{int(row.value)} ops"

    return str(row.value)


def leaderboard_lines(draft: LeaderboardDraft, rows: list[LeaderboardRow]) -> list[str]:
    if not rows:
        return ["No qualifying records found."]

    lines: list[str] = []

    for index, row in enumerate(rows, start=1):
        lines.append(
            f"{index:>2}. {short_name(row.player_name, 24):<24} {format_row_value(draft, row)}"
        )

    return lines


def code_block_from_lines(lines: list[str], max_chars: int = 3900) -> str:
    output: list[str] = []
    current = "```text\n"

    for line in lines:
        candidate = current + line + "\n"
        if len(candidate) + len("```") > max_chars:
            break
        current = candidate
        output.append(line)

    if not output:
        output = [lines[0] if lines else "No data."]

    return "```text\n" + "\n".join(output) + "\n```"


def build_result_embed(
    *,
    draft: LeaderboardDraft,
    rows: list[LeaderboardRow],
    public: bool,
) -> discord.Embed:
    title = draft.leaderboard_label()
    visible_lines = leaderboard_lines(draft, rows)[:25]
    hidden_note = ""

    if len(rows) > len(visible_lines):
        hidden_note = f"\nShowing first {len(visible_lines)} of {len(rows)} rows. Use Export for the full list."

    embed = discord.Embed(
        title=title,
        description=(
            f"**Dates:** {draft.start_date.isoformat()} → {draft.end_date.isoformat()}\n"
            f"**Event Types:** {draft.event_type_label()}\n"
            f"**Min Wires:** {draft.min_wires}\n"
            f"**Rows:** {len(rows)} requested / {len(visible_lines)} displayed"
            f"{hidden_note}\n\n"
            f"{code_block_from_lines(visible_lines)}"
        ),
        color=discord.Color.gold() if public else discord.Color.blue(),
    )

    footer = "Calculated from completed attendance records."
    if str(draft.leaderboard_type or "") in {"gpa", *AIRFRAME_BY_TYPE.keys()}:
        footer = "GPA scale: " + gpa_scale_footer_text()

    embed.set_footer(text=footer)
    return embed


def build_export_text(
    *,
    draft: LeaderboardDraft,
    rows: list[LeaderboardRow],
) -> str:
    lines = [
        draft.leaderboard_label(),
        f"Dates: {draft.start_date.isoformat()} -> {draft.end_date.isoformat()}",
        f"Event Types: {draft.event_type_label()}",
        f"Visibility: {draft.visibility_label()}",
        f"Min Wires: {draft.min_wires}",
        f"Rows: {len(rows)}",
        "",
        *leaderboard_lines(draft, rows),
        "",
    ]

    if str(draft.leaderboard_type or "") in {"gpa", *AIRFRAME_BY_TYPE.keys()}:
        lines.append("GPA scale: " + gpa_scale_footer_text())

    return "\n".join(lines)


def export_file_for_result(draft: LeaderboardDraft, rows: list[LeaderboardRow]) -> discord.File:
    safe_name = re.sub(r"[^a-zA-Z0-9_-]+", "_", draft.leaderboard_label()).strip("_").lower()
    data = build_export_text(draft=draft, rows=rows).encode("utf-8")

    return discord.File(
        fp=io.BytesIO(data),
        filename=f"{safe_name or 'leaderboard'}_{draft.start_date.isoformat()}_{draft.end_date.isoformat()}.txt",
    )


class LeaderboardTypeSelect(discord.ui.Select):
    def __init__(self, draft: LeaderboardDraft):
        options = [
            discord.SelectOption(
                label=label,
                value=value,
                default=draft.leaderboard_type == value,
            )
            for label, value, _airframe in LEADERBOARD_TYPES
        ]

        super().__init__(
            placeholder="Leaderboard type",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, LeaderboardWizardView)

        self.view.draft.leaderboard_type = self.values[0]
        await self.view.refresh(interaction)


class EventTypesSelect(discord.ui.Select):
    def __init__(self, draft: LeaderboardDraft):
        selected = set(draft.event_types)
        options = [
            discord.SelectOption(
                label=label,
                value=value,
                default=value in selected,
            )
            for label, value in EVENT_TYPE_OPTIONS
        ]

        super().__init__(
            placeholder="Event Types",
            min_values=1,
            max_values=len(options),
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, LeaderboardWizardView)

        selected_order = [
            value
            for _label, value in EVENT_TYPE_OPTIONS
            if value in set(self.values)
        ]
        self.view.draft.event_types = selected_order
        await self.view.refresh(interaction)


class VisibilitySelect(discord.ui.Select):
    def __init__(self, draft: LeaderboardDraft):
        options = [
            discord.SelectOption(
                label="Show",
                value="show",
                description="Post the leaderboard publicly in this channel.",
                default=draft.visibility == "show",
            ),
            discord.SelectOption(
                label="Private",
                value="private",
                description="Only you can see the leaderboard.",
                default=draft.visibility == "private",
            ),
        ]

        super().__init__(
            placeholder="Visibility",
            min_values=1,
            max_values=1,
            options=options,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, LeaderboardWizardView)

        self.view.draft.visibility = self.values[0]
        await self.view.refresh(interaction)


class ExportSelect(discord.ui.Select):
    def __init__(self, draft: LeaderboardDraft):
        options = [
            discord.SelectOption(
                label="Yes",
                value="yes",
                description="Attach a text export file.",
                default=draft.export == "yes",
            ),
            discord.SelectOption(
                label="No",
                value="no",
                description="Only show the Discord message.",
                default=draft.export == "no",
            ),
        ]

        super().__init__(
            placeholder="Export",
            min_values=1,
            max_values=1,
            options=options,
            row=3,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, LeaderboardWizardView)

        self.view.draft.export = self.values[0]
        await self.view.refresh(interaction)


class ExitLeaderboardButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Exit",
            style=discord.ButtonStyle.secondary,
            row=4,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content="Message dismissed.",
            embed=None,
            view=None,
        )


class LeaderboardSettingsButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Settings",
            style=discord.ButtonStyle.primary,
            row=4,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, LeaderboardWizardView)
        await interaction.response.send_modal(LeaderboardSettingsModal(self.view))


class CreateLeaderboardButton(discord.ui.Button):
    def __init__(self, draft: LeaderboardDraft):
        super().__init__(
            label="Create",
            style=discord.ButtonStyle.success if draft.is_ready() else discord.ButtonStyle.secondary,
            disabled=not draft.is_ready(),
            row=4,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, LeaderboardWizardView)

        if not self.view.draft.is_ready():
            await interaction.response.send_message(
                "Finish the required leaderboard settings first.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            rows = build_leaderboard_rows(draft=self.view.draft)
            rows = apply_live_guild_display_names(interaction.guild, rows)
            public = self.view.draft.visibility == "show"
            embed = build_result_embed(
                draft=self.view.draft,
                rows=rows,
                public=public,
            )
            file = (
                export_file_for_result(self.view.draft, rows)
                if self.view.draft.export == "yes"
                else None
            )
        except Exception as error:
            await interaction.followup.send(
                f"Could not build leaderboard: `{type(error).__name__}: {error}`",
                ephemeral=True,
            )
            return

        if self.view.draft.visibility == "show":
            kwargs: dict[str, Any] = {"embed": embed}
            if file is not None:
                kwargs["file"] = file

            await interaction.channel.send(**kwargs)
            await interaction.edit_original_response(
                content="Leaderboard posted.",
                embed=None,
                view=None,
            )
            return

        kwargs = {
            "embed": embed,
            "ephemeral": True,
        }
        if file is not None:
            kwargs["file"] = file

        await interaction.followup.send(**kwargs)
        await interaction.edit_original_response(
            content="Leaderboard created.",
            embed=None,
            view=None,
        )


class LeaderboardSettingsModal(discord.ui.Modal):
    def __init__(self, parent_view: "LeaderboardWizardView"):
        super().__init__(title="Leaderboard Settings")
        self.parent_view = parent_view

        self.start_date_input = discord.ui.TextInput(
            label="Start Date",
            required=False,
            max_length=10,
            default=date_label(parent_view.draft.start_date, is_start=True).casefold().replace(" time", ""),
            placeholder="all or YYYY-MM-DD",
        )
        self.end_date_input = discord.ui.TextInput(
            label="End Date",
            required=False,
            max_length=10,
            default=date_label(parent_view.draft.end_date, is_end=True).casefold(),
            placeholder="now or YYYY-MM-DD",
        )
        self.length_input = discord.ui.TextInput(
            label="Leaderboard Length",
            required=False,
            max_length=4,
            default=str(parent_view.draft.length),
            placeholder="25",
        )
        self.min_wires_input = discord.ui.TextInput(
            label="Min Wires",
            required=False,
            max_length=4,
            default=str(parent_view.draft.min_wires),
            placeholder="8",
        )

        self.add_item(self.start_date_input)
        self.add_item(self.end_date_input)
        self.add_item(self.length_input)
        self.add_item(self.min_wires_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            start = (
                parse_date_input(str(self.start_date_input.value), field="start")
                if str(self.start_date_input.value or "").strip()
                else default_start_date()
            )
            end = (
                parse_date_input(str(self.end_date_input.value), field="end")
                if str(self.end_date_input.value or "").strip()
                else today_utc_date()
            )
            length = (
                int(str(self.length_input.value).strip())
                if str(self.length_input.value or "").strip()
                else 25
            )
            min_wires = (
                int(str(self.min_wires_input.value).strip())
                if str(self.min_wires_input.value or "").strip()
                else 8
            )
        except Exception:
            await interaction.response.send_message(
                "Settings dates must use `YYYY-MM-DD`, `all`, or `now`; length and min wires must be whole numbers.",
                ephemeral=True,
            )
            return

        if start > end:
            await interaction.response.send_message(
                "Start date cannot be after end date.",
                ephemeral=True,
            )
            return

        if length < 1 or length > 500:
            await interaction.response.send_message(
                "Leaderboard length must be between 1 and 500.",
                ephemeral=True,
            )
            return

        if min_wires < 0 or min_wires > 500:
            await interaction.response.send_message(
                "Min wires must be between 0 and 500.",
                ephemeral=True,
            )
            return

        self.parent_view.draft.start_date = start
        self.parent_view.draft.end_date = end
        self.parent_view.draft.length = length
        self.parent_view.draft.min_wires = min_wires

        await self.parent_view.refresh(interaction)


class LeaderboardWizardView(discord.ui.View):
    def __init__(self, draft: LeaderboardDraft):
        super().__init__(timeout=900)
        self.draft = draft

        self.add_item(LeaderboardTypeSelect(draft))
        self.add_item(EventTypesSelect(draft))
        self.add_item(VisibilitySelect(draft))
        self.add_item(ExportSelect(draft))
        self.add_item(ExitLeaderboardButton())
        self.add_item(LeaderboardSettingsButton())
        self.add_item(CreateLeaderboardButton(draft))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.draft.owner_id:
            await interaction.response.send_message(
                "Only the person who opened this leaderboard wizard can use it.",
                ephemeral=True,
            )
            return False

        return True

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=build_wizard_embed(self.draft),
            view=LeaderboardWizardView(self.draft),
        )


class LeaderboardCog(commands.Cog):
    """Maintains persistent rolling-stat messages and creates custom leaderboards."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._startup_rebuild_started = False
        self.periodic_refresh.start()

    def cog_unload(self):
        self.periodic_refresh.cancel()

    @app_commands.command(
        name="leaderboard",
        description="Create a custom leaderboard.",
    )
    @app_commands.guild_only()
    async def leaderboard(
        self,
        interaction: discord.Interaction,
    ):
        if not await require_mission_qualified_command(interaction):
            return

        draft = LeaderboardDraft(owner_id=interaction.user.id)

        await interaction.response.send_message(
            embed=build_wizard_embed(draft),
            view=LeaderboardWizardView(draft),
            ephemeral=True,
        )

    @commands.Cog.listener()
    async def on_ready(self):
        # A full startup rebuild recalculates automatic rewards, deletes tracked
        # leaderboard messages, and recreates the rolling snapshot from the DB.
        if self._startup_rebuild_started:
            return

        self._startup_rebuild_started = True
        queue_leaderboard_startup_rebuild(self.bot)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload):
        if payload.guild_id is None:
            return

        if not is_tracked_leaderboard_message(
            channel_id=payload.channel_id,
            message_id=payload.message_id,
        ):
            return

        queue_leaderboard_refresh(
            self.bot,
            reason=f"leaderboard message deleted:{payload.message_id}",
        )

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload):
        if payload.guild_id is None:
            return

        if not any(
            is_tracked_leaderboard_message(
                channel_id=payload.channel_id,
                message_id=message_id,
            )
            for message_id in payload.message_ids
        ):
            return

        queue_leaderboard_refresh(
            self.bot,
            reason="leaderboard messages bulk deleted",
        )

    @tasks.loop(hours=6)
    async def periodic_refresh(self):
        # This is a normal refresh. It does not delete/repost boards.
        # It catches any data correction that happened outside a command hook.
        from services.reward_service import reconcile_auto_rewards

        reconcile_auto_rewards()
        queue_leaderboard_refresh_now(self.bot)

    @periodic_refresh.before_loop
    async def before_periodic_refresh(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(LeaderboardCog(bot))
