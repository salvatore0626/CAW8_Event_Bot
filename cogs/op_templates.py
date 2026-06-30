from __future__ import annotations

import re
import time
import traceback
import json
from dataclasses import dataclass, field
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from database import get_connection
from services.permission_service import (
    require_mission_executer_command,
    member_is_admin,
)
try:
    from config import OP_TYPES as CONFIG_OP_TYPES
except ImportError:
    CONFIG_OP_TYPES = ["Normal", "Mini", "Arcade", "Tournament"]

OP_TYPES = list(dict.fromkeys([*CONFIG_OP_TYPES, "Training"]))

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

from services.op_template_service import (
    FlightTemplateDraft,
    OpTemplateEditDraft,
    auto_flight_letter_from_name,
    clean_text,
    creator_display_name,
    get_active_airframes,
    list_op_templates,
    load_edit_draft,
    normalize_flight_name,
    normalize_op_name,
    player_slot_values_for_flight,
    resize_flights,
    save_edit_draft,
    validate_edit_draft,
)


# Shared creation wizard used by /op templates → Create.
# Kept in this cog so template list/create/edit remain together.
try:
    from config import AIRCRAFT_OPTIONS
except ImportError:
    AIRCRAFT_OPTIONS = [
        {"name": "AV-42C", "max_seats": 1},
        {"name": "F/A-26", "max_seats": 1},
        {"name": "F-45A", "max_seats": 1},
        {"name": "EF-24", "max_seats": 2},
        {"name": "T-55", "max_seats": 2},
        {"name": "AH-94", "max_seats": 2},
    ]


DEFAULT_AIRCRAFT_MAX_SEATS = {
    "av-42c": 1,
    "f/a-26": 1,
    "fa-26": 1,
    "f-45a": 1,
    "ef-24": 2,
    "ef-24g": 2,
    "t-55": 2,
    "ah-94": 2,
}


@dataclass
class FlightDraft:
    flight_index: int

    flight_letter: Optional[str] = None
    flight_name: Optional[str] = None
    description: Optional[str] = None

    airframe_id: Optional[int] = None
    airframe_name: Optional[str] = None

    aircraft_count: Optional[int] = None
    player_slots: Optional[int] = None


@dataclass
class OpTemplateDraft:
    created_by: int

    name: Optional[str] = None
    description: Optional[str] = None
    op_type: Optional[str] = None
    briefing: Optional[str] = None
    creator: Optional[str] = None

    total_players: Optional[int] = 16
    flight_count: Optional[int] = None

    current_flight_index: int = 0
    flights: list[FlightDraft] = field(default_factory=list)

    def rebuild_flights(self):
        self.flights = [
            FlightDraft(flight_index=i + 1)
            for i in range(self.flight_count or 0)
        ]
        self.current_flight_index = 0

    def is_op_info_complete(self) -> bool:
        return bool(
            self.name
            and self.op_type
            and self.total_players
            and self.flight_count
        )

    def is_flight_complete(self, index: int) -> bool:
        return not validate_flight_setup(self, index)

    def total_flight_slots(self) -> int:
        return sum(
            flight.player_slots or 0
            for flight in self.flights
        )

    def validate_for_confirm(self) -> list[str]:
        errors = []

        if not self.is_op_info_complete():
            errors.append("Operation info is incomplete.")

        if not self.flights:
            errors.append("No flights have been created yet.")

        if self.flight_count and len(self.flights) != self.flight_count:
            errors.append("Flight count does not match the amount of flight data.")

        letters = []

        for index, flight in enumerate(self.flights):
            if flight.flight_letter:
                letters.append(flight.flight_letter)

            errors.extend(validate_flight_setup(self, index))

        if len(letters) != len(set(letters)):
            errors.append("Flight letters cannot repeat inside the same op template.")

        # Flight templates may intentionally contain more selectable player slots
        # than the scheduled op max players. Example: 5 flights with 4 seats each
        # for a 16-player op where only some flights are used.
        return errors


active_op_wizards: dict[int, OpTemplateDraft] = {}


def normalize_aircraft_name(value: str | None) -> str:
    return " ".join(str(value or "").strip().split()).lower()


def aircraft_option_name(option) -> str:
    if isinstance(option, dict):
        return str(option.get("name") or option.get("label") or option.get("value") or "")

    if isinstance(option, (tuple, list)) and option:
        return str(option[0])

    return str(option or "")


def aircraft_option_max_seats(option) -> int | None:
    if isinstance(option, dict):
        try:
            seats = int(option.get("max_seats"))
        except Exception:
            return None

        return max(1, seats)

    if isinstance(option, (tuple, list)) and len(option) >= 2:
        try:
            seats = int(option[1])
        except Exception:
            return None

        return max(1, seats)

    return None


def aircraft_names_match(stored_aircraft: str | None, option_aircraft: str | None) -> bool:
    stored = normalize_aircraft_name(stored_aircraft)
    option = normalize_aircraft_name(option_aircraft)

    if not stored or not option:
        return False

    return stored == option or stored.startswith(option) or option.startswith(stored)


def fallback_max_seats_for_aircraft(aircraft_name: str | None) -> int:
    wanted = normalize_aircraft_name(aircraft_name)

    if not wanted:
        return 1

    for key, seats in DEFAULT_AIRCRAFT_MAX_SEATS.items():
        if wanted == key or wanted.startswith(key) or key.startswith(wanted):
            return int(seats)

    return 1


def max_seats_for_aircraft(aircraft_name: str | None) -> int:
    wanted = str(aircraft_name or "").strip()

    if not wanted:
        return 1

    for option in AIRCRAFT_OPTIONS:
        option_name = aircraft_option_name(option)

        if aircraft_names_match(wanted, option_name):
            configured = aircraft_option_max_seats(option)

            if configured is not None:
                return configured

            return fallback_max_seats_for_aircraft(wanted)

    return fallback_max_seats_for_aircraft(wanted)


def player_slot_range_for_flight(flight: FlightDraft) -> tuple[int | None, int | None]:
    if not flight.aircraft_count:
        return None, None

    min_slots = int(flight.aircraft_count)
    max_slots = min_slots * max_seats_for_aircraft(flight.airframe_name)

    return min_slots, max_slots


def valid_player_slot_values_for_flight(flight: FlightDraft) -> list[int]:
    min_slots, max_slots = player_slot_range_for_flight(flight)

    if min_slots is None or max_slots is None:
        return []

    return list(range(min_slots, max_slots + 1))


def adjust_player_slots_to_aircraft_limits(flight: FlightDraft) -> None:
    valid_values = valid_player_slot_values_for_flight(flight)

    if not valid_values:
        return

    if len(valid_values) == 1:
        flight.player_slots = valid_values[0]
        return

    if flight.player_slots not in valid_values:
        flight.player_slots = None


def player_slot_range_text(flight: FlightDraft) -> str:
    min_slots, max_slots = player_slot_range_for_flight(flight)

    if min_slots is None or max_slots is None:
        return "Select airframe and aircraft count first."

    if min_slots == max_slots:
        return f"Locked to `{min_slots}` player slot(s) for this airframe/count."

    return f"Valid player slots: `{min_slots}-{max_slots}`."


def remaining_slots_for_flight(draft: OpTemplateDraft, flight: FlightDraft) -> int | None:
    if not draft.total_players:
        return None

    used_by_other_flights = 0

    for other_flight in draft.flights:
        if other_flight is flight:
            continue

        used_by_other_flights += int(other_flight.player_slots or 0)

    return max(0, int(draft.total_players) - used_by_other_flights)


def capped_player_slot_values_for_flight(draft: OpTemplateDraft, flight: FlightDraft) -> list[int]:
    # Do not cap a flight by remaining op max-player slots.
    # The op can have more selectable flight seats than the max scheduled players.
    return valid_player_slot_values_for_flight(flight)


def adjust_player_slots_to_aircraft_and_remaining_limits(
    draft: OpTemplateDraft,
    flight: FlightDraft,
) -> None:
    valid_values = capped_player_slot_values_for_flight(draft, flight)

    if not valid_values:
        flight.player_slots = None
        return

    if len(valid_values) == 1:
        flight.player_slots = valid_values[0]
        return

    if flight.player_slots not in valid_values:
        flight.player_slots = None


