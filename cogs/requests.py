from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import commands

from config import INSTRUCTOR_ROLE, MIN_VTOL_HOURS, TIME_OPTIONS
from services.qual_request_review_service import (
    QualRequestRecord,
    deny_qual_request,
    get_pending_qual_requests,
    increment_qual_request_ping_counts,
    mark_qual_request_mia,
)
from services.qualification_record_service import (
    QualAttemptRecord,
    get_qualification_attempts_for_user,
)
from services.ping_cooldown_service import (
    check_ping_cooldown,
    format_cooldown,
    try_ping_cooldown,
)
from services.private_view_service import (
    PrivateTimeoutView,
    bind_private_view,
    bind_view_to_original_response,
)
from services.permission_service import (
    require_instructor_command,
    member_is_admin,
)


ORDER_OPTIONS = [
    ("Oldest", "oldest"),
    ("Newest", "newest"),
    ("VTOL Time", "vtol_time"),
    ("Pings - Ascending", "pings_asc"),
    ("Pings - Descending", "pings_desc"),
    ("Bumped - Ascending", "bumped_asc"),
    ("Bumped - Descending", "bumped_desc"),
]


FILTER_OPTIONS = [
    ("Online Now", "online_now"),
    ("Available Now", "available_now"),
    ("Age 13+: No", "age_no"),
    ("Under VTOL Hours", "under_vtol_hours"),
]


PING_FILTER_OPTIONS = [
    ("Times pinged: All", "all"),
    ("Pings: 0", "0"),
    ("Pings: 1", "1"),
    ("Pings: 2", "2"),
    ("Pings: 3+", "3_plus"),
    ("Pings: 0-1", "0_1"),
    ("Pings: 0-2", "0_2"),
    ("Pings: 0-3", "0_3"),
]


ANSI_RESET = "\u001b[0m"
ANSI_RED = "\u001b[31m"
ANSI_GREEN = "\u001b[32m"
ANSI_YELLOW = "\u001b[33m"


def has_instructor_role(member: discord.Member) -> bool:
    if member_is_admin(member):
        return True
    return any(role.id == INSTRUCTOR_ROLE for role in member.roles)


def instructor_mention() -> str:
    if INSTRUCTOR_ROLE:
        return f"<@&{INSTRUCTOR_ROLE}>"

    return "@instructor"


def time_label(value: str | None) -> str:
    if not value:
        return "Not set"

    for label, stored_value in TIME_OPTIONS:
        if stored_value == value:
            return label

    return value


def time_index(value: str | None) -> int | None:
    if value is None:
        return None

    for index, (_, stored_value) in enumerate(TIME_OPTIONS):
        if stored_value == value:
            return index

    return None


def discord_timestamp(timestamp: int | None) -> str:
    if not timestamp:
        return "Unknown"

    try:
        return f"<t:{int(timestamp)}:R>"
    except Exception:
        return "Unknown"


def yes_no(value: int | None) -> str:
    if value is None:
        return "Not set"

    return "Yes" if int(value) == 1 else "No"


def format_hours(value: float | None) -> str:
    if value is None:
        return "Not set"

    if float(value).is_integer():
        return str(int(value))

    return str(value)


def get_request_display_name(
    request: QualRequestRecord,
    guild: discord.Guild | None,
) -> str:
    if request.discord_id and guild:
        member = guild.get_member(int(request.discord_id))
        if member:
            return member.display_name

    return request.discord_username or request.discord_id or "Unknown User"


def get_request_member(
    request: QualRequestRecord,
    guild: discord.Guild | None,
) -> discord.Member | None:
    if not request.discord_id or not guild:
        return None

    try:
        return guild.get_member(int(request.discord_id))
    except ValueError:
        return None


def is_request_user_online(
    request: QualRequestRecord,
    guild: discord.Guild | None,
) -> bool:
    member = get_request_member(request, guild)

    if member is None:
        return False

    if member.status == discord.Status.offline:
        return False

    return member.status in {
        discord.Status.online,
        discord.Status.idle,
        discord.Status.dnd,
    }


def get_local_datetime(request: QualRequestRecord) -> datetime | None:
    if not request.timezone:
        return None

    try:
        return datetime.now(ZoneInfo(request.timezone))
    except ZoneInfoNotFoundError:
        return None
    except Exception:
        return None


def local_time_text(request: QualRequestRecord) -> str:
    local_now = get_local_datetime(request)

    if local_now is None:
        return "Unknown"

    hour = local_now.hour % 12
    if hour == 0:
        hour = 12

    return f"{local_now.strftime('%A')}, {hour}:{local_now.minute:02d} {local_now.strftime('%p')}"


