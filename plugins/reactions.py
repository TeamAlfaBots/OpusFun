"""Generic GIF reaction engine.

A single handler serves every reaction command.  It is registered once against
the union of all commands and aliases from ``core.reactions.REGISTRY``; the
spec looked up from the invoked command decides the folder, the locale key and
whether a reply target is required.
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from pyrogram import Client, filters
from pyrogram.enums import ChatType
from pyrogram.types import Message, User

from core.helpers import escape_html, is_group_chat, mention_user, user_display_name
from core.i18n import tr
from core.reactions import ReactionSpec, all_commands, get_spec
from core.sender import send_media_reply
from plugins.tracking import track
from utils.decorators import anti_spam, handle_errors

log = logging.getLogger(__name__)

#: Every command + alias handled by this one function.
_COMMANDS = all_commands()


def _invoked_command(message: Message) -> str:
    """The command actually typed, without prefix, bot suffix or arguments."""
    if getattr(message, "command", None):
        raw = message.command[0]
    else:
        raw = (message.text or message.caption or "").split(maxsplit=1)[0].lstrip("/!")
    return raw.split("@", 1)[0].lower()


def _resolve_target(message: Message) -> Tuple[Optional[User], bool]:
    """Find the reaction target.

    Returns ``(target_user, is_self_reaction)``.  Only an explicit reply
    designates a target — the bot never guesses arbitrary members.
    """
    replied = getattr(message, "reply_to_message", None)
    if replied is None:
        return None, False

    target = getattr(replied, "from_user", None)
    if target is None:
        # Anonymous admins / channel posts have no ``from_user``.
        return None, False

    sender = getattr(message, "from_user", None)
    is_self = bool(sender and target.id == sender.id)
    return target, is_self


@Client.on_message(filters.command(_COMMANDS) & ~filters.via_bot & ~filters.forwarded, group=0)
@handle_errors
@anti_spam(period=2.5)
async def reaction_handler(client: Client, message: Message) -> None:
    """Single entry point for /slap, /hug, /dance … and their aliases."""
    command = _invoked_command(message)
    spec: Optional[ReactionSpec] = get_spec(command)
    if spec is None:  # another plugin owns this command
        return

    sender = getattr(message, "from_user", None)
    if sender is None or getattr(sender, "is_bot", False):
        return

    await track(client, message)

    if spec.group_only and not is_group_chat(message.chat):
        await message.reply_text(tr("errors.group_only"), quote=True)
        return

    target, is_self = _resolve_target(message)

    if target is None:
        if spec.require_reply:
            await message.reply_text(tr("errors.no_reply"), quote=True)
            return
        caption = _render_solo(spec, sender)
    elif getattr(target, "is_bot", False) and target.id == getattr(client, "bot_id", 0):
        caption = tr("reactions.bot_target", user=mention_user(sender))
    elif is_self:
        caption = tr("reactions.self_target", user=mention_user(sender))
    else:
        caption = tr(
            spec.message_key,
            user1=mention_user(sender),
            user2=mention_user(target),
            user=mention_user(target),
            sender=escape_html(user_display_name(sender)),
            target=escape_html(user_display_name(target)),
        )

    item = await client.media.choose(spec.folder)
    if item is None:
        # Folder empty or missing: still deliver the fun text.
        log.warning("No media available for /%s (folder assist/%s)", spec.command, spec.folder)
        await message.reply_text(
            f"{caption}\n\n<i>{escape_html(tr('errors.no_media_hint'))}</i>", quote=True
        )
        return

    await send_media_reply(
        client,
        message,
        item,
        caption,
        pool=client.media,
        folder=spec.folder,
    )
    log.info(
        "/%s by %s in chat %s (target=%s)",
        command,
        sender.id,
        message.chat.id,
        getattr(target, "id", None),
    )


def _render_solo(spec: ReactionSpec, sender: User) -> str:
    """Message used when a no-reply-required command is sent standalone."""
    key = spec.solo_message_key or spec.message_key
    return tr(
        key,
        user=mention_user(sender),
        user1=mention_user(sender),
        user2=mention_user(sender),
        sender=escape_html(user_display_name(sender)),
        target=escape_html(user_display_name(sender)),
    )


@Client.on_message(filters.new_chat_members & filters.group, group=2)
@handle_errors
async def auto_welcome(client: Client, message: Message) -> None:
    """Greet people who join, using the /welcome media pool."""
    members = getattr(message, "new_chat_members", None) or []
    me_id = getattr(client, "bot_id", 0)

    joined_self = any(getattr(m, "id", None) == me_id for m in members)
    if joined_self:
        # The bot itself was added: register the group and say hello.
        await track(client, message)
        chat = message.chat
        await message.reply_text(
            tr(
                "system.added_to_group",
                bot_name=client.config.bot_name,
                chat=escape_html(getattr(chat, "title", "this group")),
            ),
            quote=False,
        )
        log.info("Added to group %s (%s)", getattr(chat, "title", "?"), chat.id)
        return

    humans = [m for m in members if not getattr(m, "is_bot", False)]
    if not humans:
        return

    await track(client, message)

    mentions = ", ".join(mention_user(user) for user in humans[:5])
    caption = tr(
        "reactions.welcome",
        user=mentions,
        user1=mentions,
        user2=mentions,
        chat=escape_html(getattr(message.chat, "title", "the group")),
        target=escape_html(user_display_name(humans[0])),
        sender=escape_html(user_display_name(humans[0])),
    )

    item = await client.media.choose("welcome")
    if item is None:
        await message.reply_text(caption, quote=False)
        return

    await send_media_reply(
        client, message, item, caption, pool=client.media, folder="welcome"
    )


@Client.on_message(filters.command(["ping", "alive"]) & ~filters.via_bot, group=0)
@handle_errors
@anti_spam(period=3.0)
async def ping_command(client: Client, message: Message) -> None:
    """Simple liveness/latency probe."""
    import time

    started = time.perf_counter()
    await track(client, message)
    sent = await message.reply_text(tr("system.pinging"), quote=True)
    latency_ms = (time.perf_counter() - started) * 1000

    from core.helpers import format_duration

    db_ok = await client.db.health_check()
    await sent.edit_text(
        tr(
            "system.pong",
            bot_name=client.config.bot_name,
            latency=f"{latency_ms:.0f}",
            uptime=format_duration(client.uptime),
            database=tr("system.db_ok") if db_ok else tr("system.db_down"),
        )
    )
