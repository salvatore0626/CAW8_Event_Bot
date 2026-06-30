from __future__ import annotations

import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import commands

try:
    from config import TRAINING_DM_COOLDOWN_MINUTES
except ImportError:
    TRAINING_DM_COOLDOWN_MINUTES = 15

try:
    from config import TRAINING_ROSTER_VOICE_CATEGORY_IDS
except ImportError:
    TRAINING_ROSTER_VOICE_CATEGORY_IDS = []


training_dm_cooldowns: dict[int, float] = {}
training_dm_sends_in_progress: set[tuple[int, int, str, int, str]] = set()


def training_dm_cooldown_seconds() -> int:
    try:
        minutes = int(TRAINING_DM_COOLDOWN_MINUTES)
    except (TypeError, ValueError):
        minutes = 15

    return max(0, minutes * 60)


def training_dm_cooldown_remaining(user_id: int) -> int:
    expires_at = training_dm_cooldowns.get(int(user_id), 0.0)
    remaining = int(expires_at - time.time())

    return max(0, remaining)


def set_training_dm_cooldown(user_id: int) -> None:
    cooldown_seconds = training_dm_cooldown_seconds()

    if cooldown_seconds <= 0:
        return

    training_dm_cooldowns[int(user_id)] = time.time() + cooldown_seconds


def format_training_dm_cooldown(seconds: int) -> str:
    seconds = max(0, int(seconds))

    if seconds < 60:
        return f"{seconds} seconds"

    minutes = seconds // 60
    remainder = seconds % 60

    if remainder:
        minutes += 1

    if minutes == 1:
        return "1 minute"

    return f"{minutes} minutes"


def confirm_send_key(
    *,
    interaction: discord.Interaction,
    view: "ConfirmTrainingDMView",
) -> tuple[int, int, str, int, str]:
    message_id = int(interaction.message.id) if interaction.message else 0

    return (
        message_id,
        int(view.owner_id),
        str(view.selected_topic),
        int(view.selected_voice_channel_id),
        str(view.selected_start),
    )

from services.permission_service import (
    member_is_admin,
    member_has_any_role,
    instructor_role_ids,
)
from services.training_interest_service import (
    TrainingInterestMember,
    ansi_code_block,
    configured_training_topics,
    eligible_training_dm_members,
    ensure_member,
    get_user_training_notify,
    interested_members_for_topic,
    roster_count_lines,
    set_user_training_interests,
    signup_status_lines,
    topic_label,
    training_interest_counts,
    update_user_training_notifications,
    user_training_interest_keys,
)


TRAINING_START_OPTIONS = [
    ("Now", "now", "starting now"),
    ("In 15 minutes", "15", "in 15 minutes"),
    ("In 30 minutes", "30", "in 30 minutes"),
    ("In 1 hour", "60", "in 1 hour"),
    ("In 2 hours", "120", "in 2 hours"),
]



TRAINING_ANSI_RESET = "\u001b[0m"
TRAINING_ANSI_WHITE = "\u001b[37m"
TRAINING_ANSI_GREEN_BOLD = "\u001b[1;32m"
TRAINING_ANSI_RED_BOLD = "\u001b[1;31m"


def training_notify_code_block(enabled: bool) -> str:
    status = (
        f"{TRAINING_ANSI_GREEN_BOLD}ON{TRAINING_ANSI_RESET}"
        if enabled
        else f"{TRAINING_ANSI_RED_BOLD}OFF{TRAINING_ANSI_RESET}"
    )

    return (
        "```ansi\n"
        f"{TRAINING_ANSI_WHITE}Training DM Notifications: {status}\n"
        "```"
    )

def bool_text(value: bool) -> str:
    return "On" if value else "Off"


def start_text(value: str | None) -> str:
    value = str(value or "now")

    for label, option_value, dm_text in TRAINING_START_OPTIONS:
        if value == option_value:
            return dm_text

    return "soon"


def start_label(value: str | None) -> str:
    value = str(value or "now")

    for label, option_value, _ in TRAINING_START_OPTIONS:
        if value == option_value:
            return label

    return "Now"

def start_minutes(value: str | None) -> int | None:
    value = str(value or "now")

    if value == "now":
        return 0

    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def local_time_label(timezone: str | None, start_value: str | None) -> str | None:
    if not timezone:
        return None

    minutes = start_minutes(start_value)

    if minutes is None:
        return None

    try:
        local_dt = datetime.now(ZoneInfo(str(timezone))) + timedelta(minutes=minutes)
    except ZoneInfoNotFoundError:
        return None
    except Exception:
        return None

    hour = local_dt.hour % 12

    if hour == 0:
        hour = 12

    suffix = "am" if local_dt.hour < 12 else "pm"

    return f"{hour}:{local_dt.minute:02d}{suffix}"


