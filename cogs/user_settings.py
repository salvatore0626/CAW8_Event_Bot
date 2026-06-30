import discord
from discord import app_commands
from discord.ext import commands

from config import TIMEZONE_OPTIONS, INSTRUCTOR_ROLE
from services.user_settings_service import (
    get_user_settings,
    update_timezone,
    update_notification_window,
    update_notification_toggles,
)



ANSI_RESET = "\u001b[0m"
ANSI_WHITE = "\u001b[37m"
ANSI_GREEN_BOLD = "\u001b[1;32m"
ANSI_RED_BOLD = "\u001b[1;31m"


def member_has_instructor_role(member: discord.Member) -> bool:
    try:
        role_id = int(INSTRUCTOR_ROLE or 0)
    except (TypeError, ValueError):
        return False

    if role_id <= 0:
        return False

    return any(int(role.id) == role_id for role in member.roles)


def settings_for_member(member: discord.Member):
    settings = get_user_settings(member)

    if not member_has_instructor_role(member) and settings.notify_instructor:
        update_notification_toggles(
            str(member.id),
            notify_flightlead=settings.notify_flightlead,
            notify_instructor=False,
            notify_training=settings.notify_training,
        )
        settings = get_user_settings(member)

    return settings


def ansi_on_off(value: bool) -> str:
    if value:
        return f"{ANSI_GREEN_BOLD}On{ANSI_RESET}{ANSI_WHITE}"

    return f"{ANSI_RED_BOLD}Off{ANSI_RESET}{ANSI_WHITE}"


def notification_status_code_block(member: discord.Member, settings) -> str:
    lines = [
        f"{ANSI_WHITE}Flight lead reminder: {ansi_on_off(settings.notify_flightlead)}",
        f"{ANSI_WHITE}Training Notification: {ansi_on_off(settings.notify_training)}",
    ]

    if member_has_instructor_role(member):
        lines.append(
            f"{ANSI_WHITE}Instructor Request: {ansi_on_off(settings.notify_instructor)}"
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

    embed.set_footer(
        text=f"Settings for {member.display_name}"
    )

    return embed


class RestrictedSettingsView(discord.ui.View):
    def __init__(self, member: discord.Member):
        super().__init__(timeout=900)

        self.member = member
        self.discord_id = str(member.id)

        settings = settings_for_member(member)

        self.add_item(TimezoneSelect(member, settings))
        self.add_item(NotificationStartSelect(member, settings))
        self.add_item(NotificationEndSelect(member, settings))
        self.add_item(NotificationToggleSelect(member, settings))
        self.add_item(DoneButton(disabled=not settings_ready(settings)))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.member.id:
            await interaction.response.send_message(
                "Only the user who opened this settings menu can use it.",
                ephemeral=True,
            )
            return False

        return True

    async def refresh(self, interaction: discord.Interaction):
        settings = settings_for_member(self.member)
        new_view = RestrictedSettingsView(self.member)

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

        options = []

        for label, value in TIMEZONE_OPTIONS[:25]:
            options.append(
                discord.SelectOption(
                    label=label,
                    value=value,
                    description=value,
                    default=settings.timezone == value,
                )
            )

        super().__init__(
            placeholder="Select timezone",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        update_timezone(str(self.member.id), self.values[0])

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
                label="Flight Lead",
                value="flightlead",
                description="Notify me about flight lead reminders.",
                default=settings.notify_flightlead is True,
            ),
            discord.SelectOption(
                label="Training",
                value="training",
                description="Notify me about training alerts.",
                default=settings.notify_training is True,
            ),
        ]

        if member_has_instructor_role(member):
            options.append(
                discord.SelectOption(
                    label="Instructor",
                    value="instructor",
                    description="Notify me about instructor/qualification requests.",
                    default=settings.notify_instructor is True,
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
        allow_instructor_notifications = member_has_instructor_role(self.member)

        update_notification_toggles(
            str(self.member.id),
            notify_flightlead="flightlead" in selected,
            notify_instructor=allow_instructor_notifications and "instructor" in selected,
            notify_training="training" in selected,
        )

        assert self.view is not None
        await self.view.refresh(interaction)


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
        description="Update your Air Boss user settings.",
    )
    @app_commands.guild_only()
    async def user_settings_command(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This command can only be used inside the server.",
                ephemeral=True,
            )
            return

        member = interaction.user
        settings = settings_for_member(member)

        missing = missing_required_settings(settings)
        content = None

        if missing:
            content = "Before these settings can be saved, please complete the required setup below."

        await interaction.response.send_message(
            content=content,
            embed=build_settings_embed(member, settings),
            view=RestrictedSettingsView(member),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(UserSettingsCommands(bot))