def player_slot_range_text_for_draft(draft: OpTemplateDraft, flight: FlightDraft) -> str:
    min_slots, max_slots = player_slot_range_for_flight(flight)

    if min_slots is None or max_slots is None:
        return "Select airframe and aircraft count first."

    if min_slots == max_slots:
        return f"Locked to `{min_slots}` player slot(s) for this flight."

    return f"Valid player slots: `{min_slots}-{max_slots}`."


def get_active_airframes() -> list[dict]:
    """Return airframe options in the row shape the opcreate UI expects.

    Older versions used a get_active_airframes() helper/table.
    Current bot config keeps the canonical aircraft list in AIRCRAFT_OPTIONS.
    """
    rows: list[dict] = []

    for index, option in enumerate(AIRCRAFT_OPTIONS, start=1):
        name = aircraft_option_name(option)

        if not name:
            continue

        rows.append(
            {
                "id": index,
                "display_name": name,
                "max_seats": max_seats_for_aircraft(name),
            }
        )

    return rows


def ensure_op_template_extra_columns(conn) -> None:
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(op_templates)").fetchall()
    }

    if "briefing" not in columns:
        conn.execute("ALTER TABLE op_templates ADD COLUMN briefing TEXT")

    if "creator" not in columns:
        conn.execute("ALTER TABLE op_templates ADD COLUMN creator TEXT")


def create_op_template_in_db(draft: OpTemplateDraft) -> int:
    """Persist an op template and its flight templates using the current SQLite schema."""
    ts = int(time.time())

    with get_connection() as conn:
        ensure_op_template_extra_columns(conn)

        cursor = conn.execute(
            """
            INSERT INTO op_templates (
                name,
                description,
                type,
                total_players,
                flight_count,
                briefing,
                creator,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (draft.name or "").strip().upper(),
                draft.description,
                draft.op_type or "Normal",
                int(draft.total_players or 0),
                int(draft.flight_count or 0),
                (draft.briefing or "").strip() or None,
                (draft.creator or str(draft.created_by)).strip() or str(draft.created_by),
                ts,
                ts,
            ),
        )

        op_template_id = int(cursor.lastrowid)

        for position, flight in enumerate(draft.flights, start=1):
            conn.execute(
                """
                INSERT INTO flight_templates (
                    op_template_id,
                    flight_index,
                    flight_letter,
                    flight_name,
                    aircraft,
                    aircraft_count,
                    slot_count,
                    description
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    op_template_id,
                    position,
                    flight.flight_letter,
                    flight.flight_name,
                    flight.airframe_name,
                    int(flight.aircraft_count or 0),
                    int(flight.player_slots or 0),
                    flight.description,
                ),
            )

        return op_template_id


def normalize_op_name(value: str) -> str:
    """
    Operation names are always stored/displayed in all caps.
    Examples:
    last drop -> LAST DROP
    Last Drop -> LAST DROP
    """
    return " ".join(str(value or "").strip().split()).upper()


def normalize_flight_name(value: str) -> str:
    """
    Normalize flight names to first letter uppercase and the rest lowercase.
    Examples:
    hammer -> Hammer
    HAMMer -> Hammer
    """
    clean = " ".join(str(value or "").strip().split())

    if not clean:
        return ""

    return clean[:1].upper() + clean[1:].lower()


def auto_flight_letter_from_name(value: str) -> str:
    clean = normalize_flight_name(value)

    if not clean:
        return ""

    # Use the first A-Z character in the normalized name.
    # This avoids punctuation/spaces accidentally becoming the letter.
    match = re.search(r"[A-Za-z]", clean)

    if not match:
        return ""

    return match.group(0).upper()


def slot_counter_text(draft: OpTemplateDraft) -> str:
    used = draft.total_flight_slots()
    max_players = draft.total_players or "?"
    remaining = (draft.total_players - used) if draft.total_players else "?"

    return f"`{used}/{max_players}` slots used\nRemaining: `{remaining}`"


def slot_counter_ratio_text(draft: OpTemplateDraft) -> str:
    used = draft.total_flight_slots()
    max_players = draft.total_players or "?"

    return f"{used}/{max_players}"


def validate_flight_setup(draft: OpTemplateDraft, index: int) -> list[str]:
    errors: list[str] = []
    flight = draft.flights[index]

    if not flight.flight_name:
        errors.append(f"Flight {flight.flight_index} is missing a name.")

    if not flight.flight_letter:
        errors.append(f"Flight {flight.flight_index} is missing an auto-assigned letter.")

    if flight.airframe_id is None:
        errors.append(f"Flight {flight.flight_index} is missing an airframe.")

    if not flight.aircraft_count:
        errors.append(f"Flight {flight.flight_index} is missing aircraft count.")

    if not flight.player_slots:
        errors.append(f"Flight {flight.flight_index} is missing player slots.")

    if flight.aircraft_count and flight.player_slots:
        min_slots, max_slots = player_slot_range_for_flight(flight)

        if min_slots is not None and max_slots is not None:
            if flight.player_slots < min_slots or flight.player_slots > max_slots:
                errors.append(
                    f"Flight {flight.flight_index} player slots must be within "
                    f"`{min_slots}-{max_slots}` for "
                    f"{flight.aircraft_count}x {flight.airframe_name or 'selected airframe'}. "
                    f"Selected slots: `{flight.player_slots}`."
                )

    return errors


ANSI_RESET = "\u001b[0m"
ANSI_GREEN = "\u001b[32m"
ANSI_RED = "\u001b[31m"
ANSI_YELLOW = "\u001b[33m"


def ansi_value(value, *, fallback: str = "Not set") -> tuple[str, bool]:
    if value is None:
        return fallback, False

    text = str(value).strip()

    if not text:
        return fallback, False

    return text, True


def ansi_status_line(label: str, value, *, fallback: str = "Not set") -> str:
    text, is_set = ansi_value(value, fallback=fallback)
    color = ANSI_GREEN if is_set else ANSI_RED
    return f"{color}{label}: {text}{ANSI_RESET}"


def ansi_optional_status_line(label: str, value, *, fallback: str = "Optional") -> str:
    text = str(value or "").strip() or fallback
    return f"{ANSI_YELLOW}{label}: {text}{ANSI_RESET}"


def creator_display_text(creator: str | int | None) -> str:
    raw = str(creator or "").strip()

    if not raw:
        return "Not set"

    try:
        return creator_display_name(raw) or raw
    except Exception:
        return raw


def ansi_plain_green_line(text: str) -> str:
    return f"{ANSI_GREEN}{text}{ANSI_RESET}"


def ansi_block(lines: list[str]) -> str:
    return "```ansi\n" + "\n".join(lines) + "\n```"


def build_op_info_embed(draft: OpTemplateDraft) -> discord.Embed:
    info_lines = [
        ansi_status_line("Name", draft.name),
        ansi_status_line("Type", draft.op_type),
        ansi_status_line("Max Players", draft.total_players),
        ansi_status_line("Flight Count", draft.flight_count),
        ansi_optional_status_line("Briefing", draft.briefing),
        ansi_status_line("Creator", creator_display_text(draft.creator or str(draft.created_by))),
    ]

    embed = discord.Embed(
        title="Create Operation Template",
        description=(
            f"{ansi_block(info_lines)}\n"
            f"**Description:**\n{draft.description or 'Not set'}"
        ),
    )

    return embed



def build_flight_embed(draft: OpTemplateDraft) -> discord.Embed:
    flight = draft.flights[draft.current_flight_index]

    flight_lines = [
        ansi_status_line("Flight Name", flight.flight_name),
        ansi_status_line("Letter", flight.flight_letter),
        ansi_status_line("Airframe", flight.airframe_name),
        ansi_status_line("Aircraft Count", flight.aircraft_count),
        ansi_status_line("Player Slots", flight.player_slots),
    ]

    description = (
        f"{ansi_block(flight_lines)}\n"
        f"**Description:**\n{flight.description or 'Not set'}\n\n"
        f"**Player Slot Range:**\n{player_slot_range_text_for_draft(draft, flight)}\n\n"
        f"Slots used: `{slot_counter_ratio_text(draft)}`"
    )

    embed = discord.Embed(
        title=f"Flight {flight.flight_index} of {draft.flight_count}",
        description=description,
    )

    flight_errors = validate_flight_setup(draft, draft.current_flight_index)
    if flight_errors:
        embed.add_field(
            name="Current Flight Checks",
            value="\n".join(f"- {error}" for error in flight_errors),
            inline=False,
        )

    return embed



