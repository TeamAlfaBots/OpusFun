"""Registration of users and groups.

Every interaction refreshes the database record for the sender and, in groups,
for the chat.  These records are what ``/broadcast`` iterates over, so they are
written from a single helper to keep the data consistent.

A short-lived in-process cache prevents hammering MongoDB with an identical
upsert for every message in a busy group; correctness is unaffected because
the fields written are idempotent.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, Tuple

from pyrogram import Client, filters
from pyrogram.enums import ChatType
from pyrogram.types import Message

from core.database import DatabaseError
from utils.decorators import handle_errors

log = logging.getLogger(__name__)

#: Re-write a record at most once per this many seconds.
_TOUCH_INTERVAL = 300.0
_last_touch: Dict[Tuple[str, int], float] = {}
_MAX_CACHE = 50_000


def _should_touch(kind: str, entity_id: int) -> bool:
    now = time.monotonic()
    key = (kind, int(entity_id))
    last = _last_touch.get(key)
    if last is not None and now - last < _TOUCH_INTERVAL:
        return False
    if len(_last_touch) >= _MAX_CACHE:
        cutoff = now - _TOUCH_INTERVAL
        for stale_key in [k for k, ts in _last_touch.items() if ts < cutoff]:
            _last_touch.pop(stale_key, None)
        if len(_last_touch) >= _MAX_CACHE:
            _last_touch.clear()
    _last_touch[key] = now
    return True


async def track(client: Client, message: Message, *, force: bool = False) -> None:
    """Persist the sender and (for groups) the chat. Never raises."""
    try:
        user = getattr(message, "from_user", None)
        if user is not None and not getattr(user, "is_bot", False):
            if force or _should_touch("user", user.id):
                created = await client.db.save_user(
                    user.id, first_name=user.first_name, username=user.username
                )
                if created:
                    log.info("New user registered: %s (@%s)", user.id, user.username or "-")

        chat = getattr(message, "chat", None)
        if chat is not None and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
            if force or _should_touch("group", chat.id):
                created = await client.db.save_group(
                    chat.id, title=chat.title, username=chat.username
                )
                if created:
                    log.info("New group registered: %s (%s)", chat.title, chat.id)

    except DatabaseError as exc:
        log.error("Tracking skipped, database unavailable: %s", exc)
    except Exception:  # pragma: no cover - tracking must never break a command
        log.exception("Unexpected error while tracking update")


@Client.on_message(filters.incoming & ~filters.service, group=10)
@handle_errors
async def passive_tracker(client: Client, message: Message) -> None:
    """Low-priority handler that registers activity from any message.

    It runs in group 10 so command handlers (group 0) always execute first.
    """
    await track(client, message)


@Client.on_message(filters.left_chat_member & filters.group, group=10)
@handle_errors
async def on_left_chat_member(client: Client, message: Message) -> None:
    """Deactivate a group when the bot itself is removed."""
    left = getattr(message, "left_chat_member", None)
    if left is None:
        return
    if getattr(left, "id", None) == getattr(client, "bot_id", 0):
        await client.db.mark_group_inactive(message.chat.id, reason="bot_removed")
        log.info("Removed from group %s (%s)", message.chat.title, message.chat.id)