def is_request_available_now(request: QualRequestRecord) -> bool:
    if (
        not request.timezone
        or not request.dotw
        or not request.availability_start
        or not request.availability_end
    ):
        return False

    local_now = get_local_datetime(request)

    if local_now is None:
        return False

    current_day = local_now.strftime("%A")

    available_days = {
        day.strip()
        for day in request.dotw.split(",")
        if day.strip()
    }

    if current_day not in available_days:
        return False

    start_idx = time_index(request.availability_start)
    end_idx = time_index(request.availability_end)

    if start_idx is None or end_idx is None:
        return False

    current_hhmm = local_now.strftime("%H:00")
    current_idx = time_index(current_hhmm)

    if current_idx is None:
        return False

    return start_idx <= current_idx < end_idx


def is_under_minimum_vtol_hours(request: QualRequestRecord) -> bool:
    if request.hours is None:
        return True

    try:
        return float(request.hours) < float(MIN_VTOL_HOURS)
    except Exception:
        return True


def is_age_no(request: QualRequestRecord) -> bool:
    return request.of_age is not None and int(request.of_age) == 0


def color_for_request(
    request: QualRequestRecord,
    guild: discord.Guild | None,
) -> str | None:
    # Color hierarchy: red > yellow > green.
    if is_age_no(request):
        return ANSI_RED

    if is_under_minimum_vtol_hours(request):
        return ANSI_YELLOW

    if is_request_user_online(request, guild):
        return ANSI_GREEN

    return None


class QualRequestReviewSession:
    def __init__(self, owner_id: int):
        self.owner_id = owner_id
        self.order_mode = "oldest"

        # Empty means all.
        self.filter_modes: set[str] = set()

        # "all", "0", "1", "2", "3_plus", "0_1", "0_2", "0_3".
        self.ping_filter: str = "all"

        self.selected_index = 0
        self.requests: list[QualRequestRecord] = []

        self.action_remarks: str | None = None

    @property
    def selected_request(self) -> QualRequestRecord | None:
        if not self.requests:
            return None

        self.selected_index = max(0, min(self.selected_index, len(self.requests) - 1))
        return self.requests[self.selected_index]

    def request_matches_filters(
        self,
        request: QualRequestRecord,
        guild: discord.Guild | None,
    ) -> bool:
        if not ping_count_matches_filter(request.times_pinged, self.ping_filter):
            return False

        # No selected filter means show all pending requests, subject to ping filter.
        if not self.filter_modes:
            return True

        # Swiss-cheese / AND behavior:
        # every selected filter must match.
        # Example: Online Now + Under VTOL Hours means the user must be both online
        # and under the configured VTOL hour minimum.
        if "online_now" in self.filter_modes and not is_request_user_online(request, guild):
            return False

        if "available_now" in self.filter_modes and not is_request_available_now(request):
            return False

        if "age_no" in self.filter_modes and not is_age_no(request):
            return False

        if "under_vtol_hours" in self.filter_modes and not is_under_minimum_vtol_hours(request):
            return False

        return True

    def refresh_requests(self, guild: discord.Guild | None) -> None:
        requests = get_pending_qual_requests(self.order_mode)

        requests = [
            request
            for request in requests
            if self.request_matches_filters(request, guild)
        ]

        self.requests = requests

        if self.requests:
            self.selected_index = max(0, min(self.selected_index, len(self.requests) - 1))
        else:
            self.selected_index = 0

    def move_previous(self):
        if not self.requests:
            self.selected_index = 0
            return

        self.selected_index = max(0, self.selected_index - 1)

    def move_next(self):
        if not self.requests:
            self.selected_index = 0
            return

        self.selected_index = min(len(self.requests) - 1, self.selected_index + 1)

    def get_window(self) -> tuple[int, int]:
        total = len(self.requests)

        if total <= 11:
            return 0, total

        # Keep selected request in the middle when possible.
        start = self.selected_index - 5
        end = self.selected_index + 6

        if start < 0:
            start = 0
            end = 11

        if end > total:
            end = total
            start = total - 11

        return start, end


def order_label_text(session: QualRequestReviewSession) -> str:
    return next(
        (label for label, value in ORDER_OPTIONS if value == session.order_mode),
        session.order_mode,
    )


def filter_label_text(session: QualRequestReviewSession) -> str:
    labels = [
        label
        for label, value in FILTER_OPTIONS
        if value in session.filter_modes
    ]

    if not labels:
        return "All"

    return ", ".join(labels)