def _clip_for_discord(value: str | None, limit: int) -> str:
    text = str(value or "").strip()

    if len(text) <= limit:
        return text

    return text[: max(0, limit - 15)].rstrip() + "\n... [truncated]"


def build_review_embed(draft: OpTemplateDraft) -> discord.Embed:
    errors = draft.validate_for_confirm()

    header_lines = [
        f"Name: {draft.name or 'Not set'}",
        f"Type: {draft.op_type or 'Not set'}",
        f"Max Players: {draft.total_players or 'Not set'}",
        f"Flight Count: {draft.flight_count or 'Not set'}",
        f"Selectable Flight Slots: {draft.total_flight_slots()}",
        ansi_optional_status_line("Briefing", draft.briefing),
        f"Creator: {creator_display_text(draft.creator or str(draft.created_by))}",
    ]

    embed = discord.Embed(
        title="Review Operation Template",
        description=ansi_block(header_lines),
    )

    embed.add_field(
        name="Operation Description",
        value=_clip_for_discord(draft.description or "Not set", 1000),
        inline=False,
    )

    for flight in draft.flights[:20]:
        flight_lines = [
            f"Letter: {flight.flight_letter or '?'}",
            f"Name: {flight.flight_name or 'Not set'}",
            f"Airframe: {flight.airframe_name or 'Not set'}",
            f"Aircraft Count: {flight.aircraft_count or 'Not set'}",
            f"Player Slots: {flight.player_slots or 'Not set'}",
        ]

        field_value = (
            ansi_block(flight_lines)
            + "\n"
            + _clip_for_discord(flight.description or "No description.", 650)
        )

        embed.add_field(
            name=f"Flight {flight.flight_index}",
            value=_clip_for_discord(field_value, 1024),
            inline=False,
        )

    if len(draft.flights) > 20:
        embed.add_field(
            name="Additional Flights",
            value=f"{len(draft.flights) - 20} more flight(s) not shown on this review page.",
            inline=False,
        )

    if errors:
        embed.add_field(
            name="Cannot Confirm Yet",
            value=_clip_for_discord("\n".join(f"- {error}" for error in errors), 1024),
            inline=False,
        )

    return embed



class RestrictedView(discord.ui.View):
    def __init__(self, owner_id: int, timeout: int = 900):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the staff member who started this wizard can use these buttons.",
                ephemeral=True,
            )
            return False

        return True


class CancelButton(discord.ui.Button):
    def __init__(self, draft: OpTemplateDraft, danger: bool = True, row: int = 4):
        super().__init__(
            label="Cancel",
            style=discord.ButtonStyle.danger if danger else discord.ButtonStyle.secondary,
            row=row,
        )
        self.draft = draft

    async def callback(self, interaction: discord.Interaction):
        active_op_wizards.pop(self.draft.created_by, None)

        state = TemplateListState(owner_id=self.draft.created_by)

        await interaction.response.edit_message(
            content=None,
            embed=build_templates_embed(state),
            view=TemplateListView(state),
        )


class OpInfoModal(discord.ui.Modal, title="Operation Info"):
    def __init__(self, draft: OpTemplateDraft):
        super().__init__()
        self.draft = draft

        self.name_input = discord.ui.TextInput(
            label="Operation Name",
            placeholder="Example: Operation Hammerfall",
            default=draft.name or "",
            max_length=100,
            required=True,
        )

        self.description_input = discord.ui.TextInput(
            label="Operation Description",
            placeholder="Short description of the operation.",
            default=draft.description or "",
            style=discord.TextStyle.paragraph,
            max_length=1000,
            required=False,
        )

        self.briefing_input = discord.ui.TextInput(
            label="Briefing Link",
            placeholder="Optional Google Slides link",
            default=draft.briefing or "",
            max_length=500,
            required=False,
        )

        self.creator_input = discord.ui.TextInput(
            label="Creator Discord ID",
            placeholder="Optional. Blank uses your Discord ID.",
            default=draft.creator or str(draft.created_by),
            max_length=30,
            required=False,
        )

        self.add_item(self.name_input)
        self.add_item(self.description_input)
        self.add_item(self.briefing_input)
        self.add_item(self.creator_input)

    async def on_submit(self, interaction: discord.Interaction):
        self.draft.name = str(self.name_input.value).strip()
        self.draft.description = str(self.description_input.value).strip()
        self.draft.briefing = str(self.briefing_input.value).strip() or None
        self.draft.creator = str(self.creator_input.value).strip() or str(self.draft.created_by)

        await interaction.response.edit_message(
            embed=build_op_info_embed(self.draft),
            view=OpInfoView(self.draft),
        )


class OpInfoButton(discord.ui.Button):
    def __init__(self, draft: OpTemplateDraft):
        super().__init__(
            label="Set Op Info",
            style=discord.ButtonStyle.primary,
            row=0,
        )
        self.draft = draft

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(OpInfoModal(self.draft))


