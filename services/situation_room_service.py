from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import discord

from database import get_connection
from services.display_name_service import prune_display_name


try:
    from config import SITUATION_ROOM_CHANNEL_ID
except ImportError:
    SITUATION_ROOM_CHANNEL_ID = 0

try:
    from config import SOONEST_ON_TOP
except ImportError:
    SOONEST_ON_TOP = True

try:
    from config import SITUATION_ROOM_STATE_FILE
except ImportError:
    SITUATION_ROOM_STATE_FILE = "situation_room_messages.json"

try:
    from config import SITUATION_ROOM_UPDATE_DELAY_SECONDS
except ImportError:
    SITUATION_ROOM_UPDATE_DELAY_SECONDS = 5.0

try:
    from config import SITUATION_ROOM_REORDER_DELAY_SECONDS
except ImportError:
    SITUATION_ROOM_REORDER_DELAY_SECONDS = 1.0


ANSI_RESET = "\u001b[0m"
ANSI_GREEN = "\u001b[32m"
ANSI_RED = "\u001b[31m"
ANSI_ORANGE = "\u001b[33m"
ANSI_YELLOW = ANSI_ORANGE
ANSI_BLUE = "\u001b[34m"
ANSI_PURPLE = "\u001b[35m"


@dataclass(frozen=True)
class SituationRoomEvent:
    event_id: int
    op_template_id: int
    op_name: str
    scheduled_at: int
    status: str


@dataclass(frozen=True)
class SituationBoard:
    key: str
    board_type: str
    event: SituationRoomEvent


_DEBOUNCE_TASK: asyncio.Task | None = None
_DEBOUNCE_REASONS: set[str] = set()
_REFRESH_LOCK = asyncio.Lock()


def now_ts() -> int:
    return int(time.time())


def clean_text(value: Any) -> str | None:
    if value is None:
        return None

    text = str(value).strip()
    return text or None


def state_file_path() -> Path:
    configured = Path(str(SITUATION_ROOM_STATE_FILE))

    if configured.is_absolute():
        return configured

    return Path.cwd() / configured


def default_state() -> dict[str, Any]:
    return {
        "channel_id": 0,
        "messages": {},
        "content_hashes": {},
        "order": [],
    }


def load_state() -> dict[str, Any]:
    path = state_file_path()

    if not path.exists():
        return default_state()

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default_state()

    if not isinstance(data, dict):
        return default_state()

    state = default_state()
    state["channel_id"] = int(data.get("channel_id") or 0)

    messages = data.get("messages")
    if isinstance(messages, dict):
        state["messages"] = {
            str(key): int(value)
            for key, value in messages.items()
            if str(value).isdigit()
        }

    content_hashes = data.get("content_hashes")
    if isinstance(content_hashes, dict):
        state["content_hashes"] = {
            str(key): str(value)
            for key, value in content_hashes.items()
        }

    order = data.get("order")
    if isinstance(order, list):
        state["order"] = [str(key) for key in order]

    return state


def save_state(state: dict[str, Any]) -> None:
    path = state_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "channel_id": int(state.get("channel_id") or 0),
        "messages": state.get("messages", {}),
        "content_hashes": state.get("content_hashes", {}),
        "order": state.get("order", []),
    }

    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def configured_channel_id() -> int:
    try:
        return int(SITUATION_ROOM_CHANNEL_ID or 0)
    except (TypeError, ValueError):
        return 0


def is_tracked_situation_room_message(
    *,
    channel_id: int,
    message_id: int,
) -> bool:
    """Return True only for a board message tracked in the Situation Room JSON."""
    configured_id = configured_channel_id()

    if not configured_id or int(channel_id) != configured_id:
        return False

    state = load_state()

    saved_channel_id = int(state.get("channel_id") or 0)
    if saved_channel_id and saved_channel_id != configured_id:
        return False

    tracked_ids = {
        int(saved_message_id)
        for saved_message_id in state.get("messages", {}).values()
        if str(saved_message_id).isdigit()
    }

    return int(message_id) in tracked_ids


def configured_debounce_delay() -> float:
    try:
        return max(0.0, float(SITUATION_ROOM_UPDATE_DELAY_SECONDS))
    except (TypeError, ValueError):
        return 5.0


def configured_reorder_delay() -> float:
    try:
        return max(0.5, float(SITUATION_ROOM_REORDER_DELAY_SECONDS))
    except (TypeError, ValueError):
        return 1.0