def training_start_display(start_value: str | None, timezone: str | None) -> str:
    relative = start_text(start_value)
    local_label = local_time_label(timezone, start_value)

    if local_label:
        return f"{relative} ({local_label})"

    return relative


def training_topic_options(selected_keys: set[str] | None = None) -> list[discord.SelectOption]:
    selected_keys = selected_keys or set()
    options: list[discord.SelectOption] = []

    for topic in configured_training_topics()[:25]:
        options.append(
            discord.SelectOption(
                label=topic.label,
                value=topic.key,
                default=topic.key in selected_keys,
            )
        )

    return options


def training_notify_options(current_value: bool) -> list[discord.SelectOption]:
    return [
        discord.SelectOption(
            label="Training DMs On",
            value="1",
            description="Allow training notifications within my user-settings window.",
            default=current_value is True,
        ),
        discord.SelectOption(
            label="Training DMs Off",
            value="0",
            description="Do not DM me for training alerts.",
            default=current_value is False,
        ),
    ]


def signup_dirty(view: "TrainingSignupView") -> bool:
    return view.draft_topics != view.saved_topics or view.draft_notify != view.saved_notify


def build_signup_embed(member: discord.Member, view: "TrainingSignupView") -> discord.Embed:
    embed = discord.Embed(
        title="Training Sign-ups",
        description=ansi_code_block(signup_status_lines(view.draft_topics)),
    )

    embed.add_field(
        name="Training DM Notifications",
        value=training_notify_code_block(view.draft_notify),
        inline=False,
    )

    if signup_dirty(view):
        embed.add_field(
            name="Unsaved Changes",
            value="Press **Submit** to save your training sign-ups.",
            inline=False,
        )

    embed.set_footer(
        text="Use /user settings to update notification preferences."
    )

    return embed