class OpTypeSelect(discord.ui.Select):
    def __init__(self, draft: OpTemplateDraft):
        self.draft = draft

        options = [
            discord.SelectOption(
                label=op_type,
                value=op_type,
                default=draft.op_type == op_type,
            )
            for op_type in OP_TYPES
        ]

        super().__init__(
            placeholder="Select operation type",
            min_values=1,
            max_values=1,
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        self.draft.op_type = self.values[0]

        await interaction.response.edit_message(
            embed=build_op_info_embed(self.draft),
            view=OpInfoView(self.draft),
        )


class MaxPlayersSelect(discord.ui.Select):
    def __init__(self, draft: OpTemplateDraft):
        self.draft = draft

        options = [
            discord.SelectOption(
                label=f"Players: {i}",
                value=str(i),
                default=draft.total_players == i,
            )
            for i in range(1, 26)
        ]

        super().__init__(
            placeholder="Select max players",
            min_values=1,
            max_values=1,
            options=options,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction):
        self.draft.total_players = int(self.values[0])

        await interaction.response.edit_message(
            embed=build_op_info_embed(self.draft),
            view=OpInfoView(self.draft),
        )


class FlightCountSelect(discord.ui.Select):
    def __init__(self, draft: OpTemplateDraft):
        self.draft = draft

        options = [
            discord.SelectOption(
                label=f"Flights: {i}",
                value=str(i),
                default=draft.flight_count == i,
            )
            for i in range(1, 26)
        ]

        super().__init__(
            placeholder="Select number of flights",
            min_values=1,
            max_values=1,
            options=options,
            row=3,
        )

    async def callback(self, interaction: discord.Interaction):
        new_count = int(self.values[0])

        if self.draft.flight_count != new_count:
            self.draft.flight_count = new_count
            self.draft.rebuild_flights()

        await interaction.response.edit_message(
            embed=build_op_info_embed(self.draft),
            view=OpInfoView(self.draft),
        )


class OpInfoNextButton(discord.ui.Button):
    def __init__(self, draft: OpTemplateDraft):
        super().__init__(
            label="Next",
            style=discord.ButtonStyle.success,
            row=4,
        )
        self.draft = draft

    async def callback(self, interaction: discord.Interaction):
        if not self.draft.is_op_info_complete():
            await interaction.response.send_message(
                "Please finish the operation info before moving to flights.",
                ephemeral=True,
            )
            return

        if not self.draft.flights:
            self.draft.rebuild_flights()

        self.draft.current_flight_index = 0

        await interaction.response.edit_message(
            embed=build_flight_embed(self.draft),
            view=FlightView(self.draft),
        )


class OpInfoView(RestrictedView):
    def __init__(self, draft: OpTemplateDraft):
        super().__init__(owner_id=draft.created_by)

        self.add_item(OpInfoButton(draft))
        self.add_item(OpTypeSelect(draft))
        self.add_item(MaxPlayersSelect(draft))
        self.add_item(FlightCountSelect(draft))
        self.add_item(CancelButton(draft, danger=False))
        self.add_item(OpInfoNextButton(draft))


class FlightInfoModal(discord.ui.Modal, title="Flight Info"):
    def __init__(self, draft: OpTemplateDraft):
        super().__init__()
        self.draft = draft
        self.flight = draft.flights[draft.current_flight_index]

        self.flight_name_input = discord.ui.TextInput(
            label="Flight Name",
            placeholder="Example: Alpha, Hammer, Stone",
            default=self.flight.flight_name or "",
            max_length=50,
            required=True,
        )

        self.flight_description_input = discord.ui.TextInput(
            label="Flight Description",
            placeholder="Optional: SEAD flight, CAS for ground forces, CAP north of target, etc.",
            default=self.flight.description or "",
            style=discord.TextStyle.paragraph,
            max_length=1000,
            required=False,
        )

        self.add_item(self.flight_name_input)
        self.add_item(self.flight_description_input)

    async def on_submit(self, interaction: discord.Interaction):
        flight_name = normalize_flight_name(str(self.flight_name_input.value))
        flight_letter = auto_flight_letter_from_name(flight_name)
        flight_description = str(self.flight_description_input.value).strip()

        if not flight_name:
            await interaction.response.send_message(
                "Flight name is required.",
                ephemeral=True,
            )
            return

        if not re.fullmatch(r"[A-Z]", flight_letter):
            await interaction.response.send_message(
                "Flight name must start with, or contain, at least one A-Z letter.",
                ephemeral=True,
            )
            return

        for index, existing_flight in enumerate(self.draft.flights):
            if index == self.draft.current_flight_index:
                continue

            if existing_flight.flight_letter == flight_letter:
                await interaction.response.send_message(
                    (
                        f"`{flight_name}` would auto-assign flight letter "
                        f"`{flight_letter}`, but that letter is already being used by "
                        f"`{existing_flight.flight_name}`. Choose a flight name with a "
                        "different first letter."
                    ),
                    ephemeral=True,
                )
                return

        self.flight.flight_name = flight_name
        self.flight.flight_letter = flight_letter
        self.flight.description = flight_description or None

        await interaction.response.edit_message(
            embed=build_flight_embed(self.draft),
            view=FlightView(self.draft),
        )


class FlightInfoButton(discord.ui.Button):
    def __init__(self, draft: OpTemplateDraft):
        super().__init__(
            label="Set Flight Info / Auto Letter",
            style=discord.ButtonStyle.primary,
            row=0,
        )
        self.draft = draft

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(FlightInfoModal(self.draft))


class AirframeSelect(discord.ui.Select):
    def __init__(self, draft: OpTemplateDraft):
        self.draft = draft
        self.flight = draft.flights[draft.current_flight_index]

        airframes = get_active_airframes()
        self.airframes_by_id = {
            str(row["id"]): row
            for row in airframes
        }

        options = [
            discord.SelectOption(
                label=row["display_name"],
                value=str(row["id"]),
                default=self.flight.airframe_id == row["id"],
            )
            for row in airframes[:25]
        ]

        if not options:
            options = [
                discord.SelectOption(
                    label="No airframes found",
                    value="none",
                    default=True,
                )
            ]

        super().__init__(
            placeholder="Select airframe",
            min_values=1,
            max_values=1,
            options=options,
            row=1,
            disabled=not self.airframes_by_id,
        )

    async def callback(self, interaction: discord.Interaction):
        selected_id = self.values[0]

        if selected_id == "none":
            await interaction.response.send_message(
                "No airframes are available. Add airframes first with /airframeadd.",
                ephemeral=True,
            )
            return

        row = self.airframes_by_id[selected_id]

        self.flight.airframe_id = int(row["id"])
        self.flight.airframe_name = row["display_name"]
        adjust_player_slots_to_aircraft_and_remaining_limits(self.draft, self.flight)

        await interaction.response.edit_message(
            embed=build_flight_embed(self.draft),
            view=FlightView(self.draft),
        )


class AircraftCountSelect(discord.ui.Select):
    def __init__(self, draft: OpTemplateDraft):
        self.draft = draft
        self.flight = draft.flights[draft.current_flight_index]

        options = [
            discord.SelectOption(
                label=f"Airframe: {i}",
                value=str(i),
                default=self.flight.aircraft_count == i,
            )
            for i in range(1, 26)
        ]

        super().__init__(
            placeholder="Select aircraft count",
            min_values=1,
            max_values=1,
            options=options,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction):
        new_aircraft_count = int(self.values[0])

        self.flight.aircraft_count = new_aircraft_count
        adjust_player_slots_to_aircraft_and_remaining_limits(self.draft, self.flight)

        await interaction.response.edit_message(
            embed=build_flight_embed(self.draft),
            view=FlightView(self.draft),
        )


class PlayerSlotsSelect(discord.ui.Select):
    def __init__(self, draft: OpTemplateDraft):
        self.draft = draft
        self.flight = draft.flights[draft.current_flight_index]

        valid_values = capped_player_slot_values_for_flight(self.draft, self.flight)

        if valid_values:
            options = [
                discord.SelectOption(
                    label=f"Player Slots: {i}",
                    value=str(i),
                    default=self.flight.player_slots == i,
                )
                for i in valid_values[:25]
            ]
            disabled = len(valid_values) == 1
            placeholder = (
                "Player slots locked"
                if disabled
                else "Select total player slots for this flight"
            )
        else:
            label = "Select airframe and aircraft count first"

            options = [
                discord.SelectOption(
                    label=label[:100],
                    value="none",
                    default=True,
                )
            ]
            disabled = True
            placeholder = "Select total player slots for this flight"

        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
            row=3,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction):
        selected = self.values[0]

        if selected == "none":
            await interaction.response.send_message(
                "Select the airframe and aircraft count first.",
                ephemeral=True,
            )
            return

        new_player_slots = int(selected)
        valid_values = capped_player_slot_values_for_flight(self.draft, self.flight)

        if new_player_slots not in valid_values:
            await interaction.response.send_message(
                (
                    "Player slots are outside the allowed range for this airframe. "
                    f"Allowed: `{valid_values[0]}-{valid_values[-1]}`."
                ),
                ephemeral=True,
            )
            return

        self.flight.player_slots = new_player_slots

        await interaction.response.edit_message(
            embed=build_flight_embed(self.draft),
            view=FlightView(self.draft),
        )


class FlightPreviousButton(discord.ui.Button):
    def __init__(self, draft: OpTemplateDraft):
        super().__init__(
            label="Op Info",
            style=discord.ButtonStyle.secondary,
            row=4,
        )

        self.draft = draft

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=build_op_info_embed(self.draft),
            view=OpInfoView(self.draft),
        )

class PrevFlightButton(discord.ui.Button):
    def __init__(self, draft: OpTemplateDraft):
        super().__init__(
            label="Prev Flight",
            style=discord.ButtonStyle.primary,
            row=4,
            disabled=draft.current_flight_index <= 0,
        )

        self.draft = draft

    async def callback(self, interaction: discord.Interaction):
        if self.draft.current_flight_index <= 0:
            await interaction.response.send_message(
                "You are already on the first flight.",
                ephemeral=True,
            )
            return

        self.draft.current_flight_index -= 1

        await interaction.response.edit_message(
            embed=build_flight_embed(self.draft),
            view=FlightView(self.draft),
        )


