from __future__ import annotations

import discord

try:
    from config import ADMIN_ROLE
except ImportError:
    ADMIN_ROLE = 0

try:
    from config import MISSION_EXECUTER_ROLE
except ImportError:
    MISSION_EXECUTER_ROLE = 0

try:
    from config import FLIGHT_LEAD_ROLE
except ImportError:
    FLIGHT_LEAD_ROLE = 0

try:
    from config import INSTRUCTOR_ROLE
except ImportError:
    INSTRUCTOR_ROLE = 0

try:
    from config import MISSION_QUALIFIED_ROLE
except ImportError:
    MISSION_QUALIFIED_ROLE = 0


def _single_role_id(value) -> set[int]:
    try:
        role_id = int(value or 0)
    except Exception:
        return set()

    if role_id <= 0:
        return set()

    return {role_id}


def admin_role_ids() -> set[int]:
    return _single_role_id(ADMIN_ROLE)


def mission_executer_role_ids() -> set[int]:
    return _single_role_id(MISSION_EXECUTER_ROLE)


def flight_lead_role_ids() -> set[int]:
    return _single_role_id(FLIGHT_LEAD_ROLE)


def instructor_role_ids() -> set[int]:
    return _single_role_id(INSTRUCTOR_ROLE)


def mission_qualified_role_ids() -> set[int]:
    return _single_role_id(MISSION_QUALIFIED_ROLE)


def member_has_any_role(member: discord.Member, role_ids: set[int]) -> bool:
    if not role_ids:
        return False

    return any(int(role.id) in role_ids for role in member.roles)


def member_is_admin(member: discord.Member) -> bool:
    return member_has_any_role(member, admin_role_ids())


def member_is_mission_qualified(member: discord.Member) -> bool:
    return member_has_any_role(member, mission_qualified_role_ids())


async def _send_ephemeral(
    interaction: discord.Interaction,
    content: str,
    *,
    view: discord.ui.View | None = None,
) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(
            content,
            view=view,
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            content,
            view=view,
            ephemeral=True,
        )



class GetQualifiedButton(discord.ui.Button):
    def __init__(self, owner_id: int):
        super().__init__(
            label="Get Qualified",
            style=discord.ButtonStyle.success,
        )
        self.owner_id = int(owner_id)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This button is only for the person who opened this message.",
                ephemeral=True,
            )
            return

        # A command-permission prompt may be shown while other command logic is
        # still unwinding. Defer the button immediately so Discord does not show
        # "This interaction failed" while the qualification wizard loads.
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            from cogs.get_qualified import start_request_qualification_wizard

            await start_request_qualification_wizard(interaction)
        except Exception as error:
            message = (
                "Please run `/get qualified` to start your qualification request. "
                f"Error: `{type(error).__name__}: {error}`"
            )

            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)


class GetQualifiedPromptView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=900)
        self.add_item(GetQualifiedButton(owner_id))


async def require_command_role(
    interaction: discord.Interaction,
    *,
    allowed_role_ids: set[int],
    role_label: str,
    require_mission_qualified: bool = True,
) -> bool:
    if not isinstance(interaction.user, discord.Member):
        await _send_ephemeral(
            interaction,
            "This command can only be used inside the server.",
        )
        return False

    member = interaction.user

    if member_is_admin(member):
        return True

    if require_mission_qualified and not member_is_mission_qualified(member):
        await _send_ephemeral(
            interaction,
            "Looks like you arent qualified yet! Press the button bellow to get qualified!",
            view=GetQualifiedPromptView(member.id),
        )
        return False

    if not member_has_any_role(member, allowed_role_ids):
        await _send_ephemeral(
            interaction,
            f"Sorry, that is for {role_label} only.",
        )
        return False

    return True


async def require_admin_command(interaction: discord.Interaction) -> bool:
    if not isinstance(interaction.user, discord.Member):
        await _send_ephemeral(
            interaction,
            "This command can only be used inside the server.",
        )
        return False

    if member_is_admin(interaction.user):
        return True

    if not member_is_mission_qualified(interaction.user):
        await _send_ephemeral(
            interaction,
            "Looks like you arent qualified yet! Press the button bellow to get qualified!",
            view=GetQualifiedPromptView(interaction.user.id),
        )
        return False

    await _send_ephemeral(
        interaction,
        "Sorry, that is for Admin only.",
    )
    return False


async def require_mission_executer_command(interaction: discord.Interaction) -> bool:
    return await require_command_role(
        interaction,
        allowed_role_ids=mission_executer_role_ids(),
        role_label="Mission Executer",
    )


async def require_flight_lead_command(interaction: discord.Interaction) -> bool:
    return await require_command_role(
        interaction,
        allowed_role_ids=flight_lead_role_ids(),
        role_label="Flight Lead",
    )


async def require_instructor_command(interaction: discord.Interaction) -> bool:
    return await require_command_role(
        interaction,
        allowed_role_ids=instructor_role_ids(),
        role_label="Instructor",
    )


async def require_mission_qualified_command(interaction: discord.Interaction) -> bool:
    return await require_command_role(
        interaction,
        allowed_role_ids=mission_qualified_role_ids(),
        role_label="Mission Qualified",
    )