class TrainingSignupView(discord.ui.View):
    def __init__(
        self,
        *,
        member: discord.Member,
        saved_topics: set[str],
        saved_notify: bool,
        draft_topics: set[str] | None = None,
        draft_notify: bool | None = None,
    ):
        super().__init__(timeout=900)
        self.member = member
        self.saved_topics = set(saved_topics)
        self.saved_notify = bool(saved_notify)
        self.draft_topics = set(draft_topics) if draft_topics is not None else set(saved_topics)
        self.draft_notify = bool(saved_notify) if draft_notify is None else bool(draft_notify)

        self.add_item(TrainingNotifySelect(self))
        self.add_item(TrainingTopicSelect(self))
        self.add_item(ExitSignupButton(self))
        self.add_item(SubmitSignupButton(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.member.id:
            await interaction.response.send_message(
                "Only the user who opened this training signup menu can use it.",
                ephemeral=True,
            )
            return False

        return True

    async def refresh(self, interaction: discord.Interaction):
        new_view = TrainingSignupView(
            member=self.member,
            saved_topics=self.saved_topics,
            saved_notify=self.saved_notify,
            draft_topics=self.draft_topics,
            draft_notify=self.draft_notify,
        )

        await interaction.response.edit_message(
            embed=build_signup_embed(self.member, new_view),
            view=new_view,
        )


class TrainingNotifySelect(discord.ui.Select):
    def __init__(self, parent: TrainingSignupView):
        self.parent_view = parent
        super().__init__(
            placeholder="Training notification DMs",
            min_values=1,
            max_values=1,
            options=training_notify_options(parent.draft_notify),
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, TrainingSignupView)
        self.view.draft_notify = self.values[0] == "1"
        await self.view.refresh(interaction)


class TrainingTopicSelect(discord.ui.Select):
    def __init__(self, parent: TrainingSignupView):
        options = training_topic_options(parent.draft_topics)

        # Discord requires max_values >= 1 for select menus. Empty selection can be
        # achieved by selecting none only on newer clients, but to support all
        # clients we keep min_values=0 where discord.py supports it.
        super().__init__(
            placeholder="Select training topics",
            min_values=0,
            max_values=max(1, len(options)),
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, TrainingSignupView)
        self.view.draft_topics = set(self.values)
        await self.view.refresh(interaction)


class ExitSignupButton(discord.ui.Button):
    def __init__(self, parent: TrainingSignupView):
        dirty = signup_dirty(parent)
        super().__init__(
            label="Exit",
            style=discord.ButtonStyle.danger if dirty else discord.ButtonStyle.secondary,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content="Training signup menu closed.",
            embed=None,
            view=None,
        )


class SubmitSignupButton(discord.ui.Button):
    def __init__(self, parent: TrainingSignupView):
        dirty = signup_dirty(parent)
        super().__init__(
            label="Submit",
            style=discord.ButtonStyle.success,
            disabled=not dirty,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, TrainingSignupView)

        newly_added_topics = set(self.view.draft_topics) - set(self.view.saved_topics)
        final_notify_training = True if newly_added_topics else self.view.draft_notify

        set_user_training_interests(
            str(self.view.member.id),
            self.view.draft_topics,
        )
        update_user_training_notifications(
            str(self.view.member.id),
            final_notify_training,
        )

        new_view = TrainingSignupView(
            member=self.view.member,
            saved_topics=set(self.view.draft_topics),
            saved_notify=final_notify_training,
        )

        await interaction.response.edit_message(
            content="✅ Training sign-ups saved.",
            embed=build_signup_embed(self.view.member, new_view),
            view=new_view,
        )


def build_roster_embed(
    selected_topic: str | None = None,
    *,
    selected_voice_channel_id: int | None = None,
    selected_start: str = "now",
    guild: discord.Guild | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title="Users Looking for Training",
        description=ansi_code_block(roster_count_lines()),
    )

    if selected_topic:
        total = len(interested_members_for_topic(selected_topic))
        eligible = len(eligible_training_dm_members(selected_topic))
        embed.add_field(
            name="Selected Topic",
            value=(
                f"**{topic_label(selected_topic)}**\n"
                f"Interested: **{total}**\n"
                f"Eligible for DM right now: **{eligible}**"
            ),
            inline=False,
        )

    selected_vc = selected_voice_label(selected_voice_channel_id, guild)

    embed.add_field(
        name="Training Ping Setup",
        value=(
            f"Topic: **{topic_label(selected_topic) if selected_topic else 'Not selected'}**\n"
            f"Voice Channel: **{selected_vc}**\n"
            f"Starts: **{start_label(selected_start)}**"
        ),
        inline=False,
    )

    return embed


def topic_select_options(selected_topic: str | None = None) -> list[discord.SelectOption]:
    options: list[discord.SelectOption] = []

    counts = training_interest_counts()

    for topic in configured_training_topics()[:25]:
        count = counts.get(topic.key, 0)
        options.append(
            discord.SelectOption(
                label=topic.label,
                value=topic.key,
                description=f"{count} interested",
                default=selected_topic == topic.key,
            )
        )

    return options


def start_select_options(selected_start: str) -> list[discord.SelectOption]:
    return [
        discord.SelectOption(
            label=label,
            value=value,
            default=selected_start == value,
        )
        for label, value, _ in TRAINING_START_OPTIONS
    ]


def selected_voice_label(channel_id: int | None, guild: discord.Guild | None) -> str:
    if channel_id is None or guild is None:
        return "Not selected"

    channel = guild.get_channel(int(channel_id))

    if channel is None:
        return f"Unknown VC ({channel_id})"

    return getattr(channel, "name", str(channel))



def configured_training_roster_category_ids() -> set[int]:
    result: set[int] = set()

    for value in TRAINING_ROSTER_VOICE_CATEGORY_IDS:
        try:
            category_id = int(value)
        except (TypeError, ValueError):
            continue

        if category_id > 0:
            result.add(category_id)

    return result


def roster_voice_channel_allowed(channel: discord.abc.GuildChannel) -> bool:
    category_ids = configured_training_roster_category_ids()

    if not category_ids:
        return True

    parent = getattr(channel, "category", None)
    parent_id = getattr(parent, "id", None)

    if parent_id is None:
        parent_id = getattr(channel, "category_id", None)

    try:
        return int(parent_id or 0) in category_ids
    except (TypeError, ValueError):
        return False


def roster_voice_channels(guild: discord.Guild | None) -> list[discord.abc.GuildChannel]:
    if guild is None:
        return []

    channels: list[discord.abc.GuildChannel] = []

    for channel in guild.channels:
        if getattr(channel, "type", None) not in {
            discord.ChannelType.voice,
            discord.ChannelType.stage_voice,
        }:
            continue

        if not roster_voice_channel_allowed(channel):
            continue

        channels.append(channel)

    channels.sort(
        key=lambda channel: (
            getattr(getattr(channel, "category", None), "position", 999999),
            getattr(channel, "position", 999999),
            getattr(channel, "name", ""),
        )
    )

    return channels


def roster_voice_channel_options(
    guild: discord.Guild | None,
    selected_channel_id: int | None = None,
) -> list[discord.SelectOption]:
    channels = roster_voice_channels(guild)

    if not channels:
        return [
            discord.SelectOption(
                label="No configured voice channels",
                value="0",
                description="Update TRAINING_ROSTER_VOICE_CATEGORY_IDS in config.py.",
            )
        ]

    options: list[discord.SelectOption] = []

    for channel in channels[:25]:
        category = getattr(channel, "category", None)
        category_name = getattr(category, "name", "No Category")
        channel_id = int(channel.id)

        options.append(
            discord.SelectOption(
                label=str(channel.name)[:100],
                value=str(channel_id),
                description=str(category_name)[:100],
                default=selected_channel_id == channel_id,
            )
        )

    return options


def build_training_dm_embed(
    *,
    topic_key: str,
    voice_channel: discord.abc.GuildChannel,
    start_value: str,
    instructor: discord.Member,
    guild: discord.Guild,
    recipient_timezone: str | None = None,
) -> discord.Embed:
    start_display = training_start_display(start_value, recipient_timezone)

    embed = discord.Embed(
        title="Training Alert",
        description=(
            f"We will be practicing **{topic_label(topic_key)}** training "
            f"in **{voice_channel.name}** **{start_display}**."
        ),
    )

    embed.add_field(
        name="Server",
        value=guild.name,
        inline=True,
    )

    embed.add_field(
        name="Topic",
        value=topic_label(topic_key),
        inline=True,
    )

    embed.add_field(
        name="Voice Channel",
        value=getattr(voice_channel, "mention", voice_channel.name),
        inline=False,
    )

    embed.set_footer(
        text=(
            f"Sent by {instructor.display_name}. "
            f'To change notification settings, use "/user settings" in {guild.name}.'
        )
    )

    return embed


class RosterView(discord.ui.View):
    def __init__(
        self,
        *,
        owner_id: int,
        guild: discord.Guild | None = None,
        selected_topic: str | None = None,
        selected_voice_channel_id: int | None = None,
        selected_start: str = "now",
    ):
        super().__init__(timeout=900)
        self.owner_id = int(owner_id)
        self.guild = guild
        self.selected_topic = selected_topic
        self.selected_voice_channel_id = selected_voice_channel_id
        self.selected_start = selected_start

        self.add_item(RosterTopicSelect(self))
        self.add_item(RosterVoiceSelect(self))
        self.add_item(RosterStartSelect(self))
        self.add_item(ExitRosterButton())
        self.add_item(DMInterestedUsersButton(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the instructor who opened this roster can use it.",
                ephemeral=True,
            )
            return False

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This command can only be used inside the server.",
                ephemeral=True,
            )
            return False

        if not (member_is_admin(interaction.user) or member_has_any_role(interaction.user, instructor_role_ids())):
            await interaction.response.send_message(
                "Sorry, that is for Instructor only.",
                ephemeral=True,
            )
            return False

        return True

    async def refresh(self, interaction: discord.Interaction):
        new_view = RosterView(
            owner_id=self.owner_id,
            guild=interaction.guild,
            selected_topic=self.selected_topic,
            selected_voice_channel_id=self.selected_voice_channel_id,
            selected_start=self.selected_start,
        )

        await interaction.response.edit_message(
            embed=build_roster_embed(
                self.selected_topic,
                selected_voice_channel_id=self.selected_voice_channel_id,
                selected_start=self.selected_start,
                guild=interaction.guild,
            ),
            view=new_view,
        )


class RosterTopicSelect(discord.ui.Select):
    def __init__(self, parent: RosterView):
        super().__init__(
            placeholder="Select training topic",
            min_values=1,
            max_values=1,
            options=topic_select_options(parent.selected_topic),
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, RosterView)
        self.view.selected_topic = self.values[0]
        await self.view.refresh(interaction)


class RosterVoiceSelect(discord.ui.Select):
    def __init__(self, parent: RosterView):
        self.parent_view = parent

        options = roster_voice_channel_options(
            parent.guild,
            parent.selected_voice_channel_id,
        )
        has_channels = not (len(options) == 1 and options[0].value == "0")
        placeholder = (
            "Voice channel selected"
            if parent.selected_voice_channel_id
            else "Select voice channel"
        )

        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
            disabled=not has_channels,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, RosterView)

        if not self.values or self.values[0] == "0":
            await interaction.response.send_message(
                "No training voice channels are configured for /roster.",
                ephemeral=True,
            )
            return

        self.view.selected_voice_channel_id = int(self.values[0])

        await self.view.refresh(interaction)


class RosterStartSelect(discord.ui.Select):
    def __init__(self, parent: RosterView):
        super().__init__(
            placeholder="Training starts",
            min_values=1,
            max_values=1,
            options=start_select_options(parent.selected_start),
            row=2,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, RosterView)
        self.view.selected_start = self.values[0]
        await self.view.refresh(interaction)


class ExitRosterButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Exit",
            style=discord.ButtonStyle.danger,
            row=3,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content="Roster closed.",
            embed=None,
            view=None,
        )