class FlightNextButton(discord.ui.Button):
    def __init__(self, draft: OpTemplateDraft):
        is_last = draft.current_flight_index >= len(draft.flights) - 1

        super().__init__(
            label="Review OP" if is_last else "Next Flight",
            style=discord.ButtonStyle.success,
            row=4,
        )

        self.draft = draft
        self.is_last = is_last

    async def callback(self, interaction: discord.Interaction):
        flight_errors = validate_flight_setup(
            self.draft,
            self.draft.current_flight_index,
        )

        if flight_errors:
            await interaction.response.send_message(
                "Please fix this flight before moving on:\n"
                + "\n".join(f"- {error}" for error in flight_errors),
                ephemeral=True,
            )
            return

        if self.is_last:
            try:
                await interaction.response.defer()

                review_embed = build_review_embed(self.draft)
                review_view = ReviewView(self.draft)

                await interaction.edit_original_response(
                    embed=review_embed,
                    view=review_view,
                )
            except Exception as error:
                traceback.print_exc()

                message = f"Failed to show review page: `{error}`"

                try:
                    if interaction.response.is_done():
                        await interaction.followup.send(message, ephemeral=True)
                    else:
                        await interaction.response.send_message(message, ephemeral=True)
                except Exception:
                    pass

            return

        self.draft.current_flight_index += 1

        await interaction.response.edit_message(
            embed=build_flight_embed(self.draft),
            view=FlightView(self.draft),
        )


class FlightView(RestrictedView):
    def __init__(self, draft: OpTemplateDraft):
        super().__init__(owner_id=draft.created_by)

        self.add_item(FlightInfoButton(draft))
        self.add_item(AirframeSelect(draft))
        self.add_item(AircraftCountSelect(draft))
        self.add_item(PlayerSlotsSelect(draft))

        self.add_item(FlightPreviousButton(draft))
        self.add_item(CancelButton(draft, danger=True))
        self.add_item(PrevFlightButton(draft))
        self.add_item(FlightNextButton(draft))


class CreateReviewBackButton(discord.ui.Button):
    def __init__(self, draft: OpTemplateDraft, row: int = 4):
        super().__init__(
            label="Back to Flight",
            style=discord.ButtonStyle.secondary,
            row=row,
        )
        self.draft = draft

    async def callback(self, interaction: discord.Interaction):
        self.draft.current_flight_index = max(0, len(self.draft.flights) - 1)

        await interaction.response.edit_message(
            embed=build_flight_embed(self.draft),
            view=FlightView(self.draft),
        )


class ConfirmTemplateButton(discord.ui.Button):
    def __init__(self, draft: OpTemplateDraft, row: int = 4):
        errors = draft.validate_for_confirm()

        super().__init__(
            label="Create Template",
            style=discord.ButtonStyle.success,
            disabled=bool(errors),
            row=row,
        )

        self.draft = draft

    async def callback(self, interaction: discord.Interaction):
        errors = self.draft.validate_for_confirm()

        if errors:
            await interaction.response.send_message(
                "Cannot save yet:\n" + "\n".join(f"- {error}" for error in errors),
                ephemeral=True,
            )
            return

        try:
            op_template_id = create_op_template_in_db(self.draft)
        except Exception as error:
            await interaction.response.send_message(
                f"Failed to save template: `{error}`",
                ephemeral=True,
            )
            return

        active_op_wizards.pop(self.draft.created_by, None)

        embed = discord.Embed(
            title="Operation Template Created",
            description=(
                f"Template saved successfully.\n\n"
                f"**Name:** {self.draft.name}\n"
                f"**Template ID:** `{op_template_id}`"
            ),
        )

        await interaction.response.edit_message(
            embed=embed,
            view=None,
        )


class ReviewView(RestrictedView):
    def __init__(self, draft: OpTemplateDraft):
        super().__init__(owner_id=draft.created_by)

        self.add_item(CreateReviewBackButton(draft, row=0))
        self.add_item(CancelButton(draft, danger=True, row=0))
        self.add_item(ConfirmTemplateButton(draft, row=0))


ANSI_RESET = "\u001b[0m"
ANSI_GREEN = "\u001b[32m"
ANSI_RED = "\u001b[31m"
ANSI_BLUE = "\u001b[34m"

WINDOW_SIZE = 11


def configured_mission_executer_role_ids() -> set[int]:
    role_ids: set[int] = set()

    for value in (STAFF_ROLE, MISSION_EXECUTER_ROLE):
        try:
            role_id = int(value or 0)
        except (TypeError, ValueError):
            role_id = 0

        if role_id:
            role_ids.add(role_id)

    for value in MISSION_EXECUTER_ROLES or []:
        try:
            role_id = int(value or 0)
        except (TypeError, ValueError):
            role_id = 0

        if role_id:
            role_ids.add(role_id)

    return role_ids


async def has_template_permission(interaction: discord.Interaction) -> bool:
    role_ids = configured_mission_executer_role_ids()

    if not role_ids:
        return True

    if not isinstance(interaction.user, discord.Member):
        return False

    if member_is_admin(interaction.user):
        return True

    return any(role.id in role_ids for role in interaction.user.roles)


@dataclass
class TemplateListState:
    owner_id: int
    selected_index: int = 0


def edit_draft_signature(draft: OpTemplateEditDraft) -> str:
    data = {
        "name": draft.name,
        "description": draft.description,
        "op_type": draft.op_type,
        "total_players": draft.total_players,
        "flight_count": draft.flight_count,
        "briefing": draft.briefing,
        "creator": draft.creator,
        "flights": [
            {
                "id": flight.id,
                "flight_index": flight.flight_index,
                "flight_letter": flight.flight_letter,
                "flight_name": flight.flight_name,
                "aircraft": flight.aircraft,
                "aircraft_count": flight.aircraft_count,
                "slot_count": flight.slot_count,
                "description": flight.description,
            }
            for flight in draft.flights
        ],
    }

    return json.dumps(data, sort_keys=True, default=str)


@dataclass
class TemplateEditState:
    owner_id: int
    list_selected_index: int
    draft: OpTemplateEditDraft
    original_signature: str | None = None

    def __post_init__(self):
        if self.original_signature is None:
            self.original_signature = edit_draft_signature(self.draft)

    def has_changes(self) -> bool:
        return edit_draft_signature(self.draft) != self.original_signature


def selected_window(total: int, selected_index: int) -> tuple[int, int]:
    if total <= WINDOW_SIZE:
        return 0, total

    half = WINDOW_SIZE // 2
    start = max(0, selected_index - half)
    end = min(total, start + WINDOW_SIZE)
    start = max(0, end - WINDOW_SIZE)

    return start, end


def color_line(text: str, color: str | None = None) -> str:
    if not color:
        return text

    return f"{color}{text}{ANSI_RESET}"


def table_line_for_template(row, *, selected: bool, color: str | None) -> str:
    marker = ">" if selected else " "

    return color_line(
        (
            f"{marker}#{row.id:<3} "
            f"{row.name:<20.20} "
            f"{row.total_players:<7} "
            f"{row.flight_count:<8} "
            f"{row.runtime_count}"
        ),
        color,
    )


def event_ids_text(event_ids: list[int]) -> str:
    if not event_ids:
        return "None"

    shown = ", ".join(f"#{event_id}" for event_id in event_ids[:25])

    if len(event_ids) > 25:
        shown += f", +{len(event_ids) - 25} more"

    return shown


def build_templates_embed(state: TemplateListState) -> discord.Embed:
    rows = list_op_templates()
    total = len(rows)

    if total:
        state.selected_index = max(0, min(state.selected_index, total - 1))
    else:
        state.selected_index = 0

    start, end = selected_window(total, state.selected_index)
    lines: list[str] = []

    if not rows:
        lines.append("No op templates found.")
    else:
        lines.append("ID   Name                 Players  Flights   Runs")

        for idx in range(start, end):
            row = rows[idx]
            color = ANSI_BLUE if row.runtime_count else None
            lines.append(
                table_line_for_template(
                    row,
                    selected=idx == state.selected_index,
                    color=color,
                )
            )

    embed = discord.Embed(
        title="Op Templates",
        description="```ansi\n" + "\n".join(lines)[:3900] + "\n```",
    )

    if rows:
        row = rows[state.selected_index]
        creator = row.creator_display_name or row.creator or "Not set"

        embed.add_field(
            name="Op Details",
            value=(
                f"**ID:** `{row.id}`  **Name:** `{row.name}`\n"
                f"**Type:** `{row.op_type}`  **Players:** `{row.total_players}`  "
                f"**Flights:** `{row.flight_count}`\n"
                f"**Creator:** `{creator}`\n"
                f"**Runtimes:** `{row.runtime_count}`\n"
                f"**Event IDs:** {event_ids_text(row.completed_event_ids)}"
            )[:1000],
            inline=False,
        )

    embed.set_footer(text="Blue templates have already been scheduled/run at least once.")

    return embed


