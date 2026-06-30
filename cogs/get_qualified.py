from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import commands

from database import get_connection, ensure_user_settings_schema
from config import (
    AIRCRAFT_OPTIONS,
    INSTRUCTOR_ROLE,
    MIN_VTOL_HOURS,
    MISSION_QUALIFIED_ROLE,
    REFERRAL_OPTIONS,
    TIMEZONE_OPTIONS,
    TIME_OPTIONS,
)
from services.get_qualified_service import (
    AvailabilityUpdateDraft,
    ExistingQualificationRequest,
    QualificationRequestDraft,
    bump_or_reopen_existing_request,
    cancel_existing_request,
    compute_min_requirements,
    create_request_qual_in_db,
    ensure_user_exists,
    get_existing_pending_or_mia_request,
    update_existing_request_availability,
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


active_qualification_requests: dict[int, QualificationRequestDraft] = {}



def member_has_role(member: discord.Member, role_id: int | str | None) -> bool:
    try:
        rid = int(role_id or 0)
    except (TypeError, ValueError):
        return False

    if rid <= 0:
        return False

    return any(int(role.id) == rid for role in member.roles)


def aircraft_option_name(option) -> str:
    if isinstance(option, dict):
        return str(option.get("name") or option.get("label") or option.get("value") or "").strip()

    if isinstance(option, (tuple, list)) and option:
        return str(option[0]).strip()

    return str(option or "").strip()


def configured_aircraft_names() -> list[str]:
    names: list[str] = []
    seen: set[str] = set()

    for option in AIRCRAFT_OPTIONS:
        name = aircraft_option_name(option)
        if not name:
            continue

        key = name.casefold()
        if key in seen:
            continue

        seen.add(key)
        names.append(name)

    return names

DAYS_OF_WEEK = [
    ("Monday", "Monday"),
    ("Tuesday", "Tuesday"),
    ("Wednesday", "Wednesday"),
    ("Thursday", "Thursday"),
    ("Friday", "Friday"),
    ("Saturday", "Saturday"),
    ("Sunday", "Sunday"),
]


EXISTING_REQUEST_MESSAGE = (
    "You have already submitted a request. If you are available right now, "
    "press the button below. Thank you for your interest in our community. "
    "We look forward to have you as our wingman. Your request has moved to "
    "the top of our list."
)


CANCELLED_REQUEST_MESSAGE = (
    "If you change your mind just use /get qualified again to apply! "
    "Feel free to stick around and attend our Tournaments!"
)


def bool_text(value: bool | None) -> str:
    if value is None:
        return "Not set"

    return "Yes" if value else "No"


def format_hours(value: float | None) -> str:
    if value is None:
        return "Not set"

    value = float(value)

    if value.is_integer():
        return str(int(value))

    return str(value)


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


def instructor_mention() -> str:
    if INSTRUCTOR_ROLE:
        return f"<@&{INSTRUCTOR_ROLE}>"

    return "@instructor"


def parse_notification_time(value: str | None, default: str) -> int:
    text = str(value or default).strip()

    try:
        hour_text, minute_text = text.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except (TypeError, ValueError):
        hour_text, minute_text = default.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)

    hour = max(0, min(23, hour))
    minute = max(0, min(59, minute))

    return hour * 60 + minute


def is_within_notification_window(
    *,
    timezone: str | None,
    notify_start: str | None,
    notify_end: str | None,
) -> bool:
    if not timezone:
        return False

    try:
        local_now = datetime.now(ZoneInfo(str(timezone)))
    except ZoneInfoNotFoundError:
        return False
    except Exception:
        return False

    now_minutes = local_now.hour * 60 + local_now.minute
    start_minutes = parse_notification_time(notify_start, "09:00")
    end_minutes = parse_notification_time(notify_end, "21:00")

    if start_minutes == end_minutes:
        return True

    if start_minutes < end_minutes:
        return start_minutes <= now_minutes < end_minutes

    # Overnight window, example 21:00 -> 09:00.
    return now_minutes >= start_minutes or now_minutes < end_minutes