def ping_filter_label_text(session: QualRequestReviewSession) -> str:
    for label, value in PING_FILTER_OPTIONS:
        if value == session.ping_filter:
            return label

    return "All"


def ping_count_matches_filter(times_pinged: int, ping_filter: str) -> bool:
    count = int(times_pinged or 0)

    if ping_filter == "all":
        return True

    if ping_filter == "0":
        return count == 0

    if ping_filter == "1":
        return count == 1

    if ping_filter == "2":
        return count == 2

    if ping_filter == "3_plus":
        return count >= 3

    if ping_filter == "0_1":
        return 0 <= count <= 1

    if ping_filter == "0_2":
        return 0 <= count <= 2

    if ping_filter == "0_3":
        return 0 <= count <= 3

    return True


def build_name_window(
    session: QualRequestReviewSession,
    guild: discord.Guild | None,
) -> str:
    if not session.requests:
        return "No pending requests"

    start, end = session.get_window()
    lines = []

    for index in range(start, end):
        request = session.requests[index]
        username = get_request_display_name(request, guild)

        prefix = "> " if index == session.selected_index else "  "
        line = f"{prefix}{username}"

        color = color_for_request(request, guild)
        if color:
            line = f"{color}{line}{ANSI_RESET}"

        lines.append(line)

    return "\n".join(lines)



QUAL_SCORE_KEY = "A/G A/A Form Tank Case1 Carrier Result"


def qual_rating_emoji(value: int | None) -> str:
    """
    Display scale used by /paperwork:
    NULL/0 = white N/A
    1 = red
    2 = orange
    3 = yellow
    4 = green
    5 = legacy computer
    """
    if value is None:
        return "⚪"

    try:
        value = int(value)
    except Exception:
        return "⚪"

    return {
        0: "⚪",
        1: "🔴",
        2: "🟠",
        3: "🟡",
        4: "🟢",
        5: "💻",
    }.get(value, "⚪")


def qual_result_emoji(attempt: QualAttemptRecord) -> str:
    if attempt.passed is True:
        return "✅"

    if attempt.passed is False:
        return "❌"

    return "⬜"


def qual_result_word(attempt: QualAttemptRecord) -> str:
    if attempt.passed is True:
        return "pass"

    if attempt.passed is False:
        return "fail"

    return "unknown"


def build_qual_attempt_history_block(discord_id: str | None) -> str:
    if not discord_id:
        return ""

    attempts = get_qualification_attempts_for_user(str(discord_id))

    if not attempts:
        return ""

    lines = [
        f"Key: {QUAL_SCORE_KEY}",
    ]

    for attempt_number, attempt in enumerate(attempts, start=1):
        color = ANSI_GREEN if attempt.passed is True else ANSI_RED
        result = qual_result_word(attempt)

        lines.append(
            f"{color}{attempt_number:<2} {result:<7} "
            f"{qual_rating_emoji(attempt.ag_rating)} "
            f"{qual_rating_emoji(attempt.aa_rating)} "
            f"{qual_rating_emoji(attempt.formation_rating)} "
            f"{qual_rating_emoji(attempt.tank_rating)} "
            f"{qual_rating_emoji(attempt.case1_rating)} "
            f"{qual_rating_emoji(attempt.carrier_rating)} "
            f"{qual_result_emoji(attempt)}{ANSI_RESET}"
        )

    block = "```ansi\n" + "\n".join(lines) + "\n```"

    # Keep the Selected field under Discord's 1024-character field limit.
    if len(block) > 650:
        trimmed_lines = [lines[0], "..."]
        trimmed_lines.extend(lines[-8:])
        block = "```ansi\n" + "\n".join(trimmed_lines) + "\n```"

    return block


def build_selected_summary(
    selected: QualRequestRecord,
    guild: discord.Guild | None,
    *,
    warn_qual_attempts: bool = False,
) -> str:
    show_qual_warning = warn_qual_attempts and int(selected.qual_attempts or 0) > 0

    qual_attempts_label = (
        "⚠️ **Qual Attempts:**"
        if show_qual_warning
        else "**Qual Attempts:**"
    )

    attempt_history_block = build_qual_attempt_history_block(selected.discord_id)

    return (
        f"**User:** <@{selected.discord_id}>\n"
        f"**Submitted:** {discord_timestamp(selected.created_at)}\n"
        f"**Bumped:** {discord_timestamp(selected.updated_at)}\n"
        f"**Pings Sent:** {selected.times_pinged}\n"
        f"{qual_attempts_label} {selected.qual_attempts}\n"
        f"{attempt_history_block}"
        f"**Online Now:** {'Yes' if is_request_user_online(selected, guild) else 'No'}\n"
        f"**Available Now:** {'Yes' if is_request_available_now(selected) else 'No'}"
    )