def fetch_situation_events() -> list[SituationRoomEvent]:
    current = now_ts()
    horizon = current + (7 * 24 * 60 * 60)

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                oe.event_id,
                oe.op_template_id,
                ot.name AS op_name,
                oe.scheduled_at,
                oe.status
            FROM op_events oe
            JOIN op_templates ot
                ON ot.id = oe.op_template_id
            WHERE (
                    oe.status = 'Scheduled'
                AND oe.scheduled_at >= ?
                AND oe.scheduled_at <= ?
            )
               OR oe.status = 'Briefing'
               OR oe.status = 'Open'
            ORDER BY oe.scheduled_at ASC, oe.event_id ASC
            """,
            (current, horizon),
        ).fetchall()

    return [
        SituationRoomEvent(
            event_id=int(row["event_id"]),
            op_template_id=int(row["op_template_id"]),
            op_name=str(row["op_name"]),
            scheduled_at=int(row["scheduled_at"]),
            status=str(row["status"]),
        )
        for row in rows
    ]


def desired_boards() -> list[SituationBoard]:
    boards: list[SituationBoard] = []

    for event in fetch_situation_events():
        if event.status in {"Scheduled", "Briefing"}:
            boards.append(
                SituationBoard(
                    key=f"reservation:{event.event_id}",
                    board_type="reservation",
                    event=event,
                )
            )
        elif event.status == "Open":
            boards.append(
                SituationBoard(
                    key=f"attendance:{event.event_id}",
                    board_type="attendance",
                    event=event,
                )
            )

    boards.sort(key=lambda board: (board.event.scheduled_at, board.event.event_id))

    if not bool(SOONEST_ON_TOP):
        boards.reverse()

    return boards


def user_display_name(conn, discord_id: str | None, fallback: str | None = None) -> str:
    did = clean_text(discord_id)

    if not did:
        return clean_text(fallback) or "Open"

    row = conn.execute(
        """
        SELECT display_name, discord_username
        FROM users
        WHERE discord_id = ?
        LIMIT 1
        """,
        (did,),
    ).fetchone()

    if row is not None:
        name = (
            clean_text(row["display_name"])
            or clean_text(row["discord_username"])
            or clean_text(fallback)
            or did
        )
        return prune_display_name(name, fallback=did)

    return prune_display_name(clean_text(fallback) or did, fallback=did)


def reservation_slot_is_locked(row: dict[str, Any]) -> bool:
    return str(row.get("status") or "").strip().lower() == "locked"


def reservation_slot_is_reserved(row: dict[str, Any]) -> bool:
    status = str(row.get("status") or "").strip().lower()
    return bool(row.get("reserved_by")) or status == "reserved"


def reservation_slot_color(row: dict[str, Any]) -> str:
    """Shared reservation color rules.

    Green  = open
    Yellow = reserved
    Purple = locked and reserved
    Blue   = locked and not reserved
    """
    locked = reservation_slot_is_locked(row)
    reserved = reservation_slot_is_reserved(row)

    if locked and reserved:
        return ANSI_PURPLE

    if locked and not reserved:
        return ANSI_BLUE

    if reserved:
        return ANSI_YELLOW

    return ANSI_GREEN


def reservation_slot_status_label(row: dict[str, Any]) -> str:
    locked = reservation_slot_is_locked(row)
    reserved = reservation_slot_is_reserved(row)

    if reserved:
        name = row.get("reserved_name") or row.get("reserved_by") or "Reserved"
        return str(name)

    return "Locked" if locked else "Open"


def reservation_rows(event_id: int) -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                r.id AS reservation_id,
                r.slot_index,
                r.status AS reservation_status,
                r.reserved_by,
                ft.flight_letter,
                ft.flight_name,
                ft.aircraft,
                ft.slot_count,
                u.display_name AS reserved_display_name,
                u.discord_username AS reserved_username
            FROM op_reservations r
            JOIN op_events oe
                ON oe.event_id = r.op_event_id
            LEFT JOIN flight_templates ft
                ON ft.op_template_id = oe.op_template_id
               AND ft.flight_index = r.slot_index
            LEFT JOIN users u
                ON u.discord_id = r.reserved_by
            WHERE r.op_event_id = ?
            ORDER BY r.slot_index ASC, r.id ASC
            """,
            (int(event_id),),
        ).fetchall()

        result: list[dict[str, Any]] = []

        for row in rows:
            reserved_by = clean_text(row["reserved_by"])
            reserved_name = prune_display_name(
                (
                    clean_text(row["reserved_display_name"])
                    or clean_text(row["reserved_username"])
                    or reserved_by
                ),
                fallback=reserved_by,
            )

            result.append(
                {
                    "slot_index": int(row["slot_index"] or 0),
                    "status": str(row["reservation_status"] or "open"),
                    "reserved_by": reserved_by,
                    "reserved_name": reserved_name,
                    "flight_letter": clean_text(row["flight_letter"]) or "?",
                    "flight_name": clean_text(row["flight_name"]) or "Unnamed",
                    "aircraft": clean_text(row["aircraft"]) or "Unknown",
                    "slot_count": int(row["slot_count"] or 0),
                }
            )

    return result


