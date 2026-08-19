import discord
from zoneinfo import available_timezones
from discord import app_commands
from discord.ext import commands

from config import TIMEZONE_OPTIONS
from services.permission_service import (
    member_can_receive_operation_reminders,
    member_can_receive_weekly_report,
    member_is_instructor,
)
from services.weekly_report_service import send_weekly_report_dm
from services.user_settings_service import (
    get_user_settings,
    update_timezone,
    normalize_timezone_name,
    update_notification_window,
    update_notification_toggles,
)



ANSI_RESET = "\u001b[0m"
ANSI_WHITE = "\u001b[37m"
ANSI_GREEN_BOLD = "\u001b[1;32m"
ANSI_RED_BOLD = "\u001b[1;31m"
ANSI_BLUE_BOLD = "\u001b[1;34m"


ALL_IANA_TIMEZONES = tuple(sorted(available_timezones()))
CONFIGURED_TIMEZONE_VALUES = {value for _, value in TIMEZONE_OPTIONS}


def timezone_select_options(current_timezone: str | None) -> list[discord.SelectOption]:
    """Build the quick-pick list while reserving room for an unlisted current value."""
    options: list[discord.SelectOption] = []
    current = normalize_timezone_name(current_timezone)
    current_is_unlisted = bool(current and current not in CONFIGURED_TIMEZONE_VALUES)
    configured_limit = 24 if current_is_unlisted else 25

    if current_is_unlisted and current is not None:
        options.append(
            discord.SelectOption(
                label=f"Current: {current}"[:100],
                value=current,
                description="Saved timezone (not in quick-pick list)",
                default=True,
            )
        )

    for label, value in TIMEZONE_OPTIONS[:configured_limit]:
        options.append(
            discord.SelectOption(
                label=label[:100],
                value=value,
                description=value[:100],
                default=current == value,
            )
        )

    return options[:25]


def timezone_autocomplete_matches(query: str) -> list[str]:
    text = str(query or "").strip().casefold()

    if not text:
        configured = [value for _, value in TIMEZONE_OPTIONS]
        remaining = [zone for zone in ALL_IANA_TIMEZONES if zone not in configured]
        return (configured + remaining)[:25]

    prefix_matches = [zone for zone in ALL_IANA_TIMEZONES if zone.casefold().startswith(text)]
    contains_matches = [
        zone
        for zone in ALL_IANA_TIMEZONES
        if text in zone.casefold() and zone not in prefix_matches
    ]
    return (prefix_matches + contains_matches)[:25]