class DMInterestedUsersButton(discord.ui.Button):
    def __init__(self, parent: RosterView):
        ready = parent.selected_topic is not None and parent.selected_voice_channel_id is not None
        super().__init__(
            label="DM Interested Users",
            style=discord.ButtonStyle.success if ready else discord.ButtonStyle.secondary,
            disabled=not ready,
            row=3,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, RosterView)

        if self.view.selected_topic is None:
            await interaction.response.send_message(
                "Select a training topic first.",
                ephemeral=True,
            )
            return

        if self.view.selected_voice_channel_id is None or interaction.guild is None:
            await interaction.response.send_message(
                "Select a voice channel first.",
                ephemeral=True,
            )
            return

        channel = interaction.guild.get_channel(int(self.view.selected_voice_channel_id))

        if channel is None:
            await interaction.response.send_message(
                "That voice channel no longer exists.",
                ephemeral=True,
            )
            return

        eligible = eligible_training_dm_members(self.view.selected_topic)

        await interaction.response.edit_message(
            embed=build_confirm_embed(
                topic_key=self.view.selected_topic,
                voice_channel=channel,
                start_value=self.view.selected_start,
                eligible_count=len(eligible),
            ),
            view=ConfirmTrainingDMView(
                owner_id=self.view.owner_id,
                selected_topic=self.view.selected_topic,
                selected_voice_channel_id=self.view.selected_voice_channel_id,
                selected_start=self.view.selected_start,
            ),
        )