def build_applicant_info(selected: QualRequestRecord) -> str:
    age_warning = (
        "⚠️ "
        if is_age_no(selected)
        else ""
    )

    hours_warning = (
        "⚠️ "
        if is_under_minimum_vtol_hours(selected)
        else ""
    )

    return (
        f"{age_warning}**Older Than 13:** {yes_no(selected.of_age)}\n"
        f"{hours_warning}**VTOL Time:** {format_hours(selected.hours)} / {MIN_VTOL_HOURS}\n"
        f"**Preferred Aircraft:** {selected.preferred_aircraft or 'Not set'}\n"
        f"**Referral:** {selected.referral or 'Not set'}"
    )


def build_availability_info(selected: QualRequestRecord) -> str:
    availability_emoji = "🟢" if is_request_available_now(selected) else "🔴"

    return (
        f"{availability_emoji} **Availability**\n"
        f"**Time Zone:** {selected.timezone or 'Not set'}\n"
        f"**Local Time:** {local_time_text(selected)}\n"
        f"**Days:** {selected.dotw or 'Not set'}\n"
        f"**Window:** {time_label(selected.availability_start)} → {time_label(selected.availability_end)}"
    )


def build_review_embed(
    session: QualRequestReviewSession,
    guild: discord.Guild | None,
) -> discord.Embed:
    selected = session.selected_request

    embed = discord.Embed(
        title="Requests",
    )

    embed.add_field(
        name="View",
        value=(
            f"Order: {order_label_text(session)} | "
            f"Filter: {filter_label_text(session)} | "
            f"Ping Filter: {ping_filter_label_text(session)} | "
            f"Results: {len(session.requests)}\n"
            f"```ansi\n{build_name_window(session, guild)}\n```"
        ),
        inline=False,
    )

    if not session.requests or selected is None:
        embed.add_field(
            name="Selected",
            value=(
                "No pending requests match this view.\n\n"
                "Note: Online Now requires the bot to have Presence Intent enabled. "
                "Selected filters stack together, so every selected filter must match."
            ),
            inline=False,
        )
        return embed

    embed.add_field(
        name="Selected",
        value=build_selected_summary(selected, guild, warn_qual_attempts=True),
        inline=False,
    )

    embed.add_field(
        name="Application Info",
        value=build_applicant_info(selected),
        inline=False,
    )

    embed.add_field(
        name="Availability",
        value=build_availability_info(selected),
        inline=False,
    )

    embed.set_footer(
        text=f"Request {session.selected_index + 1} of {len(session.requests)}"
    )

    return embed


def requests_ping_user_cooldown_key(user_id: int | str, request_id: int | str) -> str:
    return f"requests_ping_user:{user_id}:{request_id}"


def requests_ping_all_cooldown_key(user_id: int | str) -> str:
    return f"requests_ping_all:{user_id}"


def confirm_ping_cooldown_key(
    *,
    user_id: int | str,
    targets: list[QualRequestRecord],
    mode: str,
) -> str:
    if mode == "all":
        return requests_ping_all_cooldown_key(user_id)

    if len(targets) == 1:
        return requests_ping_user_cooldown_key(user_id, targets[0].id)

    target_ids = ",".join(str(target.id) for target in targets)
    return f"requests_ping_batch:{user_id}:{target_ids}"


class InstructorOnlyView(PrivateTimeoutView):
    def __init__(self, session: QualRequestReviewSession):
        super().__init__()

        self.session = session

        self.add_item(OrderSelect(session))          # row 0
        self.add_item(FilterSelect(session))         # row 1
        self.add_item(PingFilterSelect(session))      # row 2

        self.add_item(BackButton(session))           # row 3
        self.add_item(MarkMIAButton(session))        # row 3
        self.add_item(DenyButton(session))           # row 3
        self.add_item(NextButton(session))           # row 3

        self.add_item(PingUserButton(session))       # row 4
        self.add_item(PingAllButton(session))        # row 4

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.session.owner_id:
            await interaction.response.send_message(
                "Only the instructor who opened this viewer can use these controls.",
                ephemeral=True,
            )
            return False

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This tool can only be used inside the server.",
                ephemeral=True,
            )
            return False

        if not has_instructor_role(interaction.user):
            await interaction.response.send_message(
                "You need the instructor role to use this tool.",
                ephemeral=True,
            )
            return False

        return True

    async def refresh(self, interaction: discord.Interaction):
        self.session.refresh_requests(interaction.guild)

        view = InstructorOnlyView(self.session)

        await interaction.response.edit_message(
            embed=build_review_embed(self.session, interaction.guild),
            view=bind_private_view(view, interaction.message),
        )