def template_rows_count() -> int:
    return len(list_op_templates())


class TemplateListView(discord.ui.View):
    def __init__(self, state: TemplateListState):
        super().__init__(timeout=600)
        self.state = state
        count = template_rows_count()

        self.add_item(TemplatePrevButton(disabled=state.selected_index <= 0, row=0))
        self.add_item(TemplateCancelButton(row=0))
        self.add_item(TemplateCreateButton(row=0))
        self.add_item(TemplateEditButton(disabled=count <= 0, row=0))
        self.add_item(TemplateNextButton(disabled=count <= 0 or state.selected_index >= count - 1, row=0))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.state.owner_id:
            await interaction.response.send_message(
                "This op template view belongs to someone else.",
                ephemeral=True,
            )
            return False

        return True

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=build_templates_embed(self.state),
            view=TemplateListView(self.state),
        )


class TemplatePrevButton(discord.ui.Button):
    def __init__(self, disabled: bool, row: int):
        super().__init__(label="Prev", style=discord.ButtonStyle.secondary, disabled=disabled, row=row)

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, TemplateListView)
        self.view.state.selected_index = max(0, self.view.state.selected_index - 1)
        await self.view.refresh(interaction)


class TemplateNextButton(discord.ui.Button):
    def __init__(self, disabled: bool, row: int):
        super().__init__(label="Next", style=discord.ButtonStyle.secondary, disabled=disabled, row=row)

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, TemplateListView)
        count = template_rows_count()
        self.view.state.selected_index = min(max(0, count - 1), self.view.state.selected_index + 1)
        await self.view.refresh(interaction)


class TemplateCancelButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(label="Exit", style=discord.ButtonStyle.secondary, row=row)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content="Message dismissed.",
            embed=None,
            view=None,
        )


class TemplateCreateButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(label="Create", style=discord.ButtonStyle.success, row=row)

    async def callback(self, interaction: discord.Interaction):
        draft = OpTemplateDraft(created_by=interaction.user.id)
        draft.creator = str(interaction.user.id)
        active_op_wizards[interaction.user.id] = draft

        await interaction.response.edit_message(
            embed=build_op_info_embed(draft),
            view=OpInfoView(draft),
        )


class TemplateEditButton(discord.ui.Button):
    def __init__(self, disabled: bool, row: int):
        super().__init__(label="Edit", style=discord.ButtonStyle.primary, disabled=disabled, row=row)

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, TemplateListView)

        rows = list_op_templates()

        if not rows:
            await interaction.response.send_message("No templates exist to edit.", ephemeral=True)
            return

        row = rows[self.view.state.selected_index]
        draft = load_edit_draft(row.id)

        if draft is None:
            await interaction.response.send_message("That template no longer exists.", ephemeral=True)
            return

        state = TemplateEditState(
            owner_id=self.view.state.owner_id,
            list_selected_index=self.view.state.selected_index,
            draft=draft,
        )

        await interaction.response.edit_message(
            embed=build_edit_op_info_embed(state),
            view=TemplateEditOpInfoView(state),
        )


def ansi_status(label: str, value, *, locked: bool = False, valid: bool = True) -> str:
    shown = value if value not in (None, "") else "Not set"
    prefix = "🔒 " if locked else ""
    color = ANSI_GREEN if valid and value not in (None, "") else ANSI_RED

    return f"{color}{prefix}{label}: {shown}{ANSI_RESET}"


def ansi_optional_status(label: str, value, *, locked: bool = False) -> str:
    shown = value if value not in (None, "") else "Optional"
    prefix = "🔒 " if locked else ""

    return f"{ANSI_YELLOW}{prefix}{label}: {shown}{ANSI_RESET}"


def ansi_block(lines: list[str]) -> str:
    return "```ansi\n" + "\n".join(lines) + "\n```"


def selected_flight(state: TemplateEditState) -> FlightTemplateDraft | None:
    draft = state.draft

    if not draft.flights:
        return None

    draft.selected_flight_index = max(0, min(draft.selected_flight_index, len(draft.flights) - 1))

    return draft.flights[draft.selected_flight_index]


def build_edit_op_info_embed(state: TemplateEditState) -> discord.Embed:
    draft = state.draft
    locked = not draft.full_edit_allowed

    lines = [
        ansi_status("Name", draft.name, locked=locked),
        ansi_status("Type", draft.op_type, locked=locked),
        ansi_status("Max Players", draft.total_players, locked=locked),
        ansi_status("Flight Count", len(draft.flights), locked=locked),
        ansi_optional_status("Briefing", draft.briefing),
        ansi_status("Creator", creator_display_name(draft.creator) or draft.creator or "Not set"),
    ]

    embed = discord.Embed(
        title=f"Edit Operation Template #{draft.id}",
        description=(
            f"{ansi_block(lines)}\n"
            f"**Description:**\n{draft.description or 'Not set'}"
        ),
    )

    if locked:
        embed.add_field(
            name="Locked Template",
            value=(
                "This template has already been scheduled/run. Locked fields are shown with 🔒.\n"
                "You can still edit description, briefing link, creator id, and flight descriptions."
            ),
            inline=False,
        )

    return embed


def build_edit_flight_embed(state: TemplateEditState) -> discord.Embed:
    draft = state.draft
    flight = selected_flight(state)
    locked = not draft.full_edit_allowed

    if flight is None:
        return discord.Embed(title="Edit Flight", description="No flight selected.")

    valid_slots = player_slot_values_for_flight(flight)
    slot_range = f"{valid_slots[0]}-{valid_slots[-1]}" if valid_slots else "Select airframe and aircraft count first"

    lines = [
        ansi_status("Flight Name", flight.flight_name, locked=locked),
        ansi_status("Letter", flight.flight_letter, locked=locked),
        ansi_status("Airframe", flight.aircraft, locked=locked),
        ansi_status("Aircraft Count", flight.aircraft_count, locked=locked),
        ansi_status("Player Slots", flight.slot_count, locked=locked),
    ]

    embed = discord.Embed(
        title=f"Edit Flight {flight.flight_index} of {len(draft.flights)}",
        description=(
            f"{ansi_block(lines)}\n"
            f"**Description:**\n{flight.description or 'Not set'}\n\n"
            f"**Player Slot Range:**\n{slot_range}"
        ),
    )

    errors = validate_edit_draft(draft) if draft.full_edit_allowed else []
    flight_errors = [
        error
        for error in errors
        if error.startswith(f"Flight {flight.flight_index}:")
    ]

    if flight_errors:
        embed.add_field(
            name="Current Flight Checks",
            value="\n".join(f"- {error}" for error in flight_errors)[:1000],
            inline=False,
        )

    return embed


def build_edit_review_embed(state: TemplateEditState) -> discord.Embed:
    draft = state.draft
    errors = validate_edit_draft(draft) if draft.full_edit_allowed else []

    header_lines = [
        f"ID: {draft.id}",
        f"Name: {draft.name or 'Not set'}",
        f"Type: {draft.op_type or 'Not set'}",
        f"Max Players: {draft.total_players or 'Not set'}",
        f"Selectable Flight Slots: {sum(f.slot_count or 0 for f in draft.flights)}",
        color_line(f"Briefing: {draft.briefing or 'Optional'}", ANSI_YELLOW),
        f"Creator: {creator_display_name(draft.creator) or draft.creator or 'Not set'}",
    ]

    flight_lines: list[str] = []

    for flight in draft.flights:
        flight_lines.append(color_line(f"Flight {flight.flight_letter or '?'} | {flight.flight_name or 'Not set'}", ANSI_GREEN))
        flight_lines.append(color_line(f"{flight.aircraft_count or 'Not set'}x {flight.aircraft or 'Not set'}", ANSI_GREEN))
        flight_lines.append(color_line(f"{flight.slot_count or 'Not set'} Slots", ANSI_GREEN))
        if flight.description:
            flight_lines.append(color_line(f"Description: {flight.description}", ANSI_GREEN))
        flight_lines.append("")

    if not flight_lines:
        flight_lines = [color_line("No flights set.", ANSI_RED)]

    embed = discord.Embed(
        title=f"Review Edit Template #{draft.id}",
        description=(
            "\n".join(header_lines)
            + "\n\n"
            + "**Description**\n"
            + f"{draft.description or 'Not set'}\n\n"
            + ansi_block(flight_lines).replace("\n\n```", "\n```")
        )[:4000],
    )

    if errors:
        embed.add_field(
            name="Cannot Save Yet",
            value="\n".join(f"- {error}" for error in errors)[:1000],
            inline=False,
        )

    return embed


