"""/start, /help and the inline keyboard callbacks behind them."""

from __future__ import annotations

import logging

from pyrogram import Client, filters
from pyrogram.errors import MessageNotModified, RPCError
from pyrogram.types import CallbackQuery, Message

from core.helpers import escape_html, format_number, is_valid_http_url, mention_user
from core.i18n import tr
from core.keyboards import help_keyboard, help_section_keyboard, start_keyboard
from core.reactions import REACTIONS
from plugins.tracking import track
from utils.decorators import anti_spam, handle_errors

log = logging.getLogger(__name__)


def _fun_commands_block() -> str:
    """Build the fun-command list straight from the reaction registry."""
    lines = ["/couple"]
    for spec in REACTIONS:
        lines.append(f"/{spec.command}")
    return "\n".join(lines)


def _fun_commands_detailed() -> str:
    lines = [f"• /couple — {escape_html(tr('help.couple_desc'))}"]
    for spec in REACTIONS:
        desc = tr(f"help.desc.{spec.command}")
        lines.append(f"• /{spec.command} — {spec.emoji} {escape_html(desc)}")
    return "\n".join(lines)


def _start_text(client: Client, message_user) -> str:
    return tr(
        "start.text",
        user=mention_user(message_user),
        user_name=escape_html(getattr(message_user, "first_name", "there")),
        bot_name=escape_html(client.config.bot_name),
    )


@Client.on_message(filters.command("start") & ~filters.via_bot, group=0)
@handle_errors
@anti_spam(period=2.0)
async def start_command(client: Client, message: Message) -> None:
    """Send the branded start card with the inline keyboard."""
    await track(client, message, force=True)

    user = message.from_user
    text = _start_text(client, user)
    keyboard = start_keyboard(client.config, tr)
    image = client.config.start_img_url

    if image and (is_valid_http_url(image) or (client.config.base_dir / image).is_file()):
        target = image if is_valid_http_url(image) else str(client.config.base_dir / image)
        try:
            await message.reply_photo(
                target, caption=text, reply_markup=keyboard, quote=True
            )
            log.info("/start served (with image) to %s", getattr(user, "id", "?"))
            return
        except RPCError as exc:
            # Bad/unreachable image must not block the greeting.
            log.warning("START_IMG_URL could not be sent (%s); falling back to text", exc)

    await message.reply_text(
        text, reply_markup=keyboard, quote=True, disable_web_page_preview=False
    )
    log.info("/start served (text) to %s", getattr(user, "id", "?"))


@Client.on_message(filters.command("help") & ~filters.via_bot, group=0)
@handle_errors
@anti_spam(period=2.0)
async def help_command(client: Client, message: Message) -> None:
    await track(client, message)
    text = tr(
        "help.text",
        bot_name=escape_html(client.config.bot_name),
        fun_commands=_fun_commands_block(),
    )
    await message.reply_text(
        text, reply_markup=help_keyboard(client.config, tr), quote=True
    )


@Client.on_callback_query(filters.regex(r"^help:"), group=0)
@handle_errors
async def help_callbacks(client: Client, query: CallbackQuery) -> None:
    """Handle the Help / Fun / Owner / Back / Close inline buttons."""
    action = (query.data or "").split(":", 1)[-1]

    if action == "close":
        try:
            await query.message.delete()
        except RPCError:
            await query.message.edit_text(tr("help.closed"))
        await query.answer()
        return

    if action == "back":
        user = query.from_user
        text = _start_text(client, user)
        await _safe_edit(query, text, start_keyboard(client.config, tr))
        await query.answer()
        return

    if action == "fun":
        text = tr("help.fun_section", commands=_fun_commands_detailed())
        await _safe_edit(query, text, help_section_keyboard(tr))
        await query.answer()
        return

    if action == "owner":
        if not client.is_owner(query.from_user.id):
            await query.answer(tr("errors.owner_only_short"), show_alert=True)
            return
        stats = await client.db.stats()
        text = tr(
            "help.owner_section",
            users=format_number(stats["users"]),
            groups=format_number(stats["groups"]),
        )
        await _safe_edit(query, text, help_section_keyboard(tr))
        await query.answer()
        return

    # Default: "help:open"
    text = tr(
        "help.text",
        bot_name=escape_html(client.config.bot_name),
        fun_commands=_fun_commands_block(),
    )
    await _safe_edit(query, text, help_keyboard(client.config, tr))
    await query.answer()


async def _safe_edit(query: CallbackQuery, text: str, markup) -> None:
    """Edit text or caption depending on the original message type."""
    message = query.message
    try:
        if getattr(message, "caption", None) is not None or message.media:
            await message.edit_caption(caption=text, reply_markup=markup)
        else:
            await message.edit_text(
                text, reply_markup=markup, disable_web_page_preview=True
            )
    except MessageNotModified:
        pass
    except RPCError as exc:
        log.debug("Could not edit help message: %s", exc)
        try:
            await message.reply_text(text, reply_markup=markup, quote=True)
        except RPCError:
            pass


@Client.on_message(filters.command(["stats", "status"]) & ~filters.via_bot, group=0)
@handle_errors
@anti_spam(period=5.0)
async def stats_command(client: Client, message: Message) -> None:
    """Owner-visible statistics (public users get a friendly refusal)."""
    if not client.is_owner(getattr(message.from_user, "id", None)):
        await message.reply_text(tr("errors.owner_only"), quote=True)
        return

    from core.helpers import format_duration

    stats = await client.db.stats()
    await message.reply_text(
        tr(
            "system.stats",
            bot_name=escape_html(client.config.bot_name),
            users=format_number(stats["users"]),
            active_users=format_number(stats["active_users"]),
            groups=format_number(stats["groups"]),
            active_groups=format_number(stats["active_groups"]),
            uptime=format_duration(client.uptime),
        ),
        quote=True,
    )