class OrderSelect(discord.ui.Select):
    def __init__(self, session: QualRequestReviewSession):
        self.session = session

        options = [
            discord.SelectOption(
                label=label,
                value=value,
                default=session.order_mode == value,
            )
            for label, value in ORDER_OPTIONS
        ]

        super().__init__(
            placeholder="Sort",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        self.session.order_mode = self.values[0]
        self.session.selected_index = 0

        assert self.view is not None
        await self.view.refresh(interaction)


class FilterSelect(discord.ui.Select):
    def __init__(self, session: QualRequestReviewSession):
        self.session = session

        options = [
            discord.SelectOption(
                label=label,
                value=value,
                default=value in session.filter_modes,
            )
            for label, value in FILTER_OPTIONS
        ]

        super().__init__(
            placeholder="Filter",
            min_values=0,
            max_values=4,
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        self.session.filter_modes = set(self.values)
        self.session.selected_index = 0

        assert self.view is not None
        await self.view.refresh(interaction)


class PingFilterSelect(discord.ui.Select):
    def __init__(self, session: QualRequestReviewSession):
        self.session = session

        options = [
            discord.SelectOption(
                label=label,
                value=value,
                default=value == session.ping_filter,
            )
            for label, value in PING_FILTER_OPTIONS
        ]

        super().__init__(
            placeholder="Ping filter",
            min_values=1,
            max_values=1,
            options=options,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction):
        self.session.ping_filter = self.values[0]
        self.session.selected_index = 0

        assert self.view is not None
        await self.view.refresh(interaction)


class BackButton(discord.ui.Button):
    def __init__(self, session: QualRequestReviewSession):
        super().__init__(
            label="Back",
            style=discord.ButtonStyle.secondary,
            row=3,
        )
        self.session = session

    async def callback(self, interaction: discord.Interaction):
        self.session.move_previous()

        assert self.view is not None
        await self.view.refresh(interaction)


class NextButton(discord.ui.Button):
    def __init__(self, session: QualRequestReviewSession):
        super().__init__(
            label="Next",
            style=discord.ButtonStyle.secondary,
            row=3,
        )
        self.session = session

    async def callback(self, interaction: discord.Interaction):
        self.session.move_next()

        assert self.view is not None
        await self.view.refresh(interaction)


class MarkMIAButton(discord.ui.Button):
    def __init__(self, session: QualRequestReviewSession):
        super().__init__(
            label="MIA",
            style=discord.ButtonStyle.danger,
            row=3,
        )
        self.session = session

    async def callback(self, interaction: discord.Interaction):
        selected = self.session.selected_request

        if selected is None:
            await interaction.response.send_message(
                "No request is currently selected.",
                ephemeral=True,
            )
            return

        self.session.action_remarks = None

        await interaction.response.edit_message(
            embed=build_action_embed(self.session, interaction.guild, "mia"),
            view=ActionReviewView(self.session, "mia"),
        )


class DenyButton(discord.ui.Button):
    def __init__(self, session: QualRequestReviewSession):
        super().__init__(
            label="Deny",
            style=discord.ButtonStyle.danger,
            row=3,
        )
        self.session = session

    async def callback(self, interaction: discord.Interaction):
        selected = self.session.selected_request

        if selected is None:
            await interaction.response.send_message(
                "No request is currently selected.",
                ephemeral=True,
            )
            return

        self.session.action_remarks = None

        await interaction.response.edit_message(
            embed=build_action_embed(self.session, interaction.guild, "deny"),
            view=ActionReviewView(self.session, "deny"),
        )


class PingUserButton(discord.ui.Button):
    def __init__(self, session: QualRequestReviewSession):
        selected = session.selected_request
        disabled = selected is None
        label = "Ping User"

        if selected is not None:
            cooldown = check_ping_cooldown(
                requests_ping_user_cooldown_key(session.owner_id, selected.id)
            )

            if not cooldown.allowed:
                disabled = True
                label = f"Ping User ({format_cooldown(cooldown.remaining_seconds)})"

        super().__init__(
            label=label,
            style=discord.ButtonStyle.primary,
            row=4,
            disabled=disabled,
        )
        self.session = session

    async def callback(self, interaction: discord.Interaction):
        selected = self.session.selected_request

        if selected is None:
            await interaction.response.send_message(
                "No request is currently selected.",
                ephemeral=True,
            )
            return

        await interaction.response.edit_message(
            embed=build_ping_confirm_embed(
                session=self.session,
                guild=interaction.guild,
                targets=[selected],
                mode="user",
            ),
            view=bind_private_view(
                ConfirmPingView(
                    session=self.session,
                    targets=[selected],
                    mode="user",
                ),
                interaction.message,
            ),
        )


class PingAllButton(discord.ui.Button):
    def __init__(self, session: QualRequestReviewSession):
        cooldown = check_ping_cooldown(
            requests_ping_all_cooldown_key(session.owner_id)
        )
        label = "Ping All"
        disabled = not cooldown.allowed

        if not cooldown.allowed:
            label = f"Ping All ({format_cooldown(cooldown.remaining_seconds)})"

        super().__init__(
            label=label,
            style=discord.ButtonStyle.primary,
            row=4,
            disabled=disabled,
        )
        self.session = session

    async def callback(self, interaction: discord.Interaction):
        targets = [
            request
            for request in self.session.requests
            if request.discord_id
        ]

        if not targets:
            await interaction.response.send_message(
                "There are no matching users to ping.",
                ephemeral=True,
            )
            return

        await interaction.response.edit_message(
            embed=build_ping_confirm_embed(
                session=self.session,
                guild=interaction.guild,
                targets=targets,
                mode="all",
            ),
            view=bind_private_view(
                ConfirmPingView(
                    session=self.session,
                    targets=targets,
                    mode="all",
                ),
                interaction.message,
            ),
        )


def build_ping_confirm_embed(
    session: QualRequestReviewSession,
    guild: discord.Guild | None,
    targets: list[QualRequestRecord],
    mode: str,
) -> discord.Embed:
    title = "Confirm Ping User" if mode == "user" else "Confirm Ping All"

    embed = discord.Embed(
        title=title,
        description=(
            "Confirm before sending a ping message.\n\n"
            f"**Targets:** {len(targets)}"
        ),
    )

    lines = []
    for request in targets[:11]:
        lines.append(get_request_display_name(request, guild))

    if len(targets) > 11:
        lines.append(f"...and {len(targets) - 11} more")

    embed.add_field(
        name="Users",
        value=f"```text\n{chr(10).join(lines)}\n```",
        inline=False,
    )

    embed.add_field(
        name="Current Filter",
        value=(
            f"Order: {order_label_text(session)} | "
            f"Filter: {filter_label_text(session)} | "
            f"Ping Filter: {ping_filter_label_text(session)}"
        ),
        inline=False,
    )

    return embed


class ConfirmPingView(PrivateTimeoutView):
    def __init__(
        self,
        session: QualRequestReviewSession,
        targets: list[QualRequestRecord],
        mode: str,
    ):
        super().__init__()
        self.session = session
        self.targets = targets
        self.mode = mode

        self.add_item(ConfirmPingButton(session, targets, mode))
        self.add_item(CancelToReviewButton(session))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.session.owner_id:
            await interaction.response.send_message(
                "Only the instructor who opened this viewer can use these controls.",
                ephemeral=True,
            )
            return False

        return True


class ConfirmPingButton(discord.ui.Button):
    def __init__(
        self,
        session: QualRequestReviewSession,
        targets: list[QualRequestRecord],
        mode: str,
    ):
        cooldown = check_ping_cooldown(
            confirm_ping_cooldown_key(
                user_id=session.owner_id,
                targets=targets,
                mode=mode,
            )
        )
        label = "Confirm"
        disabled = not cooldown.allowed

        if not cooldown.allowed:
            label = f"Confirm ({format_cooldown(cooldown.remaining_seconds)})"

        super().__init__(
            label=label,
            style=discord.ButtonStyle.success,
            row=0,
            disabled=disabled,
        )
        self.session = session
        self.targets = targets
        self.mode = mode

    async def callback(self, interaction: discord.Interaction):
        if interaction.channel is None:
            await interaction.response.send_message(
                "I could not find the current channel to send a ping.",
                ephemeral=True,
            )
            return

        targets = [
            target
            for target in self.targets
            if target.discord_id
        ]

        if not targets:
            await interaction.response.send_message(
                "There are no users to ping.",
                ephemeral=True,
            )
            return

        cooldown_key = confirm_ping_cooldown_key(
            user_id=interaction.user.id,
            targets=targets,
            mode=self.mode,
        )

        cooldown = try_ping_cooldown(cooldown_key)

        if not cooldown.allowed:
            await interaction.response.send_message(
                (
                    "Please wait before sending another qualification ping. "
                    f"Cooldown remaining: **{format_cooldown(cooldown.remaining_seconds)}**."
                ),
                ephemeral=True,
            )
            return

        await send_ping_messages(interaction.channel, targets, interaction.user.mention)
        increment_qual_request_ping_counts([target.id for target in targets])

        self.session.refresh_requests(interaction.guild)

        view = InstructorOnlyView(self.session)

        await interaction.response.edit_message(
            embed=build_review_embed(self.session, interaction.guild),
            view=bind_private_view(view, interaction.message),
        )


class CancelToReviewButton(discord.ui.Button):
    def __init__(self, session: QualRequestReviewSession):
        super().__init__(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            row=0,
        )
        self.session = session

    async def callback(self, interaction: discord.Interaction):
        self.session.refresh_requests(interaction.guild)

        view = InstructorOnlyView(self.session)

        await interaction.response.edit_message(
            embed=build_review_embed(self.session, interaction.guild),
            view=bind_private_view(view, interaction.message),
        )


async def send_ping_messages(
    channel: discord.abc.Messageable,
    targets: list[QualRequestRecord],
    instructor_mention_text: str,
):
    mentions = [f"<@{target.discord_id}>" for target in targets if target.discord_id]

    if not mentions:
        return

    prefix = "Qualification request ping: "
    suffix = f"\n{instructor_mention_text} is available now for a qualification test."

    chunks: list[str] = []
    current = prefix

    for mention in mentions:
        addition = mention + " "

        if len(current) + len(addition) + len(suffix) > 1900:
            chunks.append(current.strip() + suffix)
            current = prefix + addition
        else:
            current += addition

    if current.strip() != prefix.strip():
        chunks.append(current.strip() + suffix)

    for chunk in chunks:
        await channel.send(
            chunk,
            allowed_mentions=discord.AllowedMentions(
                users=True,
                roles=True,
                everyone=False,
            ),
        )


def build_action_embed(
    session: QualRequestReviewSession,
    guild: discord.Guild | None,
    action_type: str,
) -> discord.Embed:
    selected = session.selected_request

    if selected is None:
        return discord.Embed(
            title="No Request Selected",
            description="Return to the viewer and select a request.",
        )

    title = "Deny Qualification Request" if action_type == "deny" else "Mark Request MIA"

    instructions = (
        "Use **Remarks** to enter the denial reason. "
        "When you press **Send**, the requester will receive a DM and the request will be marked denied."
        if action_type == "deny"
        else
        "Use **Remarks** to enter why this user is being marked MIA. "
        "When you press **Confirm**, the request will be marked MIA. No DM will be sent."
    )

    embed = discord.Embed(
        title=title,
        description=instructions,
    )

    embed.add_field(
        name="Selected",
        value=build_selected_summary(selected, guild),
        inline=False,
    )

    embed.add_field(
        name="Application Info",
        value=build_applicant_info(selected),
        inline=False,
    )

    embed.add_field(
        name="Availability",
        value=build_availability_info(selected),
        inline=False,
    )

    embed.add_field(
        name="Instructor Remarks",
        value=session.action_remarks or "Not set",
        inline=False,
    )

    return embed


class ActionReviewView(discord.ui.View):
    def __init__(
        self,
        session: QualRequestReviewSession,
        action_type: str,
    ):
        super().__init__(timeout=900)
        self.session = session
        self.action_type = action_type

        self.add_item(ActionRemarksButton(session, action_type))

        self.add_item(ActionCancelButton(session))

        if action_type == "deny":
            self.add_item(ActionDenySendButton(session))
        else:
            self.add_item(ActionMIAConfirmButton(session))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.session.owner_id:
            await interaction.response.send_message(
                "Only the instructor who opened this viewer can use these controls.",
                ephemeral=True,
            )
            return False

        return True


class ActionRemarksModal(discord.ui.Modal):
    def __init__(
        self,
        session: QualRequestReviewSession,
        action_type: str,
    ):
        title = "Denial Remarks" if action_type == "deny" else "MIA Remarks"
        super().__init__(title=title)

        self.session = session
        self.action_type = action_type

        self.remarks_input = discord.ui.TextInput(
            label="Remarks",
            placeholder="Explain the reason.",
            default=session.action_remarks or "",
            style=discord.TextStyle.paragraph,
            max_length=1000,
            required=True,
        )

        self.add_item(self.remarks_input)

    async def on_submit(self, interaction: discord.Interaction):
        remarks = str(self.remarks_input.value).strip()

        if not remarks:
            await interaction.response.send_message(
                "Remarks are required.",
                ephemeral=True,
            )
            return

        self.session.action_remarks = remarks

        await interaction.response.edit_message(
            embed=build_action_embed(self.session, interaction.guild, self.action_type),
            view=ActionReviewView(self.session, self.action_type),
        )


class ActionRemarksButton(discord.ui.Button):
    def __init__(
        self,
        session: QualRequestReviewSession,
        action_type: str,
    ):
        super().__init__(
            label="Remarks",
            style=discord.ButtonStyle.primary,
            row=0,
        )
        self.session = session
        self.action_type = action_type

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            ActionRemarksModal(self.session, self.action_type)
        )


class ActionCancelButton(discord.ui.Button):
    def __init__(self, session: QualRequestReviewSession):
        super().__init__(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        self.session = session

    async def callback(self, interaction: discord.Interaction):
        self.session.action_remarks = None
        self.session.refresh_requests(interaction.guild)

        view = InstructorOnlyView(self.session)

        await interaction.response.edit_message(
            embed=build_review_embed(self.session, interaction.guild),
            view=bind_private_view(view, interaction.message),
        )


class ActionDenySendButton(discord.ui.Button):
    def __init__(self, session: QualRequestReviewSession):
        super().__init__(
            label="Send",
            style=discord.ButtonStyle.danger,
            row=1,
        )
        self.session = session

    async def callback(self, interaction: discord.Interaction):
        selected = self.session.selected_request

        if selected is None:
            await interaction.response.send_message(
                "No request is selected.",
                ephemeral=True,
            )
            return

        if not self.session.action_remarks:
            await interaction.response.send_message(
                "Remarks are required before denying the request.",
                ephemeral=True,
            )
            return

        dm_status = "DM sent."

        if selected.discord_id:
            try:
                user = await interaction.client.fetch_user(int(selected.discord_id))
                await user.send(
                    "Your qualification request has been denied.\n\n"
                    f"Reason:\n{self.session.action_remarks}"
                )
            except Exception:
                dm_status = "DM failed, but the request was marked denied."

        deny_qual_request(
            request_id=selected.id,
            instructor_discord_id=str(interaction.user.id),
            remarks=self.session.action_remarks,
        )

        self.session.action_remarks = None
        self.session.refresh_requests(interaction.guild)

        await interaction.response.edit_message(
            content=dm_status,
            embed=build_review_embed(self.session, interaction.guild),
            view=InstructorOnlyView(self.session),
        )


class ActionMIAConfirmButton(discord.ui.Button):
    def __init__(self, session: QualRequestReviewSession):
        super().__init__(
            label="Confirm",
            style=discord.ButtonStyle.danger,
            row=1,
        )
        self.session = session

    async def callback(self, interaction: discord.Interaction):
        selected = self.session.selected_request

        if selected is None:
            await interaction.response.send_message(
                "No request is selected.",
                ephemeral=True,
            )
            return

        if not self.session.action_remarks:
            await interaction.response.send_message(
                "Remarks are required before marking MIA.",
                ephemeral=True,
            )
            return

        mark_qual_request_mia(
            request_id=selected.id,
            instructor_discord_id=str(interaction.user.id),
            remarks=self.session.action_remarks,
        )

        self.session.action_remarks = None
        self.session.refresh_requests(interaction.guild)

        view = InstructorOnlyView(self.session)

        await interaction.response.edit_message(
            embed=build_review_embed(self.session, interaction.guild),
            view=bind_private_view(view, interaction.message),
        )


class QualRequestCommands(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    
    @app_commands.command(
        name="requests",
        description="Review pending qualification requests.",
    )
    @app_commands.guild_only()
    async def qual_request_command(
        self,
        interaction: discord.Interaction,
    ):
        if not await require_instructor_command(interaction):
            return
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This command can only be used inside the server.",
                ephemeral=True,
            )
            return

        if not has_instructor_role(interaction.user):
            await interaction.response.send_message(
                "You need the instructor role to use this command.",
                ephemeral=True,
            )
            return

        session = QualRequestReviewSession(owner_id=interaction.user.id)
        session.refresh_requests(interaction.guild)

        view = InstructorOnlyView(session)

        await interaction.response.send_message(
            embed=build_review_embed(session, interaction.guild),
            view=view,
            ephemeral=True,
        )
        await bind_view_to_original_response(interaction, view)


async def setup(bot: commands.Bot):
    await bot.add_cog(QualRequestCommands(bot))