class BaseTemplateEditView(discord.ui.View):
    def __init__(self, state: TemplateEditState):
        super().__init__(timeout=600)
        self.state = state

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.state.owner_id:
            await interaction.response.send_message("This edit view belongs to someone else.", ephemeral=True)
            return False

        return True


class TemplateEditOpInfoView(BaseTemplateEditView):
    def __init__(self, state: TemplateEditState):
        super().__init__(state)
        self.add_item(OpInfoEditButton(row=0))
        self.add_item(OpTypeEditSelect(state, row=1))
        self.add_item(MaxPlayersEditSelect(state, row=2))
        self.add_item(FlightCountEditSelect(state, row=3))
        self.add_item(EditCancelButton(state, row=4))
        self.add_item(EditOpInfoNextButton(row=4))


class TemplateEditFlightView(BaseTemplateEditView):
    def __init__(self, state: TemplateEditState):
        super().__init__(state)
        self.add_item(FlightInfoEditButton(row=0))
        self.add_item(AirframeEditSelect(state, row=1))
        self.add_item(AircraftCountEditSelect(state, row=2))
        self.add_item(PlayerSlotsEditSelect(state, row=3))
        self.add_item(EditFlightBackButton(row=4))
        self.add_item(EditCancelButton(state, row=4))
        self.add_item(EditFlightNextButton(state, row=4))


class TemplateEditReviewView(BaseTemplateEditView):
    def __init__(self, state: TemplateEditState):
        super().__init__(state)
        self.add_item(EditReviewBackButton(row=4))
        self.add_item(EditCancelButton(state, row=4))
        self.add_item(EditSaveButton(state, row=4))


class EditCancelButton(discord.ui.Button):
    def __init__(self, state: TemplateEditState, row: int):
        has_changes = state.has_changes()

        super().__init__(
            label="Cancel" if has_changes else "Exit",
            style=discord.ButtonStyle.danger if has_changes else discord.ButtonStyle.secondary,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, BaseTemplateEditView)
        state = TemplateListState(owner_id=self.view.state.owner_id, selected_index=self.view.state.list_selected_index)

        await interaction.response.edit_message(
            embed=build_templates_embed(state),
            view=TemplateListView(state),
        )


class EditOpInfoNextButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(label="Next", style=discord.ButtonStyle.success, row=row)

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, TemplateEditOpInfoView)

        try:
            if not self.view.state.draft.flights:
                await interaction.response.edit_message(
                    embed=build_edit_review_embed(self.view.state),
                    view=TemplateEditReviewView(self.view.state),
                )
                return

            self.view.state.draft.selected_flight_index = 0

            await interaction.response.edit_message(
                embed=build_edit_flight_embed(self.view.state),
                view=TemplateEditFlightView(self.view.state),
            )
        except Exception as error:
            traceback.print_exc()

            if interaction.response.is_done():
                await interaction.followup.send(
                    f"Could not open the flight edit page: `{type(error).__name__}: {error}`",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    f"Could not open the flight edit page: `{type(error).__name__}: {error}`",
                    ephemeral=True,
                )


class EditFlightBackButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(label="Back to Op Info", style=discord.ButtonStyle.secondary, row=row)

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, TemplateEditFlightView)

        await interaction.response.edit_message(
            embed=build_edit_op_info_embed(self.view.state),
            view=TemplateEditOpInfoView(self.view.state),
        )


class EditFlightNextButton(discord.ui.Button):
    def __init__(self, state: TemplateEditState, row: int):
        is_last = state.draft.selected_flight_index >= len(state.draft.flights) - 1
        super().__init__(
            label="Review OP" if is_last else "Next Flight",
            style=discord.ButtonStyle.success,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, TemplateEditFlightView)
        draft = self.view.state.draft

        if draft.selected_flight_index >= len(draft.flights) - 1:
            await interaction.response.edit_message(
                embed=build_edit_review_embed(self.view.state),
                view=TemplateEditReviewView(self.view.state),
            )
            return

        draft.selected_flight_index += 1

        await interaction.response.edit_message(
            embed=build_edit_flight_embed(self.view.state),
            view=TemplateEditFlightView(self.view.state),
        )


class EditReviewBackButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(label="Back to Flight", style=discord.ButtonStyle.secondary, row=row)

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, TemplateEditReviewView)

        if self.view.state.draft.flights:
            self.view.state.draft.selected_flight_index = max(0, len(self.view.state.draft.flights) - 1)

        await interaction.response.edit_message(
            embed=build_edit_flight_embed(self.view.state),
            view=TemplateEditFlightView(self.view.state),
        )


class EditSaveButton(discord.ui.Button):
    def __init__(self, state: TemplateEditState, row: int):
        errors = validate_edit_draft(state.draft) if state.draft.full_edit_allowed else []
        super().__init__(
            label="Save",
            style=discord.ButtonStyle.success,
            disabled=bool(errors),
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, TemplateEditReviewView)

        try:
            save_edit_draft(self.view.state.draft)
        except Exception as error:
            await interaction.response.send_message(f"Could not save template: `{error}`", ephemeral=True)
            return

        state = TemplateListState(owner_id=self.view.state.owner_id, selected_index=self.view.state.list_selected_index)

        await interaction.response.edit_message(
            embed=build_templates_embed(state),
            view=TemplateListView(state),
        )


class OpInfoEditModal(discord.ui.Modal):
    def __init__(self, state: TemplateEditState):
        super().__init__(title="Edit Op Info")
        self.state = state
        draft = state.draft

        self.name_input = discord.ui.TextInput(
            label="Operation Name",
            default=draft.name or "",
            max_length=100,
            required=draft.full_edit_allowed,
        )
        self.description_input = discord.ui.TextInput(
            label="Operation Description",
            default=draft.description or "",
            style=discord.TextStyle.paragraph,
            max_length=1000,
            required=False,
        )
        self.briefing_input = discord.ui.TextInput(
            label="Briefing Link",
            default=draft.briefing or "",
            max_length=500,
            required=False,
        )
        self.creator_input = discord.ui.TextInput(
            label="Creator Discord ID",
            default=draft.creator or "",
            max_length=30,
            required=False,
        )

        if draft.full_edit_allowed:
            self.add_item(self.name_input)

        self.add_item(self.description_input)
        self.add_item(self.briefing_input)
        self.add_item(self.creator_input)

    async def on_submit(self, interaction: discord.Interaction):
        draft = self.state.draft

        if draft.full_edit_allowed:
            draft.name = normalize_op_name(str(self.name_input.value))

        draft.description = clean_text(str(self.description_input.value))
        draft.briefing = clean_text(str(self.briefing_input.value))
        draft.creator = clean_text(str(self.creator_input.value)) or str(interaction.user.id)

        await interaction.response.edit_message(
            embed=build_edit_op_info_embed(self.state),
            view=TemplateEditOpInfoView(self.state),
        )


class OpInfoEditButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(label="Op Info", style=discord.ButtonStyle.primary, row=row)

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, BaseTemplateEditView)
        await interaction.response.send_modal(OpInfoEditModal(self.view.state))