async def timezone_autocomplete_choices(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    del interaction
    return [
        app_commands.Choice(name=zone, value=zone)
        for zone in timezone_autocomplete_matches(current)
    ]


def settings_for_member(member: discord.Member):
    settings = get_user_settings(member)

    allow_instructor = member_is_instructor(member)
    allow_weekly_report = member_can_receive_weekly_report(member)
    allow_operations = member_can_receive_operation_reminders(member)

    # Keep role-gated notification flags clean if a member later loses the role.
    if (not allow_instructor and settings.notify_instructor) or (
        not allow_weekly_report and settings.notify_weekly_report
    ) or (not allow_operations and settings.notify_operations):
        update_notification_toggles(
            str(member.id),
            notify_flightlead=settings.notify_flightlead,
            notify_instructor=allow_instructor and settings.notify_instructor,
            notify_training=settings.notify_training,
            notify_weekly_report=(
                settings.notify_weekly_report if allow_weekly_report else False
            ),
            notify_operations=(
                settings.notify_operations if allow_operations else False
            ),
        )
        settings = get_user_settings(member)

    return settings


def ansi_on_off(value: bool) -> str:
    if value:
        return f"{ANSI_GREEN_BOLD}On{ANSI_RESET}{ANSI_WHITE}"

    return f"{ANSI_RED_BOLD}Off{ANSI_RESET}{ANSI_WHITE}"

def ansi_on_off_blue(value: bool) -> str:
    if value:
        return f"{ANSI_BLUE_BOLD}On{ANSI_RESET}{ANSI_WHITE}"

    return f"{ANSI_RED_BOLD}Off{ANSI_RESET}{ANSI_WHITE}"


def notification_status_code_block(member: discord.Member, settings) -> str:
    lines = [
        f"{ANSI_WHITE}Flight Lead Reservations: {ansi_on_off(settings.notify_flightlead)}",
        f"{ANSI_WHITE}Training Alerts: {ansi_on_off(settings.notify_training)}",
    ]


    if member_is_instructor(member):
            lines.append(
                f"{ANSI_WHITE}Qualification Requests: {ansi_on_off(settings.notify_instructor)}"
            )

    if member_can_receive_operation_reminders(member):
        lines.append(
            f"{ANSI_WHITE}OP Execution Reminders: {ansi_on_off(settings.notify_operations)}"
        )

    if member_can_receive_weekly_report(member):
        lines.append(
            f"{ANSI_WHITE}Weekly Server Report: {ansi_on_off_blue(settings.notify_weekly_report)}"
        )

    return "```ansi\n" + "\n".join(lines) + f"\n{ANSI_RESET}```"


def bool_text(value: bool) -> str:
    return "On" if value else "Off"


def format_time(value: str | None) -> str:
    text = str(value or "").strip()

    try:
        hour_text, minute_text = text.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except (TypeError, ValueError):
        return text or "Not set"

    suffix = "AM" if hour < 12 else "PM"
    hour_12 = hour % 12

    if hour_12 == 0:
        hour_12 = 12

    return f"{hour_12}:{minute:02d} {suffix}"


def time_options(settings_value: str):
    options = []

    for hour in range(24):
        value = f"{hour:02d}:00"
        options.append(
            discord.SelectOption(
                label=format_time(value),
                value=value,
                description=value,
                default=settings_value == value,
            )
        )

    return options


def notification_window_text(settings) -> str:
    return f"{format_time(settings.notify_start)} - {format_time(settings.notify_end)}"


def missing_required_settings(settings) -> list[str]:
    missing: list[str] = []

    if not settings.timezone:
        missing.append("Timezone")

    if not settings.notify_start:
        missing.append("Notification start time")

    if not settings.notify_end:
        missing.append("Notification end time")

    return missing


def settings_ready(settings) -> bool:
    return not missing_required_settings(settings)


def build_settings_embed(
    member: discord.Member,
    settings,
) -> discord.Embed:
    embed = discord.Embed(
        title="User Settings",
        description="Use the dropdowns below to update your Air Boss notification settings.",
    )

    embed.add_field(
        name="Timezone",
        value=settings.timezone or "Not set",
        inline=False,
    )

    embed.add_field(
        name="Notification Window",
        value=notification_window_text(settings),
        inline=False,
    )

    missing = missing_required_settings(settings)

    if missing:
        embed.add_field(
            name="Required Setup",
            value=(
                "You need to set these before this menu can be marked saved:\n"
                + "\n".join(f"- {item}" for item in missing)
            ),
            inline=False,
        )

    embed.add_field(
        name="Notifications",
        value=notification_status_code_block(member, settings),
        inline=False,
    )

    footer_text = f"Settings for {member.display_name}"
    if member_can_receive_weekly_report(member):
        footer_text += (
            " | Notifications will only be sent during your notification window."
        )

    embed.set_footer(text=footer_text)

    return embed


class RestrictedSettingsView(discord.ui.View):
    def __init__(
        self,
        member: discord.Member,
        *,
        owner_id: int,
    ):
        super().__init__(timeout=900)

        self.member = member
        self.discord_id = str(member.id)
        self.owner_id = int(owner_id)

        settings = settings_for_member(member)

        self.add_item(TimezoneSelect(member, settings))
        self.add_item(NotificationStartSelect(member, settings))
        self.add_item(NotificationEndSelect(member, settings))
        self.add_item(NotificationToggleSelect(member, settings))
        self.add_item(DoneButton(disabled=not settings_ready(settings)))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the user who opened this settings menu can use it.",
                ephemeral=True,
            )
            return False

        return True

    async def refresh(self, interaction: discord.Interaction):
        settings = settings_for_member(self.member)
        new_view = RestrictedSettingsView(
            self.member,
            owner_id=self.owner_id,
        )

        await interaction.response.edit_message(
            embed=build_settings_embed(
                self.member,
                settings,
            ),
            view=new_view,
        )


