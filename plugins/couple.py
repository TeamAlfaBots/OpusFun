"""/couple — a random, purely-for-fun pairing of two group members.

Rules enforced here:

* groups/supergroups only;
* two *different*, non-bot members;
* one successful run per group per cooldown window (6h by default), stored in
  MongoDB so a restart cannot reset it;
* the same pair is shown again for the rest of the window.
"""

from __future__ import annotations

import logging
import random
from typing import Dict, List, Optional

from pyrogram import Client, filters
from pyrogram.enums import ChatMembersFilter, ChatType
from pyrogram.errors import ChatAdminRequired, RPCError
from pyrogram.types import Message, User

from core.helpers import escape_html, format_duration, mention_id
from core.i18n import tr
from core.reactions import COUPLE_FOLDER
from core.sender import send_media_reply
from plugins.tracking import track
from utils.decorators import anti_spam, group_only, handle_errors

log = logging.getLogger(__name__)

#: How many recent members to scan when building the candidate pool.
_MEMBER_SCAN_LIMIT = 250
#: Minimum candidates required to form a couple.
_MIN_CANDIDATES = 2


async def _collect_candidates(client: Client, chat_id: int) -> List[User]:
    """Gather eligible (human, non-deleted) members of a group."""
    candidates: Dict[int, User] = {}

    try:
        async for member in client.get_chat_members(
            chat_id, limit=_MEMBER_SCAN_LIMIT, filter=ChatMembersFilter.RECENT
        ):
            user = getattr(member, "user", None)
            if not _is_eligible(user, client):
                continue
            candidates[user.id] = user
    except ChatAdminRequired:
        log.info("Cannot list members of %s without admin rights", chat_id)
    except RPCError as exc:
        log.warning("get_chat_members failed for %s: %s", chat_id, exc)

    if len(candidates) < _MIN_CANDIDATES:
        # Fallback for groups where member listing is restricted: use the
        # people we have actually seen chatting there.
        try:
            async for member in client.get_chat_members(chat_id, limit=_MEMBER_SCAN_LIMIT):
                user = getattr(member, "user", None)
                if not _is_eligible(user, client):
                    continue
                candidates[user.id] = user
        except RPCError as exc:
            log.debug("Secondary member scan failed for %s: %s", chat_id, exc)

    return list(candidates.values())


def _is_eligible(user: Optional[User], client: Client) -> bool:
    if user is None:
        return False
    if getattr(user, "is_bot", False) or getattr(user, "is_deleted", False):
        return False
    if getattr(user, "id", None) == getattr(client, "bot_id", 0):
        return False
    return True


def _format_announcement(
    client: Client,
    first_mention: str,
    second_mention: str,
    *,
    repeat: bool = False,
    remaining: float = 0.0,
) -> str:
    text = tr(
        "couple.announcement",
        user1=first_mention,
        user2=second_mention,
        bot_name=escape_html(client.config.bot_name),
    )
    if repeat:
        text += "\n\n" + tr("couple.repeat_note", remaining=format_duration(remaining))
    return text


@Client.on_message(filters.command(["couple", "couples"]) & ~filters.via_bot, group=0)
@handle_errors
@anti_spam(period=4.0)
@group_only
async def couple_command(client: Client, message: Message) -> None:
    chat = message.chat
    await track(client, message)

    cooldown = client.couple_cooldown
    state = await cooldown.acquire(chat.id)

    if not state.allowed:
        # Still inside the window: re-show the stored pair, plus time left.
        stored = await client.db.get_last_couple(chat.id)
        if stored:
            first = stored.get("first", {})
            second = stored.get("second", {})
            caption = _format_announcement(
                client,
                mention_id(first.get("id", 0), first.get("name", "Someone")),
                mention_id(second.get("id", 0), second.get("name", "Someone")),
                repeat=True,
                remaining=state.remaining,
            )
            item = await client.media.choose(COUPLE_FOLDER)
            await send_media_reply(
                client, message, item, caption, pool=client.media, folder=COUPLE_FOLDER
            )
            return

        await message.reply_text(
            tr("couple.cooldown", remaining=format_duration(state.remaining)), quote=True
        )
        return

    # Cooldown claimed — from here on, any failure must release it again.
    try:
        candidates = await _collect_candidates(client, chat.id)

        if len(candidates) < _MIN_CANDIDATES:
            await cooldown.release(chat.id)
            await message.reply_text(tr("couple.not_enough_members"), quote=True)
            return

        first, second = random.sample(candidates, 2)

        first_doc = {"id": first.id, "name": first.first_name or "Someone"}
        second_doc = {"id": second.id, "name": second.first_name or "Someone"}

        caption = _format_announcement(
            client,
            mention_id(first_doc["id"], first_doc["name"]),
            mention_id(second_doc["id"], second_doc["name"]),
        )

        item = await client.media.choose(COUPLE_FOLDER)
        sent = await send_media_reply(
            client, message, item, caption, pool=client.media, folder=COUPLE_FOLDER
        )

        if sent is None:
            await cooldown.release(chat.id)
            log.warning("Couple announcement failed to send in %s; cooldown released", chat.id)
            return

        await client.db.record_couple(chat.id, first_doc, second_doc)
        log.info("Couple chosen in %s: %s + %s", chat.id, first.id, second.id)

    except Exception:
        # Never leave a group locked out because of an unexpected error.
        await cooldown.release(chat.id)
        raise