def build_confirm_embed(
    *,
    topic_key: str,
    voice_channel: discord.abc.GuildChannel,
    start_value: str,
    eligible_count: int,
) -> discord.Embed:
    embed = discord.Embed(
        title="Confirm Training DM",
        description=(
            f"You are about to DM **{eligible_count}** users.\n\n"
            f"Topic: **{topic_label(topic_key)}**\n"
            f"Voice Channel: **{voice_channel.name}**\n"
            f"Starts: **{start_label(start_value)}**"
        ),
    )

    return embed


def build_sending_embed(
    *,
    topic_key: str,
    voice_channel: discord.abc.GuildChannel,
    start_value: str,
) -> discord.Embed:
    embed = discord.Embed(
        title="Sending Training DMs",
        description=(
            "Training DMs are being sent now. This may take a moment.\n\n"
            f"Topic: **{topic_label(topic_key)}**\n"
            f"Voice Channel: **{voice_channel.name}**\n"
            f"Starts: **{start_label(start_value)}**"
        ),
    )

    return embed


class ConfirmTrainingDMView(discord.ui.View):
    def __init__(
        self,
        *,
        owner_id: int,
        selected_topic: str,
        selected_voice_channel_id: int,
        selected_start: str,
    ):
        super().__init__(timeout=900)
        self.owner_id = int(owner_id)
        self.selected_topic = selected_topic
        self.selected_voice_channel_id = int(selected_voice_channel_id)
        self.selected_start = selected_start

        self.add_item(BackToRosterButton())
        self.add_item(ExitConfirmButton())
        self.add_item(ConfirmTrainingDMButton())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the instructor who opened this roster can use it.",
                ephemeral=True,
            )
            return False

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This command can only be used inside the server.",
                ephemeral=True,
            )
            return False

        if not (member_is_admin(interaction.user) or member_has_any_role(interaction.user, instructor_role_ids())):
            await interaction.response.send_message(
                "Sorry, that is for Instructor only.",
                ephemeral=True,
            )
            return False

        return True


class BackToRosterButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Back",
            style=discord.ButtonStyle.secondary,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, ConfirmTrainingDMView)

        await interaction.response.edit_message(
            embed=build_roster_embed(
                self.view.selected_topic,
                selected_voice_channel_id=self.view.selected_voice_channel_id,
                selected_start=self.view.selected_start,
                guild=interaction.guild,
            ),
            view=RosterView(
                owner_id=self.view.owner_id,
                guild=interaction.guild,
                selected_topic=self.view.selected_topic,
                selected_voice_channel_id=self.view.selected_voice_channel_id,
                selected_start=self.view.selected_start,
            ),
        )


class ExitConfirmButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Exit",
            style=discord.ButtonStyle.danger,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content="Roster closed.",
            embed=None,
            view=None,
        )


class ConfirmTrainingDMButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Confirm",
            style=discord.ButtonStyle.success,
        )

    async def callback(self, interaction: discord.Interaction):
        assert isinstance(self.view, ConfirmTrainingDMView)

        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This command can only be used inside the server.",
                ephemeral=True,
            )
            return

        remaining = training_dm_cooldown_remaining(interaction.user.id)

        if remaining > 0:
            await interaction.response.send_message(
                f"You can send another training DM in {format_training_dm_cooldown(remaining)}.",
                ephemeral=True,
            )
            return

        send_key = confirm_send_key(interaction=interaction, view=self.view)

        if send_key in training_dm_sends_in_progress:
            await interaction.response.send_message(
                "This training DM is already being sent.",
                ephemeral=True,
            )
            return

        channel = interaction.guild.get_channel(int(self.view.selected_voice_channel_id))

        if channel is None:
            await interaction.response.send_message(
                "That voice channel no longer exists.",
                ephemeral=True,
            )
            return

        training_dm_sends_in_progress.add(send_key)
        set_training_dm_cooldown(interaction.user.id)

        await interaction.response.edit_message(
            embed=build_sending_embed(
                topic_key=self.view.selected_topic,
                voice_channel=channel,
                start_value=self.view.selected_start,
            ),
            view=None,
        )

        try:
            eligible = eligible_training_dm_members(self.view.selected_topic)
            sent = 0
            failed = 0
            missing = 0

            for record in eligible:
                try:
                    member = interaction.guild.get_member(int(record.discord_id))
                except (TypeError, ValueError):
                    member = None

                if member is None:
                    missing += 1
                    continue

                if member.bot:
                    continue

                embed = build_training_dm_embed(
                    topic_key=self.view.selected_topic,
                    voice_channel=channel,
                    start_value=self.view.selected_start,
                    instructor=interaction.user,
                    guild=interaction.guild,
                    recipient_timezone=record.timezone,
                )

                try:
                    await member.send(embed=embed)
                    sent += 1
                except (discord.Forbidden, discord.HTTPException):
                    failed += 1
                except Exception:
                    failed += 1

            result = discord.Embed(
                title="Training DMs Sent",
                description=(
                    f"Topic: **{topic_label(self.view.selected_topic)}**\n"
                    f"Sent: **{sent}**\n"
                    f"Failed/blocked: **{failed}**\n"
                    f"Not in server: **{missing}**"
                ),
            )

            await interaction.edit_original_response(
                embed=result,
                view=None,
            )
        finally:
            training_dm_sends_in_progress.discard(send_key)


class TrainingCommands(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    training_group = app_commands.Group(
        name="training",
        description="Training commands",
    )

    @training_group.command(
        name="signup",
        description="Sign up for training interest notifications.",
    )
    @app_commands.guild_only()
    async def training_signup(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This command can only be used inside the server.",
                ephemeral=True,
            )
            return

        ensure_member(interaction.user)

        saved_topics = user_training_interest_keys(str(interaction.user.id))
        saved_notify = get_user_training_notify(str(interaction.user.id))

        view = TrainingSignupView(
            member=interaction.user,
            saved_topics=saved_topics,
            saved_notify=saved_notify,
        )

        await interaction.response.send_message(
            embed=build_signup_embed(interaction.user, view),
            view=view,
            ephemeral=True,
        )

    @app_commands.command(
        name="roster",
        description="View training interest roster and send training DMs.",
    )
    @app_commands.guild_only()
    async def roster(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This command can only be used inside the server.",
                ephemeral=True,
            )
            return

        if not (member_is_admin(interaction.user) or member_has_any_role(interaction.user, instructor_role_ids())):
            await interaction.response.send_message(
                "Sorry, that is for Instructor only.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=build_roster_embed(guild=interaction.guild),
            view=RosterView(owner_id=interaction.user.id, guild=interaction.guild),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(TrainingCommands(bot))