def instructor_notification_settings() -> dict[str, dict]:
    ensure_user_settings_schema()

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                discord_id,
                timezone,
                notify_start,
                notify_end,
                notify_instructor
            FROM user_settings
            WHERE notify_instructor = 1
            """
        ).fetchall()

    return {
        str(row["discord_id"]): dict(row)
        for row in rows
    }



def applicant_server_display_text(applicant: discord.abc.User) -> str:
    display_name = getattr(applicant, "display_name", None)
    global_name = getattr(applicant, "global_name", None)
    username = getattr(applicant, "name", None)

    lines = []

    if display_name:
        lines.append(f"Server Display: **{display_name}**")

    if global_name and global_name != display_name:
        lines.append(f"Global Name: **{global_name}**")

    if username and username not in {display_name, global_name}:
        lines.append(f"Username: `{username}`")

    return "\n".join(lines) if lines else "Not available"


def build_instructor_notification_embed(
    *,
    applicant: discord.abc.User,
    request_id: int,
    draft: QualificationRequestDraft,
    guild: discord.Guild,
) -> discord.Embed:
    embed = discord.Embed(
        title="New Qualification Request",
        description=(
            f"**{applicant.display_name}** submitted a new qualification request in **{guild.name}**.\n\n"
            "Use `/requests` to review pending qualification requests."
        ),
    )

    embed.add_field(
        name="Request ID",
        value=str(request_id),
        inline=True,
    )

    embed.add_field(
        name="Applicant",
        value=(
            f"{applicant.mention}\n"
            f"{applicant_server_display_text(applicant)}\n"
            f"Discord ID: `{applicant.id}`"
        ),
        inline=False,
    )

    embed.add_field(
        name="VTOL Hours",
        value=format_hours(draft.hours),
        inline=True,
    )

    embed.add_field(
        name="Age 13+",
        value=bool_text(draft.of_age),
        inline=True,
    )

    embed.add_field(
        name="Favorite Airframe",
        value=draft.preferred_aircraft or "Not set",
        inline=True,
    )

    embed.add_field(
        name="Referral",
        value=draft.referral or "Not set",
        inline=True,
    )

    embed.add_field(
        name="Availability",
        value=(
            f"Days: **{draft.dotw or 'Not set'}**\n"
            f"Time Zone: **{draft.timezone or 'Not set'}**\n"
            f"Hours: **{time_label(draft.availability_start)} → {time_label(draft.availability_end)}**"
        ),
        inline=False,
    )

    return embed


async def notify_instructors_of_new_qual_request(
    interaction: discord.Interaction,
    *,
    request_id: int,
    draft: QualificationRequestDraft,
) -> None:
    guild = interaction.guild

    if guild is None or not INSTRUCTOR_ROLE:
        return

    role = guild.get_role(int(INSTRUCTOR_ROLE))

    if role is None:
        return

    settings_by_id = instructor_notification_settings()
    embed = build_instructor_notification_embed(
        applicant=interaction.user,
        request_id=request_id,
        draft=draft,
        guild=guild,
    )

    for member in list(role.members):
        if member.bot:
            continue

        settings = settings_by_id.get(str(member.id))

        if not settings:
            continue

        if not is_within_notification_window(
            timezone=settings.get("timezone"),
            notify_start=settings.get("notify_start"),
            notify_end=settings.get("notify_end"),
        ):
            continue

        try:
            await member.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            continue
        except Exception:
            continue


ANSI_RESET = "\u001b[0m"
ANSI_GREEN = "\u001b[32m"
ANSI_RED = "\u001b[31m"


def requirements_text(draft: QualificationRequestDraft) -> str:
    if draft.hours is None or draft.of_age is None:
        return "Not set"

    return compute_min_requirements(draft.hours, draft.of_age)


def status_color(value_is_set: bool) -> str:
    return ANSI_GREEN if value_is_set else ANSI_RED


def ansi_status_value(label: str, value: str | None, *, is_set: bool | None = None) -> str:
    if is_set is None:
        is_set = bool(value and value != "Not set")

    shown = value if value else "Missing"
    return f"{status_color(is_set)}{label}: {shown}{ANSI_RESET}"


def availability_hours_text(draft: QualificationRequestDraft) -> str:
    start = time_label(draft.availability_start)
    end = time_label(draft.availability_end)

    if start == "Not set" or end == "Not set":
        return "Missing"

    return f"{start} → {end}"


def build_application_status_block(draft: QualificationRequestDraft) -> str:
    lines = [
        "Application Info",
        ansi_status_value(
            "VTOL Hours",
            format_hours(draft.hours),
            is_set=draft.hours is not None,
        ),
        ansi_status_value(
            "Age",
            bool_text(draft.of_age),
            is_set=draft.of_age is not None,
        ),
        ansi_status_value(
            "Favorite Airframe",
            draft.preferred_aircraft,
        ),
        ansi_status_value(
            "Days Available",
            draft.dotw,
        ),
        ansi_status_value(
            "Time Zone",
            draft.timezone,
        ),
        ansi_status_value(
            "Hours",
            availability_hours_text(draft),
            is_set=bool(draft.availability_start and draft.availability_end),
        ),
        ansi_status_value(
            "How did you hear about us?",
            draft.referral,
        ),
    ]

    return "```ansi\n" + "\n".join(lines) + "\n```"


def page_body_text(page: int) -> str:
    if page == 1:
        return (
            "**Application Info**\n"
            "Tell us your VTOL VR hours, age, and favorite airframe.\n\n"
            "Use the **Set VTOL Hours** button to enter the hours you currently have."
        )

    if page == 2:
        return (
            "**Availability**\n"
            "Tell us your timezone and when you are usually available for a qualification.\n\n"
            f"If you are available outside your submitted time, no worries. You can still ping "
            f"{instructor_mention()} and an instructor can respond if anyone is free."
        )

    return (
        "**Referral**\n"
        "Final step. Tell us how you found the group, then submit your request."
    )


def build_request_embed(
    draft: QualificationRequestDraft,
    page: int,
) -> discord.Embed:
    embed = discord.Embed(
        title="Get Mission Qualified",
        description=(
            build_application_status_block(draft)
            + "\n"
            + page_body_text(page)
        ),
    )

    embed.set_footer(text=f"Page {page} of 3")

    return embed





async def send_qualification_response(
    interaction: discord.Interaction,
    *,
    content: str | None = None,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
    ephemeral: bool = True,
) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(
            content=content,
            embed=embed,
            view=view,
            ephemeral=ephemeral,
        )
    else:
        await interaction.response.send_message(
            content=content,
            embed=embed,
            view=view,
            ephemeral=ephemeral,
        )

def build_existing_request_embed() -> discord.Embed:
    return discord.Embed(
        title="Qualification Application",
        description=EXISTING_REQUEST_MESSAGE,
    )


def build_adjust_availability_embed(draft: AvailabilityUpdateDraft) -> discord.Embed:
    embed = discord.Embed(
        title="Adjust Availability",
        description=(
            "Update the days and times you are normally available for a qualification."
        ),
    )

    embed.add_field(
        name="Timezone",
        value=draft.timezone or "Not set",
        inline=True,
    )

    embed.add_field(
        name="Days Available",
        value=draft.dotw or "Not set",
        inline=False,
    )

    embed.add_field(
        name="Availability Window",
        value=f"{time_label(draft.availability_start)} → {time_label(draft.availability_end)}",
        inline=False,
    )

    errors = draft.validate()

    if errors:
        embed.add_field(
            name="Missing Before Save",
            value="\n".join(f"- {error}" for error in errors)[:1024],
            inline=False,
        )
    else:
        embed.add_field(
            name="Ready",
            value="Press **Save** to update your availability.",
            inline=False,
        )

    return embed



class DismissMessageButton(discord.ui.Button):
    def __init__(self, row: int | None = None):
        super().__init__(
            label="Dismiss Message",
            style=discord.ButtonStyle.secondary,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content="Message dismissed.",
            embed=None,
            view=None,
        )

class BaseOwnerView(PrivateTimeoutView):
    def __init__(
        self,
        owner_id: int,
        timeout: int | None = None,
    ):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the user who opened this can use these controls.",
                ephemeral=True,
            )
            return False

        return True


class BaseRequestView(BaseOwnerView):
    def __init__(
        self,
        owner_id: int,
        draft: QualificationRequestDraft,
        timeout: int = 900,
    ):
        super().__init__(owner_id=owner_id, timeout=timeout)
        self.draft = draft


def ready_now_cooldown_key(user_id: int | str) -> str:
    return f"get_qualified_ready_now:{user_id}"


class ExistingRequestView(BaseOwnerView):
    def __init__(
        self,
        owner_id: int,
        existing_request: ExistingQualificationRequest,
    ):
        super().__init__(owner_id=owner_id)
        self.existing_request = existing_request

        self.add_item(ReadyNowButton(existing_request, owner_id))
        self.add_item(CancelExistingRequestButton(existing_request))
        self.add_item(AdjustAvailabilityButton(existing_request))
        self.add_item(DismissMessageButton())


class SubmittedRequestView(BaseOwnerView):
    def __init__(
        self,
        owner_id: int,
        existing_request: ExistingQualificationRequest,
    ):
        super().__init__(owner_id=owner_id)
        self.existing_request = existing_request

        self.add_item(ReadyNowButton(existing_request, owner_id))
        self.add_item(CancelSubmittedRequestButton(existing_request))
        self.add_item(AdjustAvailabilityButton(existing_request))
        self.add_item(DismissMessageButton())


class CancelSubmittedRequestButton(discord.ui.Button):
    def __init__(self, existing_request: ExistingQualificationRequest):
        super().__init__(
            label="Cancel Application",
            style=discord.ButtonStyle.danger,
            row=0,
        )
        self.existing_request = existing_request

    async def callback(self, interaction: discord.Interaction):
        cancel_existing_request(
            request_id=self.existing_request.id,
            discord_id=str(interaction.user.id),
        )

        await interaction.response.edit_message(
            content=CANCELLED_REQUEST_MESSAGE,
            embed=None,
            view=None,
        )


class ReadyNowButton(discord.ui.Button):
    def __init__(
        self,
        existing_request: ExistingQualificationRequest,
        owner_id: int | str,
    ):
        cooldown = check_ping_cooldown(ready_now_cooldown_key(owner_id))
        label = "I'm Ready Now"

        if not cooldown.allowed:
            label = f"Ready Now ({format_cooldown(cooldown.remaining_seconds)})"

        super().__init__(
            label=label,
            style=discord.ButtonStyle.success,
            row=0,
            disabled=not cooldown.allowed,
        )
        self.existing_request = existing_request

    async def callback(self, interaction: discord.Interaction):
        if interaction.channel is None:
            await interaction.response.send_message(
                "I could not find this channel to ping instructors.",
                ephemeral=True,
            )
            return

        cooldown = try_ping_cooldown(
            ready_now_cooldown_key(interaction.user.id),
        )

        if not cooldown.allowed:
            await interaction.response.send_message(
                (
                    "Please wait before using **I'm Ready Now** again. "
                    f"Cooldown remaining: **{format_cooldown(cooldown.remaining_seconds)}**."
                ),
                ephemeral=True,
            )
            return

        await interaction.channel.send(
            f"{instructor_mention()} {interaction.user.mention} is ready to be qualified!",
            allowed_mentions=discord.AllowedMentions(
                users=True,
                roles=True,
                everyone=False,
            ),
        )

        if isinstance(self.view, ExistingRequestView):
            replacement_view = ExistingRequestView(
                owner_id=interaction.user.id,
                existing_request=self.existing_request,
            )
        elif isinstance(self.view, SubmittedRequestView):
            replacement_view = SubmittedRequestView(
                owner_id=interaction.user.id,
                existing_request=self.existing_request,
            )
        else:
            replacement_view = None

        if replacement_view is not None:
            await interaction.response.edit_message(
                view=bind_private_view(replacement_view, interaction.message)
            )
        else:
            await interaction.response.defer()


class AdjustAvailabilityButton(discord.ui.Button):
    def __init__(self, existing_request: ExistingQualificationRequest):
        super().__init__(
            label="Adjust Availability",
            style=discord.ButtonStyle.primary,
            row=0,
        )
        self.existing_request = existing_request

    async def callback(self, interaction: discord.Interaction):
        draft = AvailabilityUpdateDraft(
            request_id=self.existing_request.id,
            discord_id=str(interaction.user.id),
            timezone=self.existing_request.timezone,
            availability_start=self.existing_request.availability_start,
            availability_end=self.existing_request.availability_end,
            dotw=self.existing_request.dotw,
        )

        await interaction.response.edit_message(
            embed=build_adjust_availability_embed(draft),
            view=AdjustAvailabilityView(interaction.user.id, draft),
        )


class CancelExistingRequestButton(discord.ui.Button):
    def __init__(self, existing_request: ExistingQualificationRequest):
        super().__init__(
            label="Cancel Application",
            style=discord.ButtonStyle.danger,
            row=0,
        )
        self.existing_request = existing_request

    async def callback(self, interaction: discord.Interaction):
        cancel_existing_request(
            request_id=self.existing_request.id,
            discord_id=str(interaction.user.id),
        )

        await interaction.response.edit_message(
            content=CANCELLED_REQUEST_MESSAGE,
            embed=None,
            view=None,
        )


class AdjustAvailabilityView(BaseOwnerView):
    def __init__(
        self,
        owner_id: int,
        draft: AvailabilityUpdateDraft,
    ):
        super().__init__(owner_id=owner_id)
        self.draft = draft

        self.add_item(AdjustDaysOfWeekSelect(draft))
        self.add_item(AdjustTimezoneSelect(draft))
        self.add_item(AdjustStartTimeSelect(draft))
        self.add_item(AdjustEndTimeSelect(draft))

        self.add_item(AdjustBackButton(draft))
        self.add_item(AdjustSaveButton(draft))

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=build_adjust_availability_embed(self.draft),
            view=AdjustAvailabilityView(self.owner_id, self.draft),
        )


class AdjustDaysOfWeekSelect(discord.ui.Select):
    def __init__(self, draft: AvailabilityUpdateDraft):
        self.draft = draft

        selected_days = set()

        if draft.dotw:
            selected_days = {
                day.strip()
                for day in draft.dotw.split(",")
                if day.strip()
            }

        options = [
            discord.SelectOption(
                label=label,
                value=value,
                default=value in selected_days,
            )
            for label, value in DAYS_OF_WEEK
        ]

        super().__init__(
            placeholder="Select days you are usually available",
            min_values=1,
            max_values=7,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        self.draft.dotw = ", ".join(self.values)

        assert self.view is not None
        await self.view.refresh(interaction)


class AdjustTimezoneSelect(discord.ui.Select):
    def __init__(self, draft: AvailabilityUpdateDraft):
        self.draft = draft

        options = [
            discord.SelectOption(
                label=label,
                value=value,
                description=value,
                default=draft.timezone == value,
            )
            for label, value in TIMEZONE_OPTIONS[:25]
        ]

        if not options:
            options = [
                discord.SelectOption(
                    label="No timezones configured",
                    value="none",
                    default=True,
                )
            ]

        super().__init__(
            placeholder="Select timezone",
            min_values=1,
            max_values=1,
            options=options,
            row=1,
            disabled=not TIMEZONE_OPTIONS,
        )

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            await interaction.response.send_message(
                "No timezones are configured in config.py.",
                ephemeral=True,
            )
            return

        self.draft.timezone = self.values[0]

        assert self.view is not None
        await self.view.refresh(interaction)


class AdjustStartTimeSelect(discord.ui.Select):
    def __init__(self, draft: AvailabilityUpdateDraft):
        self.draft = draft

        options = [
            discord.SelectOption(
                label=label,
                value=value,
                default=draft.availability_start == value,
            )
            for label, value in TIME_OPTIONS[:25]
        ]

        if not options:
            options = [
                discord.SelectOption(
                    label="No times configured",
                    value="none",
                    default=True,
                )
            ]

        super().__init__(
            placeholder="Select availability start time",
            min_values=1,
            max_values=1,
            options=options,
            row=2,
            disabled=not TIME_OPTIONS,
        )

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            await interaction.response.send_message(
                "No times are configured in config.py.",
                ephemeral=True,
            )
            return

        self.draft.availability_start = self.values[0]

        start_index = time_index(self.draft.availability_start)
        end_index = time_index(self.draft.availability_end)

        if (
            start_index is not None
            and end_index is not None
            and end_index <= start_index
        ):
            self.draft.availability_end = None

        assert self.view is not None
        await self.view.refresh(interaction)


class AdjustEndTimeSelect(discord.ui.Select):
    def __init__(self, draft: AvailabilityUpdateDraft):
        self.draft = draft

        start_index = time_index(draft.availability_start)

        if start_index is None:
            options = [
                discord.SelectOption(
                    label="Select start time first",
                    value="none",
                    default=True,
                )
            ]
            disabled = True
        else:
            valid_times = TIME_OPTIONS[start_index + 1:]

            if not valid_times:
                options = [
                    discord.SelectOption(
                        label="No later times available",
                        value="none",
                        default=True,
                    )
                ]
                disabled = True
            else:
                options = [
                    discord.SelectOption(
                        label=label,
                        value=value,
                        default=draft.availability_end == value,
                    )
                    for label, value in valid_times[:25]
                ]
                disabled = False

        super().__init__(
            placeholder="Select availability end time",
            min_values=1,
            max_values=1,
            options=options,
            row=3,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            await interaction.response.send_message(
                "Select a start time first.",
                ephemeral=True,
            )
            return

        self.draft.availability_end = self.values[0]

        assert self.view is not None
        await self.view.refresh(interaction)


class AdjustBackButton(discord.ui.Button):
    def __init__(self, draft: AvailabilityUpdateDraft):
        super().__init__(
            label="Back",
            style=discord.ButtonStyle.secondary,
            row=4,
        )
        self.draft = draft

    async def callback(self, interaction: discord.Interaction):
        existing = get_existing_pending_or_mia_request(str(interaction.user.id))

        if existing is None:
            await interaction.response.edit_message(
                content="Your request is no longer active.",
                embed=None,
                view=None,
            )
            return

        await interaction.response.edit_message(
            embed=build_existing_request_embed(),
            view=ExistingRequestView(interaction.user.id, existing),
        )


class AdjustSaveButton(discord.ui.Button):
    def __init__(self, draft: AvailabilityUpdateDraft):
        super().__init__(
            label="Save",
            style=discord.ButtonStyle.success,
            row=4,
        )
        self.draft = draft

    async def callback(self, interaction: discord.Interaction):
        errors = self.draft.validate()

        if errors:
            await interaction.response.send_message(
                "Cannot save yet:\n"
                + "\n".join(f"- {error}" for error in errors),
                ephemeral=True,
            )
            return

        try:
            update_existing_request_availability(self.draft)
        except Exception as error:
            await interaction.response.send_message(
                f"Failed to update availability: `{error}`",
                ephemeral=True,
            )
            return

        existing = get_existing_pending_or_mia_request(str(interaction.user.id))

        if existing is None:
            await interaction.response.edit_message(
                content="Availability updated, but your request is no longer active.",
                embed=None,
                view=None,
            )
            return

        await interaction.response.edit_message(
            content="Availability updated.",
            embed=build_existing_request_embed(),
            view=ExistingRequestView(interaction.user.id, existing),
        )


class RequestPageOneView(BaseRequestView):
    def __init__(self, owner_id: int, draft: QualificationRequestDraft):
        super().__init__(owner_id, draft)

        self.add_item(SetVTOLHoursButton(draft))
        self.add_item(AgeSelect(draft))
        self.add_item(PreferredAircraftSelect(draft))

        self.add_item(CancelButton(draft, row=4))
        self.add_item(PageOneNextButton(draft))

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=build_request_embed(self.draft, page=1),
            view=RequestPageOneView(self.owner_id, self.draft),
        )


class RequestPageTwoView(BaseRequestView):
    def __init__(self, owner_id: int, draft: QualificationRequestDraft):
        super().__init__(owner_id, draft)

        self.add_item(DaysOfWeekSelect(draft))
        self.add_item(TimezoneSelect(draft))
        self.add_item(StartTimeSelect(draft))
        self.add_item(EndTimeSelect(draft))

        self.add_item(PageTwoBackButton(draft))
        self.add_item(CancelButton(draft, row=4))
        self.add_item(PageTwoNextButton(draft))

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=build_request_embed(self.draft, page=2),
            view=RequestPageTwoView(self.owner_id, self.draft),
        )


class RequestPageThreeView(BaseRequestView):
    def __init__(self, owner_id: int, draft: QualificationRequestDraft):
        super().__init__(owner_id, draft)

        self.add_item(ReferralSelect(draft))

        self.add_item(PageThreeBackButton(draft))
        self.add_item(CancelButton(draft, row=4))
        self.add_item(SubmitRequestButton(draft))

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=build_request_embed(self.draft, page=3),
            view=RequestPageThreeView(self.owner_id, self.draft),
        )


class VTOLHoursModal(discord.ui.Modal, title="VTOL Hours"):
    def __init__(self, draft: QualificationRequestDraft):
        super().__init__()
        self.draft = draft

        self.hours_input = discord.ui.TextInput(
            label="VTOL VR Hours",
            placeholder="Example: 42.5",
            default=format_hours(draft.hours) if draft.hours is not None else "",
            max_length=12,
            required=True,
        )

        self.add_item(self.hours_input)

    async def on_submit(self, interaction: discord.Interaction):
        raw_hours = str(self.hours_input.value).strip().replace(",", "")

        try:
            hours = float(raw_hours)
        except ValueError:
            await interaction.response.send_message(
                "VTOL hours must be a number. Example: `42` or `42.5`.",
                ephemeral=True,
            )
            return

        if hours < 0:
            await interaction.response.send_message(
                "VTOL hours cannot be negative.",
                ephemeral=True,
            )
            return

        if hours > 10000:
            await interaction.response.send_message(
                "That number is too high. Please enter a realistic VTOL hour amount.",
                ephemeral=True,
            )
            return

        self.draft.hours = float(hours)

        # Do not store hours here. min_requirements is computed as Yes/No in the service.
        self.draft.min_requirements = None

        await interaction.response.edit_message(
            embed=build_request_embed(self.draft, page=1),
            view=RequestPageOneView(interaction.user.id, self.draft),
        )


class SetVTOLHoursButton(discord.ui.Button):
    def __init__(self, draft: QualificationRequestDraft):
        super().__init__(
            label="Set VTOL Hours",
            style=discord.ButtonStyle.primary,
            row=0,
        )
        self.draft = draft

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(VTOLHoursModal(self.draft))


class AgeSelect(discord.ui.Select):
    def __init__(self, draft: QualificationRequestDraft):
        self.draft = draft

        options = [
            discord.SelectOption(
                label="Yes",
                value="1",
                description="I am 13 years old or older.",
                default=draft.of_age is True,
            ),
            discord.SelectOption(
                label="No",
                value="0",
                description="I am younger than 13.",
                default=draft.of_age is False,
            ),
        ]

        super().__init__(
            placeholder="Age: 13 years or older?",
            min_values=1,
            max_values=1,
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        self.draft.of_age = self.values[0] == "1"
        self.draft.min_requirements = None

        assert self.view is not None
        await self.view.refresh(interaction)


class PreferredAircraftSelect(discord.ui.Select):
    def __init__(self, draft: QualificationRequestDraft):
        self.draft = draft
        aircraft_names = configured_aircraft_names()

        options = [
            discord.SelectOption(
                label=aircraft,
                value=aircraft,
                default=draft.preferred_aircraft == aircraft,
            )
            for aircraft in aircraft_names[:25]
        ]

        if not options:
            options = [
                discord.SelectOption(
                    label="No aircraft configured",
                    value="none",
                    default=True,
                )
            ]

        super().__init__(
            placeholder="Preferred aircraft",
            min_values=1,
            max_values=1,
            options=options,
            row=2,
            disabled=not aircraft_names,
        )

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            await interaction.response.send_message(
                "No aircraft are configured in config.py.",
                ephemeral=True,
            )
            return

        self.draft.preferred_aircraft = self.values[0]

        assert self.view is not None
        await self.view.refresh(interaction)


class PageOneNextButton(discord.ui.Button):
    def __init__(self, draft: QualificationRequestDraft):
        super().__init__(
            label="Next",
            style=discord.ButtonStyle.success,
            row=4,
        )
        self.draft = draft

    async def callback(self, interaction: discord.Interaction):
        errors = self.draft.validate_page_one()

        if errors:
            await interaction.response.send_message(
                "Please finish page 1 before continuing:\n"
                + "\n".join(f"- {error}" for error in errors),
                ephemeral=True,
            )
            return

        await interaction.response.edit_message(
            embed=build_request_embed(self.draft, page=2),
            view=RequestPageTwoView(interaction.user.id, self.draft),
        )


class DaysOfWeekSelect(discord.ui.Select):
    def __init__(self, draft: QualificationRequestDraft):
        self.draft = draft

        selected_days = set()

        if draft.dotw:
            selected_days = {
                day.strip()
                for day in draft.dotw.split(",")
                if day.strip()
            }

        options = [
            discord.SelectOption(
                label=label,
                value=value,
                default=value in selected_days,
            )
            for label, value in DAYS_OF_WEEK
        ]

        super().__init__(
            placeholder="Select days you are usually available",
            min_values=1,
            max_values=7,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        self.draft.dotw = ", ".join(self.values)

        assert self.view is not None
        await self.view.refresh(interaction)


class TimezoneSelect(discord.ui.Select):
    def __init__(self, draft: QualificationRequestDraft):
        self.draft = draft

        options = [
            discord.SelectOption(
                label=label,
                value=value,
                description=value,
                default=draft.timezone == value,
            )
            for label, value in TIMEZONE_OPTIONS[:25]
        ]

        if not options:
            options = [
                discord.SelectOption(
                    label="No timezones configured",
                    value="none",
                    default=True,
                )
            ]

        super().__init__(
            placeholder="Select timezone",
            min_values=1,
            max_values=1,
            options=options,
            row=1,
            disabled=not TIMEZONE_OPTIONS,
        )

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            await interaction.response.send_message(
                "No timezones are configured in config.py.",
                ephemeral=True,
            )
            return

        self.draft.timezone = self.values[0]

        assert self.view is not None
        await self.view.refresh(interaction)


class StartTimeSelect(discord.ui.Select):
    def __init__(self, draft: QualificationRequestDraft):
        self.draft = draft

        options = [
            discord.SelectOption(
                label=label,
                value=value,
                default=draft.availability_start == value,
            )
            for label, value in TIME_OPTIONS[:25]
        ]

        if not options:
            options = [
                discord.SelectOption(
                    label="No times configured",
                    value="none",
                    default=True,
                )
            ]

        super().__init__(
            placeholder="Select availability start time",
            min_values=1,
            max_values=1,
            options=options,
            row=2,
            disabled=not TIME_OPTIONS,
        )

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            await interaction.response.send_message(
                "No times are configured in config.py.",
                ephemeral=True,
            )
            return

        self.draft.availability_start = self.values[0]

        start_index = time_index(self.draft.availability_start)
        end_index = time_index(self.draft.availability_end)

        if (
            start_index is not None
            and end_index is not None
            and end_index <= start_index
        ):
            self.draft.availability_end = None

        assert self.view is not None
        await self.view.refresh(interaction)


class EndTimeSelect(discord.ui.Select):
    def __init__(self, draft: QualificationRequestDraft):
        self.draft = draft

        start_index = time_index(draft.availability_start)

        if start_index is None:
            options = [
                discord.SelectOption(
                    label="Select start time first",
                    value="none",
                    default=True,
                )
            ]
            disabled = True
        else:
            valid_times = TIME_OPTIONS[start_index + 1:]

            if not valid_times:
                options = [
                    discord.SelectOption(
                        label="No later times available",
                        value="none",
                        default=True,
                    )
                ]
                disabled = True
            else:
                options = [
                    discord.SelectOption(
                        label=label,
                        value=value,
                        default=draft.availability_end == value,
                    )
                    for label, value in valid_times[:25]
                ]
                disabled = False

        super().__init__(
            placeholder="Select availability end time",
            min_values=1,
            max_values=1,
            options=options,
            row=3,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            await interaction.response.send_message(
                "Select a start time first.",
                ephemeral=True,
            )
            return

        self.draft.availability_end = self.values[0]

        assert self.view is not None
        await self.view.refresh(interaction)


class PageTwoBackButton(discord.ui.Button):
    def __init__(self, draft: QualificationRequestDraft):
        super().__init__(
            label="Back",
            style=discord.ButtonStyle.secondary,
            row=4,
        )
        self.draft = draft

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=build_request_embed(self.draft, page=1),
            view=RequestPageOneView(interaction.user.id, self.draft),
        )


class PageTwoNextButton(discord.ui.Button):
    def __init__(self, draft: QualificationRequestDraft):
        super().__init__(
            label="Next",
            style=discord.ButtonStyle.success,
            row=4,
        )
        self.draft = draft

    async def callback(self, interaction: discord.Interaction):
        errors = self.draft.validate_page_two()

        if errors:
            await interaction.response.send_message(
                "Please finish page 2 before continuing:\n"
                + "\n".join(f"- {error}" for error in errors),
                ephemeral=True,
            )
            return

        await interaction.response.edit_message(
            embed=build_request_embed(self.draft, page=3),
            view=RequestPageThreeView(interaction.user.id, self.draft),
        )


class ReferralSelect(discord.ui.Select):
    def __init__(self, draft: QualificationRequestDraft):
        self.draft = draft

        options = [
            discord.SelectOption(
                label=referral,
                value=referral,
                default=draft.referral == referral,
            )
            for referral in REFERRAL_OPTIONS[:25]
        ]

        if not options:
            options = [
                discord.SelectOption(
                    label="No referral options configured",
                    value="none",
                    default=True,
                )
            ]

        super().__init__(
            placeholder="How did you hear about us?",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
            disabled=not REFERRAL_OPTIONS,
        )

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            await interaction.response.send_message(
                "No referral options are configured in config.py.",
                ephemeral=True,
            )
            return

        self.draft.referral = self.values[0]

        assert self.view is not None
        await self.view.refresh(interaction)


class PageThreeBackButton(discord.ui.Button):
    def __init__(self, draft: QualificationRequestDraft):
        super().__init__(
            label="Back",
            style=discord.ButtonStyle.secondary,
            row=4,
        )
        self.draft = draft

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=build_request_embed(self.draft, page=2),
            view=RequestPageTwoView(interaction.user.id, self.draft),
        )


class SubmitRequestButton(discord.ui.Button):
    def __init__(self, draft: QualificationRequestDraft):
        errors = draft.validate()

        super().__init__(
            label="Submit",
            style=discord.ButtonStyle.success,
            disabled=bool(errors),
            row=4,
        )
        self.draft = draft

    async def callback(self, interaction: discord.Interaction):
        errors = self.draft.validate()

        if errors:
            await interaction.response.send_message(
                "Cannot submit yet:\n"
                + "\n".join(f"- {error}" for error in errors),
                ephemeral=True,
            )
            return

        try:
            request_id = create_request_qual_in_db(self.draft)
        except Exception as error:
            await interaction.response.send_message(
                f"Failed to submit request: `{error}`",
                ephemeral=True,
            )
            return

        active_qualification_requests.pop(interaction.user.id, None)

        embed = discord.Embed(
            title="Qualification Application Submitted",
            description=(
                "Your qualification application has been submitted.\n\n"
                "Thank you for wanting to join our weekend ops. "
                "An instructor will review your request when available."
            ),
        )

        submitted_request = ExistingQualificationRequest(
            id=request_id,
            discord_id=str(interaction.user.id),
            discord_username=str(interaction.user.name),
            status="pending",
            timezone=self.draft.timezone,
            availability_start=self.draft.availability_start,
            availability_end=self.draft.availability_end,
            dotw=self.draft.dotw,
            times_pinged=0,
            created_at=0,
            updated_at=0,
        )

        await interaction.response.edit_message(
            embed=embed,
            view=SubmittedRequestView(interaction.user.id, submitted_request),
        )

        asyncio.create_task(
            notify_instructors_of_new_qual_request(
                interaction,
                request_id=request_id,
                draft=self.draft,
            )
        )


class CancelButton(discord.ui.Button):
    def __init__(self, draft: QualificationRequestDraft, row: int = 4):
        super().__init__(
            label="Cancel Application",
            style=discord.ButtonStyle.danger,
            row=row,
        )
        self.draft = draft

    async def callback(self, interaction: discord.Interaction):
        active_qualification_requests.pop(interaction.user.id, None)

        await interaction.response.edit_message(
            content="Qualification application cancelled.",
            embed=None,
            view=None,
        )



async def start_request_qualification_wizard(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member):
        await send_qualification_response(
            interaction,
            content="This command can only be used inside the server.",
            ephemeral=True,
        )
        return

    if member_has_role(interaction.user, MISSION_QUALIFIED_ROLE):
        await send_qualification_response(
            interaction,
            content=(
                "You are already Mission Qualified, so you do not need to submit "
                "a qualification application."
            ),
            ephemeral=True,
        )
        return

    ensure_user_exists(interaction.user)

    existing = get_existing_pending_or_mia_request(str(interaction.user.id))

    if existing is not None:
        updated_existing = bump_or_reopen_existing_request(existing.id)

        if updated_existing is None:
            await send_qualification_response(
                interaction,
                content="I could not update your existing request. Please try again.",
                ephemeral=True,
            )
            return

        await send_qualification_response(
            interaction,
            embed=build_existing_request_embed(),
            view=ExistingRequestView(interaction.user.id, updated_existing),
            ephemeral=True,
        )
        return

    draft = QualificationRequestDraft(
        discord_id=str(interaction.user.id),
        discord_username=str(interaction.user.name),
    )

    active_qualification_requests[interaction.user.id] = draft

    await send_qualification_response(
        interaction,
        embed=build_request_embed(draft, page=1),
        view=RequestPageOneView(interaction.user.id, draft),
        ephemeral=True,
    )


class GetQualifiedCommands(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    get_group = app_commands.Group(
        name="get",
        description="Get commands",
    )

    @get_group.command(
        name="qualified",
        description="Submit a qualification application.",
    )
    @app_commands.guild_only()
    async def get_qualified_command(
        self,
        interaction: discord.Interaction,
    ):
        await start_request_qualification_wizard(interaction)


async def setup(bot: commands.Bot):
    await bot.add_cog(GetQualifiedCommands(bot))