class TimezoneSelect(discord.ui.Select):
    def __init__(self, member: discord.Member, settings):
        self.member = member

        options = timezone_select_options(settings.timezone)

        super().__init__(
            placeholder="Select timezone",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        if not update_timezone(str(self.member.id), self.values[0]):
            await interaction.response.send_message(
                "That timezone is not a valid IANA timezone.",
                ephemeral=True,
            )
            return

        assert self.view is not None
        await self.view.refresh(interaction)


class NotificationStartSelect(discord.ui.Select):
    def __init__(self, member: discord.Member, settings):
        self.member = member

        super().__init__(
            placeholder="Notification start time",
            min_values=1,
            max_values=1,
            options=time_options(settings.notify_start),
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        update_notification_window(
            str(self.member.id),
            notify_start=self.values[0],
        )

        assert self.view is not None
        await self.view.refresh(interaction)


class NotificationEndSelect(discord.ui.Select):
    def __init__(self, member: discord.Member, settings):
        self.member = member

        super().__init__(
            placeholder="Notification end time",
            min_values=1,
            max_values=1,
            options=time_options(settings.notify_end),
            row=2,
        )

    async def callback(self, interaction: discord.Interaction):
        update_notification_window(
            str(self.member.id),
            notify_end=self.values[0],
        )

        assert self.view is not None
        await self.view.refresh(interaction)


class NotificationToggleSelect(discord.ui.Select):
    def __init__(self, member: discord.Member, settings):
        self.member = member

        options = [
            discord.SelectOption(
                label="Flight Lead Reservations",
                value="flightlead",
                description="Notify me about flight lead reservations.",
                default=settings.notify_flightlead is True,
            ),
            discord.SelectOption(
                label="Training Alerts",
                value="training",
                description="Notify me about mass training events.",
                default=settings.notify_training is True,
            ),
        ]

        if member_is_instructor(member):
            options.append(
                discord.SelectOption(
                    label="Qualification Requests",
                    value="instructor",
                    description="Notify me when new qualification requests are received.",
                    default=settings.notify_instructor is True,
                )
            )

        if member_can_receive_operation_reminders(member):
            options.append(
                discord.SelectOption(
                    label="OP Execution Reminders",
                    value="operations",
                    description="Remind me when ops need to be completed or canceled.",
                    default=settings.notify_operations is True,
                )
            )

        if member_can_receive_weekly_report(member):
            options.append(
                discord.SelectOption(
                    label="Weekly Server Report",
                    value="weekly_report",
                    description="Weekly CAW-8 server report and trends.",
                    default=settings.notify_weekly_report is True,
                )
            )

        super().__init__(
            placeholder="Notification types",
            min_values=0,
            max_values=len(options),
            options=options,
            row=3,
        )

    async def callback(self, interaction: discord.Interaction):
        selected = set(self.values)
        allow_instructor_notifications = member_is_instructor(self.member)
        before = settings_for_member(self.member)

        weekly_value = None
        weekly_eligible = member_can_receive_weekly_report(self.member)
        if weekly_eligible:
            weekly_value = "weekly_report" in selected

        operations_value = None
        operations_eligible = member_can_receive_operation_reminders(self.member)
        if operations_eligible:
            operations_value = "operations" in selected

        update_notification_toggles(
            str(self.member.id),
            notify_flightlead="flightlead" in selected,
            notify_instructor=allow_instructor_notifications and "instructor" in selected,
            notify_training="training" in selected,
            notify_weekly_report=weekly_value,
            notify_operations=operations_value,
        )

        assert self.view is not None
        await self.view.refresh(interaction)

        # Enabling the role-gated report sends the latest cached report
        # immediately so the user can see exactly what future weekly DMs
        # will look like. Notification-window settings are intentionally ignored.
        if (
            weekly_eligible
            and not before.notify_weekly_report
            and weekly_value is True
        ):
            sent = await send_weekly_report_dm(self.member)
            if sent:
                await interaction.followup.send(
                    "✅ Weekly Server Report enabled. "
                    "The latest report was sent to you by DM.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    "⚠️ Weekly Server Report was enabled, "
                    "but the preview DM could not be delivered.",
                    ephemeral=True,
                )


class DoneButton(discord.ui.Button):
    def __init__(self, disabled: bool = False):
        super().__init__(
            label="Done",
            style=discord.ButtonStyle.success,
            row=4,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction):
        if not isinstance(self.view, RestrictedSettingsView):
            await interaction.response.send_message(
                "Could not read this settings menu.",
                ephemeral=True,
            )
            return

        settings = settings_for_member(self.view.member)
        missing = missing_required_settings(settings)

        if missing:
            await interaction.response.send_message(
                "You still need to set:\n"
                + "\n".join(f"- {item}" for item in missing),
                ephemeral=True,
            )
            return

        await interaction.response.edit_message(
            content="✅ Settings saved.",
            embed=None,
            view=None,
        )


class UserSettingsCommands(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    user_group = app_commands.Group(
        name="user",
        description="User commands",
    )

    @user_group.command(
        name="settings",
        description="Update Air Boss user settings or set any IANA timezone.",
    )
    @app_commands.describe(
        timezone="Optional IANA timezone, such as America/Moncton or Europe/London.",
    )
    @app_commands.autocomplete(timezone=timezone_autocomplete_choices)
    @app_commands.guild_only()
    async def user_settings_command(
        self,
        interaction: discord.Interaction,
        timezone: str | None = None,
    ):
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This command can only be used inside the server.",
                ephemeral=True,
            )
            return

        # Acknowledge the slash command before touching SQLite/building the UI.
        # This prevents Discord's 3-second interaction timeout on a busy DB.
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            member = interaction.user
            settings = settings_for_member(member)

            if timezone is not None:
                normalized = normalize_timezone_name(timezone)

                if normalized is None:
                    await interaction.edit_original_response(
                        content=(
                            "That is not a valid IANA timezone. Choose one from autocomplete, "
                            "such as `America/Moncton`."
                        ),
                        embed=None,
                        view=None,
                    )
                    return

                if not update_timezone(str(member.id), normalized):
                    await interaction.edit_original_response(
                        content="That timezone could not be saved.",
                        embed=None,
                        view=None,
                    )
                    return

                await interaction.edit_original_response(
                    content=f"✅ Your timezone is now `{normalized}`.",
                    embed=None,
                    view=None,
                )
                return

            missing = missing_required_settings(settings)
            content = None

            if missing:
                content = (
                    "Before these settings can be saved, please complete the required setup below."
                )

            await interaction.edit_original_response(
                content=content,
                embed=build_settings_embed(member, settings),
                view=RestrictedSettingsView(
                    member,
                    owner_id=member.id,
                ),
            )

        except Exception as error:
            import traceback

            traceback.print_exc()
            try:
                await interaction.edit_original_response(
                    content=(
                        "⚠️ `/user settings` failed: "
                        f"`{type(error).__name__}: {error}`"
                    ),
                    embed=None,
                    view=None,
                )
            except Exception:
                traceback.print_exc()



async def setup(bot: commands.Bot):
    await bot.add_cog(UserSettingsCommands(bot))
