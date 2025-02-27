"""
This module defines the "client" singleton. It really should be a singleton so it should never be re-created.
"""

import discord
import discord.ext.commands
import asyncio
import logging
from typing import Any
import static_config

logger: logging.Logger = logging.getLogger(__name__)

try:
    client
    logger.warn("Refusing to re-create the Discord client", stack_info=True)
except NameError:
    intents = discord.Intents.all()
    intents.presences = False
    client: discord.ext.commands.Bot = discord.ext.commands.Bot(
        command_prefix=(),
        loop=asyncio.get_event_loop(),
        max_messages=None,
        intents=intents,
        allowed_mentions=discord.AllowedMentions(everyone=False, roles=False))

    # Disable command functionality until reenabled again in plugins.commands
    @client.event
    async def on_message(*args: Any, **kwargs: Any) -> None:
        pass
    del on_message

    @client.event
    async def on_error(event: str, *args: Any, **kwargs: Any) -> None:
        logger.error("Uncaught exception in {}".format(event), exc_info=True)

async def main_task() -> None:
    try:
        await client.start(static_config.Discord["token"], reconnect=True)
    except:
        logger.critical("Exception in main Discord task", exc_info=True)
    finally:
        await client.close()
