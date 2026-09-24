"""
helper/ask.py
─────────────
Native Pyrogram v2 replacement for pyromod's client.ask().
"""

import asyncio

from pyrogram import Client
from pyrogram.handlers import MessageHandler
from pyrogram.types import Message


async def ask(
    client: Client,
    chat_id: int,
    text: str,
    timeout: int = 30,
    filters=None,
    **send_kwargs,
) -> Message:
    send_kwargs.pop("reply_to_message_id", None)
    await client.send_message(chat_id, text, **send_kwargs)

    loop = asyncio.get_running_loop()          # fix: get_running_loop not get_event_loop
    future: asyncio.Future = loop.create_future()

    async def _on_message(_, message: Message) -> None:
        if message.chat.id != chat_id:
            return
        if filters is not None:
            if not await filters(client, message):
                return
        if not future.done():
            future.set_result(message)

    handler = MessageHandler(_on_message)
    client.add_handler(handler, group=-1)

    try:
        return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
    except asyncio.TimeoutError:
        if not future.done():
            future.cancel()
        raise
    finally:
        try:
            client.remove_handler(handler, group=-1)
        except Exception:
            pass