class OpTypeEditSelect(discord.ui.Select):
    def __init__(self, state: TemplateEditState, row: int):
        self.state = state
        locked = not state.draft.full_edit_allowed
        options = [
            discord.SelectOption(
                label=str(op_type),
                value=str(op_type),
                default=state.draft.op_type == str(op_type),
            )
            for op_type in OP_TYPES
        ]

        super().__init__(
            placeholder="Op Type",
            min_values=1,
            max_values=1,
            options=options,
            disabled=locked,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        self.state.draft.op_type = self.values[0]
        assert isinstance(self.view, BaseTemplateEditView)
        await interaction.response.edit_message(
            embed=build_edit_op_info_embed(self.state),
            view=TemplateEditOpInfoView(self.state),
        )


class MaxPlayersEditSelect(discord.ui.Select):
    def __init__(self, state: TemplateEditState, row: int):
        self.state = state
        locked = not state.draft.full_edit_allowed
        options = [
            discord.SelectOption(
                label=f"Players: {count}",
                value=str(count),
                default=state.draft.total_players == count,
            )
            for count in range(1, 26)
        ]

        super().__init__(
            placeholder="Max Players",
            min_values=1,
            max_values=1,
            options=options,
            disabled=locked,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        self.state.draft.total_players = int(self.values[0])
        assert isinstance(self.view, BaseTemplateEditView)
        await interaction.response.edit_message(
            embed=build_edit_op_info_embed(self.state),
            view=TemplateEditOpInfoView(self.state),
        )


class FlightCountEditSelect(discord.ui.Select):
    def __init__(self, state: TemplateEditState, row: int):
        self.state = state
        locked = not state.draft.full_edit_allowed
        options = [
            discord.SelectOption(
                label=f"Flights: {count}",
                value=str(count),
                default=len(state.draft.flights) == count,
            )
            for count in range(1, 26)
        ]

        super().__init__(
            placeholder="Flight Count",
            min_values=1,
            max_values=1,
            options=options,
            disabled=locked,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        resize_flights(self.state.draft, int(self.values[0]))
        assert isinstance(self.view, BaseTemplateEditView)
        await interaction.response.edit_message(
            embed=build_edit_op_info_embed(self.state),
            view=TemplateEditOpInfoView(self.state),
        )


class FlightInfoEditModal(discord.ui.Modal):
    def __init__(self, state: TemplateEditState):
        super().__init__(title="Edit Flight")
        self.state = state
        self.flight = selected_flight(state)

        if self.flight is None:
            raise ValueError("No flight selected.")

        self.flight_name_input = discord.ui.TextInput(
            label="Flight Name",
            default=self.flight.flight_name or "",
            max_length=50,
            required=state.draft.full_edit_allowed,
        )
        self.description_input = discord.ui.TextInput(
            label="Flight Description",
            default=self.flight.description or "",
            style=discord.TextStyle.paragraph,
            max_length=1000,
            required=False,
        )

        if state.draft.full_edit_allowed:
            self.add_item(self.flight_name_input)

        self.add_item(self.description_input)

    async def on_submit(self, interaction: discord.Interaction):
        if self.state.draft.full_edit_allowed:
            name = normalize_flight_name(str(self.flight_name_input.value))
            letter = auto_flight_letter_from_name(name)

            if not name or not letter:
                await interaction.response.send_message("Flight name must contain at least one A-Z letter.", ephemeral=True)
                return

            for index, flight in enumerate(self.state.draft.flights):
                if index == self.state.draft.selected_flight_index:
                    continue

                if flight.flight_letter == letter:
                    await interaction.response.send_message(
                        f"Flight letter `{letter}` is already used by `{flight.flight_name}`.",
                        ephemeral=True,
                    )
                    return

            self.flight.flight_name = name
            self.flight.flight_letter = letter

        self.flight.description = clean_text(str(self.description_input.value))

        await interaction.response.edit_message(
            embed=build_edit_flight_embed(self.state),
            view=TemplateEditFlightView(self.state),
        )


class FlightInfoEditButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(label="Flight Info", style=discord.ButtonStyle.primary, row=row)

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, BaseTemplateEditView)

        try:
            modal = FlightInfoEditModal(self.view.state)
        except Exception as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return

        await interaction.response.send_modal(modal)


def airframe_option_display_name(item: dict) -> str:
    """Accept both old edit rows and merged /opcreate rows."""
    return str(
        item.get("name")
        or item.get("display_name")
        or item.get("aircraft")
        or ""
    ).strip()


class AirframeEditSelect(discord.ui.Select):
    def __init__(self, state: TemplateEditState, row: int):
        self.state = state
        self.flight = selected_flight(state)
        locked = not state.draft.full_edit_allowed
        raw_airframes = get_active_airframes()

        airframes: list[dict] = []

        for item in raw_airframes:
            if not isinstance(item, dict):
                continue

            name = airframe_option_display_name(item)

            if not name:
                continue

            try:
                max_seats = int(item.get("max_seats") or 1)
            except (TypeError, ValueError):
                max_seats = 1

            airframes.append(
                {
                    "name": name,
                    "max_seats": max(1, max_seats),
                }
            )

        options = [
            discord.SelectOption(
                label=f"{item['name']} ({item['max_seats']} seat max)",
                value=item["name"],
                default=self.flight is not None and self.flight.aircraft == item["name"],
            )
            for item in airframes[:25]
        ]

        if not options:
            options = [discord.SelectOption(label="No airframes configured", value="none", default=True)]

        super().__init__(
            placeholder="Airframe",
            min_values=1,
            max_values=1,
            options=options,
            disabled=locked or not bool(airframes),
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            await interaction.response.defer()
            return

        flight = selected_flight(self.state)
        if flight is not None:
            flight.aircraft = self.values[0]
            flight.slot_count = None

        assert isinstance(self.view, BaseTemplateEditView)
        await interaction.response.edit_message(
            embed=build_edit_flight_embed(self.state),
            view=TemplateEditFlightView(self.state),
        )


class AircraftCountEditSelect(discord.ui.Select):
    def __init__(self, state: TemplateEditState, row: int):
        self.state = state
        flight = selected_flight(state)
        locked = not state.draft.full_edit_allowed

        options = [
            discord.SelectOption(
                label=f"Airframe: {count}",
                value=str(count),
                default=flight is not None and flight.aircraft_count == count,
            )
            for count in range(1, 26)
        ]

        super().__init__(
            placeholder="Aircraft Count",
            min_values=1,
            max_values=1,
            options=options,
            disabled=locked,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        flight = selected_flight(self.state)
        if flight is not None:
            flight.aircraft_count = int(self.values[0])
            flight.slot_count = None

        assert isinstance(self.view, BaseTemplateEditView)
        await interaction.response.edit_message(
            embed=build_edit_flight_embed(self.state),
            view=TemplateEditFlightView(self.state),
        )


class PlayerSlotsEditSelect(discord.ui.Select):
    def __init__(self, state: TemplateEditState, row: int):
        self.state = state
        flight = selected_flight(state)
        locked = not state.draft.full_edit_allowed
        values = player_slot_values_for_flight(flight) if flight else []

        if values:
            options = [
                discord.SelectOption(
                    label=f"Player Slots: {value}",
                    value=str(value),
                    default=flight is not None and flight.slot_count == value,
                )
                for value in values[:25]
            ]

            if len(values) == 1 and flight is not None:
                flight.slot_count = values[0]

            disabled = locked or len(values) == 1
            placeholder = "Player Slots Locked" if disabled else "Player Slots"
        else:
            options = [discord.SelectOption(label="Select aircraft first", value="none", default=True)]
            disabled = True
            placeholder = "Player Slots"

        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
            disabled=disabled,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            return

        flight = selected_flight(self.state)
        if flight is not None:
            flight.slot_count = int(self.values[0])

        assert isinstance(self.view, BaseTemplateEditView)
        await interaction.response.edit_message(
            embed=build_edit_flight_embed(self.state),
            view=TemplateEditFlightView(self.state),
        )


class OpTemplatesCog(commands.Cog):
    op_group = app_commands.Group(
        name="op",
        description="Operation tools.",
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @op_group.command(
        name="templates",
        description="Browse, create, and edit operation templates.",
    )
    async def templates(self, interaction: discord.Interaction):
        if not await require_mission_executer_command(interaction):
            return
        if not await has_template_permission(interaction):
            await interaction.response.send_message("You do not have permission to manage op templates.", ephemeral=True)
            return

        state = TemplateListState(owner_id=interaction.user.id)

        await interaction.response.send_message(
            embed=build_templates_embed(state),
            view=TemplateListView(state),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(OpTemplatesCog(bot))