def attendance_flights_and_rows(event_id: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    with get_connection() as conn:
        event_row = conn.execute(
            """
            SELECT op_template_id
            FROM op_events
            WHERE event_id = ?
            LIMIT 1
            """,
            (int(event_id),),
        ).fetchone()

        if event_row is None:
            return [], []

        flights = conn.execute(
            """
            SELECT
                flight_index,
                flight_letter,
                flight_name,
                aircraft,
                aircraft_count,
                slot_count
            FROM flight_templates
            WHERE op_template_id = ?
            ORDER BY flight_index ASC, id ASC
            """,
            (int(event_row["op_template_id"]),),
        ).fetchall()

        attendance_rows = conn.execute(
            """
            SELECT
                a.entry_id,
                a.discord_id,
                a.user_name,
                a.slot,
                a.aircraft,
                a.combat_deaths,
                a.landing_type,
                a.wires,
                a.status,
                u.display_name,
                u.discord_username
            FROM attendance a
            LEFT JOIN users u
                ON u.discord_id = a.discord_id
            WHERE a.scheduled_op_id = ?
              AND a.status = 'submitted'
              AND a.discord_id IS NOT NULL
            ORDER BY a.updated_at ASC, a.entry_id ASC
            """,
            (int(event_id),),
        ).fetchall()

    return [dict(row) for row in flights], [dict(row) for row in attendance_rows]


def normalized_slot(value: str | None) -> str:
    return (clean_text(value) or "").lower().replace(" ", "")


def flight_slot_suffix(slot: str | None) -> int | None:
    normalized = normalized_slot(slot)

    if "1-" not in normalized:
        return None

    tail = normalized.rsplit("1-", 1)[-1]

    if not tail.isdigit():
        return None

    return int(tail)


def belongs_to_flight(slot: str | None, flight_name: str | None, flight_letter: str | None) -> bool:
    normalized = normalized_slot(slot)
    name = normalized_slot(flight_name)
    letter = normalized_slot(flight_letter)

    if name and normalized.startswith(name):
        return True

    return bool(letter and normalized.startswith(letter))


def attendance_detail_line(label: str, row: dict[str, Any]) -> str:
    raw_name = (
        clean_text(row.get("display_name"))
        or clean_text(row.get("discord_username"))
        or clean_text(row.get("user_name"))
        or clean_text(row.get("discord_id"))
        or "Unknown"
    )
    name = prune_display_name(raw_name, fallback=row.get("discord_id") or "Unknown")
    deaths = row.get("combat_deaths")
    landing = clean_text(row.get("landing_type")) or "Unknown"

    parts = [
        label,
        name,
        f"{int(deaths) if deaths is not None else 0} Deaths",
        landing,
    ]

    if landing == "Arrested" and row.get("wires") is not None:
        wire_count = int(row["wires"])
        parts.append(f"{wire_count} Wire" + ("" if wire_count == 1 else "s"))

    return " - ".join(parts)


def render_reservation_embed(board: SituationBoard) -> discord.Embed:
    title_suffix = (
        "Expected Flight Assignments"
        if board.event.status == "Briefing"
        else "Flight Lead Slots"
    )
    legend = (
        "Green = open | Yellow = reserved | "
        "Purple = locked/reserved | Blue = locked/open"
    )

    lines = [
        "Callsign    Airframe  Flight Size  Reserved",
    ]

    for row in reservation_rows(board.event.event_id):
        reserved = reservation_slot_status_label(row)
        color = reservation_slot_color(row)

        line = (
            f"{row['flight_letter']} | {row['flight_name']} - "
            f"{row['aircraft']} - {row['slot_count']} - {reserved}"
        )
        lines.append(f"{color}{line[:110]}{ANSI_RESET}")

    if len(lines) == 1:
        lines.append(f"{ANSI_GREEN}No flight lead slots found.{ANSI_RESET}")

    return discord.Embed(
        title=f"#{board.event.event_id} {board.event.op_name} - {title_suffix}",
        description=(
            f"Status: **{board.event.status}**\n"
            f"{legend}\n"
            f"<t:{board.event.scheduled_at}:F>\n"
            f"<t:{board.event.scheduled_at}:R>\n"
            f"```ansi\n{chr(10).join(lines)[:3600]}\n```"
        ),
    )


def render_attendance_embed(board: SituationBoard) -> discord.Embed:
    flights, attendance_rows = attendance_flights_and_rows(board.event.event_id)
    lines: list[str] = []

    for flight in flights:
        letter = (clean_text(flight.get("flight_letter")) or "?").upper()[:1] or "?"
        flight_name = clean_text(flight.get("flight_name")) or letter

        try:
            aircraft_count = max(1, int(flight.get("aircraft_count") or 1))
        except (TypeError, ValueError):
            aircraft_count = 1

        try:
            player_slots = max(0, int(flight.get("slot_count") or 0))
        except (TypeError, ValueError):
            player_slots = 0

        flight_rows = [
            row
            for row in attendance_rows
            if belongs_to_flight(
                row.get("slot"),
                flight_name,
                letter,
            )
        ]

        pilot_rows: dict[int, list[dict[str, Any]]] = {}
        non_pilot_rows: list[dict[str, Any]] = []

        for row in flight_rows:
            slot_number = flight_slot_suffix(row.get("slot"))
            landing_type = clean_text(row.get("landing_type"))

            if landing_type == "Non-Pilot":
                non_pilot_rows.append(row)
                continue

            if slot_number is not None:
                pilot_rows.setdefault(slot_number, []).append(row)
            else:
                non_pilot_rows.append(row)

        # Pilot positions are based on aircraft count, not total player slots.
        for aircraft_number in range(1, aircraft_count + 1):
            display_slot = f"{letter}1-{aircraft_number}"
            rows_for_slot = pilot_rows.get(aircraft_number, [])

            if rows_for_slot:
                for row in rows_for_slot:
                    lines.append(
                        f"{ANSI_GREEN}{attendance_detail_line(display_slot, row)[:110]}{ANSI_RESET}"
                    )
            else:
                lines.append(f"{ANSI_RED}{display_slot} - Open{ANSI_RESET}")

        # Extra player slots are the available non-pilot seats. They have no
        # physical aircraft assignment until a non-pilot actually submits.
        extra_non_pilot_slots = max(0, player_slots - aircraft_count)

        for row in non_pilot_rows:
            slot_number = flight_slot_suffix(row.get("slot"))
            display_slot = f"{letter}1-{slot_number}" if slot_number is not None else f"{letter}-NP"
            lines.append(
                f"{ANSI_GREEN}{attendance_detail_line(display_slot, row)[:110]}{ANSI_RESET}"
            )

        remaining_np = max(0, extra_non_pilot_slots - len(non_pilot_rows))

        for _ in range(remaining_np):
            lines.append(f"{ANSI_RED}{letter}-NP - Open{ANSI_RESET}")

        if flight is not flights[-1]:
            lines.append("")

    if not lines:
        lines.append(f"{ANSI_RED}No attendance slots found.{ANSI_RESET}")

    description_lines = [
        f"<t:{board.event.scheduled_at}:F>",
        f"<t:{board.event.scheduled_at}:R>",
        f"```ansi\n{chr(10).join(lines)[:3600]}\n```",
    ]

    return discord.Embed(
        title=f"#{board.event.event_id} {board.event.op_name} - Attendance",
        description="\n".join(description_lines),
    )


def render_board_embed(board: SituationBoard) -> discord.Embed:
    if board.board_type == "attendance":
        return render_attendance_embed(board)

    return render_reservation_embed(board)


def embed_hash(embed: discord.Embed) -> str:
    payload = json.dumps(embed.to_dict(), sort_keys=True, separators=(",", ":"))

    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def fetch_situation_room_channel(bot: discord.Client) -> discord.TextChannel | None:
    channel_id = configured_channel_id()

    if not channel_id:
        return None

    channel = bot.get_channel(channel_id)

    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    if not isinstance(channel, discord.TextChannel):
        return None

    return channel


async def delete_message_if_present(channel: discord.TextChannel, message_id: int) -> None:
    try:
        message = await channel.fetch_message(int(message_id))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return

    try:
        await message.delete()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return


async def send_or_edit_board(
    *,
    channel: discord.TextChannel,
    state: dict[str, Any],
    board: SituationBoard,
) -> None:
    messages: dict[str, int] = state.setdefault("messages", {})
    content_hashes: dict[str, str] = state.setdefault("content_hashes", {})
    embed = render_board_embed(board)
    desired_hash = embed_hash(embed)
    existing_id = messages.get(board.key)

    if existing_id:
        try:
            message = await channel.fetch_message(int(existing_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            message = None

        if message is not None:
            if content_hashes.get(board.key) != desired_hash:
                try:
                    await message.edit(embed=embed)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    message = None

            if message is not None:
                content_hashes[board.key] = desired_hash
                return

    message = await channel.send(embed=embed)
    messages[board.key] = int(message.id)
    content_hashes[board.key] = desired_hash


async def delete_all_state_messages(
    *,
    channel: discord.TextChannel,
    state: dict[str, Any],
) -> None:
    messages = dict(state.get("messages", {}))

    for index, message_id in enumerate(messages.values()):
        await delete_message_if_present(channel, int(message_id))

        if index < len(messages) - 1:
            await asyncio.sleep(configured_reorder_delay())

    state["messages"] = {}
    state["content_hashes"] = {}
    state["order"] = []


async def reconcile_situation_room(
    bot: discord.Client,
    *,
    force_rebuild: bool = False,
) -> None:
    """Reconcile all next-seven-day boards with the persistent JSON message state.

    force_rebuild=True deletes every tracked Situation Room message first, then
    recreates all current boards from the database.
    """
    channel = await fetch_situation_room_channel(bot)

    if channel is None:
        return

    async with _REFRESH_LOCK:
        state = load_state()
        desired = desired_boards()
        desired_keys = [board.key for board in desired]
        current_keys = [
            key
            for key in state.get("order", [])
            if key in state.get("messages", {})
        ]

        # If the configured channel changed, start cleanly in the new channel.
        if int(state.get("channel_id") or 0) != int(channel.id):
            state = default_state()

        # On startup we intentionally rebuild everything from the current
        # database state. This catches reservations/attendance changes that
        # happened while the bot was offline.
        #
        # During normal operation, rebuild only if board ordering/type changes;
        # ordinary reservation/attendance changes edit the existing message.
        if force_rebuild and state.get("messages"):
            await delete_all_state_messages(channel=channel, state=state)
            current_keys = []
        elif current_keys != desired_keys and state.get("messages"):
            await delete_all_state_messages(channel=channel, state=state)
            current_keys = []

        state["channel_id"] = int(channel.id)

        active_keys = set(desired_keys)
        for key, message_id in list(state.get("messages", {}).items()):
            if key not in active_keys:
                await delete_message_if_present(channel, int(message_id))
                state["messages"].pop(key, None)
                state["content_hashes"].pop(key, None)

        for index, board in enumerate(desired):
            await send_or_edit_board(
                channel=channel,
                state=state,
                board=board,
            )

            if index < len(desired) - 1 and board.key not in current_keys:
                await asyncio.sleep(configured_reorder_delay())

        state["order"] = desired_keys
        save_state(state)


async def _debounced_reconcile(bot: discord.Client) -> None:
    global _DEBOUNCE_TASK

    try:
        await asyncio.sleep(configured_debounce_delay())
        await reconcile_situation_room(bot)
    except Exception:
        # Situation room failures must never take down a command callback.
        pass
    finally:
        _DEBOUNCE_REASONS.clear()
        _DEBOUNCE_TASK = None


def queue_situation_room_refresh(
    bot: discord.Client,
    *,
    reason: str = "",
) -> None:
    """Queue one shared refresh; changes arriving in the next few seconds coalesce."""
    global _DEBOUNCE_TASK

    if not configured_channel_id():
        return

    if reason:
        _DEBOUNCE_REASONS.add(str(reason))

    if _DEBOUNCE_TASK is not None and not _DEBOUNCE_TASK.done():
        return

    _DEBOUNCE_TASK = asyncio.create_task(_debounced_reconcile(bot))


def queue_situation_room_refresh_now(bot: discord.Client) -> None:
    """Queue an immediate normal reconciliation without the debounce."""
    async def runner() -> None:
        try:
            await reconcile_situation_room(bot)
        except Exception:
            pass

    asyncio.create_task(runner())


def queue_situation_room_startup_rebuild(bot: discord.Client) -> None:
    """Delete tracked boards, then rebuild Situation Room from the live database."""
    async def runner() -> None:
        try:
            await reconcile_situation_room(bot, force_rebuild=True)
        except Exception:
            pass

    asyncio.create_task(runner())
