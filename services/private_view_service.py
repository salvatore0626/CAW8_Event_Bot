from __future__ import annotations

import discord

try:
    from config import PRIVATE_VIEW_TIMEOUT_SECONDS
except ImportError:
    PRIVATE_VIEW_TIMEOUT_SECONDS = 900


TIMEOUT_MESSAGE = "Message timed out. Please rerun the command."


class PrivateTimeoutView(discord.ui.View):
    """Small helper for ephemeral/private interactive messages.

    Discord does not let us reliably clean up old ephemeral messages after a bot
    restart, but while the bot is running this edits the message on timeout.
    """

    def __init__(self, *, timeout: float | None = None):
        super().__init__(
            timeout=(
                float(timeout)
                if timeout is not None
                else float(PRIVATE_VIEW_TIMEOUT_SECONDS)
            )
        )
        self._timeout_message: discord.InteractionMessage | discord.Message | None = None

    def bind_timeout_message(
        self,
        message: discord.InteractionMessage | discord.Message | None,
    ):
        self._timeout_message = message
        return self

    async def on_timeout(self) -> None:
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True

        if self._timeout_message is None:
            return

        try:
            await self._timeout_message.edit(
                content=TIMEOUT_MESSAGE,
                embed=None,
                attachments=[],
                view=None,
            )
        except Exception:
            return


def bind_private_view(
    view,
    message: discord.InteractionMessage | discord.Message | None,
):
    if hasattr(view, "bind_timeout_message"):
        view.bind_timeout_message(message)

    return view


async def bind_view_to_original_response(
    interaction: discord.Interaction,
    view,
):
    if view is None:
        return None

    try:
        message = await interaction.original_response()
    except Exception:
        return view

    return bind_private_view(view, message)
