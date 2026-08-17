"""Resilient media sending.

One place decides *how* to put a :class:`MediaItem` on the wire and what to do
when Telegram refuses it.  Failures fall back through: requested kind ->
alternate kind -> plain text, so an invalid file degrades a single message
instead of breaking the command.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Optional

from pyrogram import Client
from pyrogram.errors import (
    FloodWait,
    MediaEmpty,
    PhotoInvalidDimensions,
    RPCError,
    WebpageCurlFailed,
    WebpageMediaEmpty,
)
from pyrogram.types import InlineKeyboardMarkup, Message

from core.helpers import MAX_CAPTION_LENGTH, truncate
from utils.random_media import MediaItem, MediaKind, MediaPool

log = logging.getLogger(__name__)

#: Errors that mean "this specific file is unusable" — try another one.
_BAD_MEDIA_ERRORS = (
    MediaEmpty,
    PhotoInvalidDimensions,
    WebpageCurlFailed,
    WebpageMediaEmpty,
)


async def send_media_reply(
    client: Client,
    message: Message,
    item: Optional[MediaItem],
    caption: str,
    *,
    pool: Optional[MediaPool] = None,
    folder: Optional[str] = None,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    max_attempts: int = 3,
) -> Optional[Message]:
    """Reply with ``item``; retry other files from ``folder`` on media errors.

    Returns the sent :class:`~pyrogram.types.Message`, or ``None`` if even the
    text-only fallback failed.
    """
    caption = truncate(caption, MAX_CAPTION_LENGTH)
    tried: list[str] = []
    current = item

    for attempt in range(1, max_attempts + 1):
        if current is None:
            break
        tried.append(current.value)
        try:
            return await _dispatch(client, message, current, caption, reply_markup)

        except _BAD_MEDIA_ERRORS as exc:
            log.warning(
                "Invalid media (%s) from %s: %s",
                type(exc).__name__,
                _short(current.value),
                exc,
            )
        except FloodWait as exc:
            wait = int(getattr(exc, "value", 0) or 0)
            if wait > 20:
                log.warning("FloodWait %ss while sending media; giving up on media", wait)
                break
            log.info("FloodWait %ss while sending media; sleeping", wait)
            await asyncio.sleep(wait + 1)
            continue
        except RPCError as exc:
            log.warning("Telegram refused media %s: %s", _short(current.value), exc)
            if attempt >= 2:
                break
        except OSError as exc:
            log.warning("Filesystem error reading %s: %s", _short(current.value), exc)

        # Pick a different file for the next attempt.
        current = None
        if pool is not None and folder:
            current = await pool.choose(folder, exclude=tried)

    # Last resort: deliver the message without media so the user still gets it.
    try:
        return await message.reply_text(
            truncate(caption), quote=True, reply_markup=reply_markup, disable_web_page_preview=True
        )
    except RPCError as exc:
        log.error("Could not deliver text fallback: %s", exc)
        return None


async def _dispatch(
    client: Client,
    message: Message,
    item: MediaItem,
    caption: str,
    reply_markup: Optional[InlineKeyboardMarkup],
) -> Message:
    """Send one media item using the right Telegram method."""
    target: Any = item.value
    if not item.is_remote:
        path = Path(item.value)
        if not path.is_file():
            raise MediaEmpty(f"File disappeared: {path.name}")
        target = str(path)

    kwargs: dict[str, Any] = {"caption": caption, "quote": True}
    if reply_markup is not None:
        kwargs["reply_markup"] = reply_markup

    if item.kind == MediaKind.PHOTO:
        return await message.reply_photo(target, **kwargs)
    if item.kind == MediaKind.VIDEO:
        return await message.reply_video(target, **kwargs)
    return await message.reply_animation(target, **kwargs)


async def send_media_to_chat(
    client: Client,
    chat_id: int,
    item: Optional[MediaItem],
    caption: str,
    *,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> Optional[Message]:
    """Send media to a chat without replying to a specific message."""
    caption = truncate(caption, MAX_CAPTION_LENGTH)
    if item is not None:
        try:
            if item.kind == MediaKind.PHOTO:
                return await client.send_photo(
                    chat_id, item.value, caption=caption, reply_markup=reply_markup
                )
            if item.kind == MediaKind.VIDEO:
                return await client.send_video(
                    chat_id, item.value, caption=caption, reply_markup=reply_markup
                )
            return await client.send_animation(
                chat_id, item.value, caption=caption, reply_markup=reply_markup
            )
        except (RPCError, OSError) as exc:
            log.warning("Falling back to text for chat %s: %s", chat_id, exc)

    try:
        return await client.send_message(
            chat_id, truncate(caption), reply_markup=reply_markup, disable_web_page_preview=True
        )
    except RPCError as exc:
        log.error("Could not send message to %s: %s", chat_id, exc)
        return None


def _short(value: str, limit: int = 80) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."
