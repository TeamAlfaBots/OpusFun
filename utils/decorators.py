"""Reusable handler decorators: error capture, authorisation, throttling.

Every plugin handler is wrapped by :func:`handle_errors`, which converts the
long tail of Telegram RPC problems (FloodWait, lost permissions, deleted
peers…) into logged, user-friendly outcomes rather than tracebacks that would
kill the update task.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any, Awaitable, Callable, Optional

from pyrogram import Client
from pyrogram.enums import ChatType
from pyrogram.errors import (
    ChatWriteForbidden,
    FloodWait,
    MessageDeleteForbidden,
    PeerIdInvalid,
    RPCError,
    SlowmodeWait,
    UserIsBlocked,
)
from pyrogram.types import CallbackQuery, Message

from config import get_config
from core.i18n import tr
from utils.cooldown import MemoryCooldown

log = logging.getLogger(__name__)

Handler = Callable[..., Awaitable[Any]]

#: Global anti-spam guard: one reaction command per user per chat every 2.5s.
_spam_guard = MemoryCooldown(period=2.5)

#: Maximum FloodWait we will transparently sleep through inside a handler.
MAX_INLINE_FLOOD_WAIT = 30


def _extract_update(args: tuple[Any, ...]) -> Optional[Message | CallbackQuery]:
    for arg in args:
        if isinstance(arg, (Message, CallbackQuery)):
            return arg
    return None


async def _safe_notify(update: Message | CallbackQuery | None, text: str) -> None:
    """Best-effort user feedback that never raises."""
    if update is None:
        return
    try:
        if isinstance(update, CallbackQuery):
            await update.answer(text[:200], show_alert=True)
        else:
            await update.reply_text(text, quote=True)
    except RPCError as exc:
        log.debug("Could not deliver notification to user: %s", exc)
    except Exception:  # pragma: no cover - defensive
        log.debug("Unexpected failure while notifying user", exc_info=True)


def handle_errors(func: Handler) -> Handler:
    """Catch and classify everything a handler can throw."""

    @functools.wraps(func)
    async def wrapper(client: Client, update: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return await func(client, update, *args, **kwargs)

        except FloodWait as exc:
            wait = int(getattr(exc, "value", 0) or 0)
            log.warning("FloodWait for %ss in %s", wait, func.__name__)
            if wait <= MAX_INLINE_FLOOD_WAIT:
                await asyncio.sleep(wait + 1)
                try:
                    return await func(client, update, *args, **kwargs)
                except RPCError as retry_exc:
                    log.warning("Retry after FloodWait failed in %s: %s", func.__name__, retry_exc)
            return None

        except SlowmodeWait as exc:
            log.info("Slowmode active (%ss) in %s", getattr(exc, "value", "?"), func.__name__)
            return None

        except (ChatWriteForbidden, UserIsBlocked, MessageDeleteForbidden) as exc:
            log.info("Missing permission in %s: %s", func.__name__, type(exc).__name__)
            return None

        except PeerIdInvalid:
            log.info("Invalid peer encountered in %s", func.__name__)
            return None

        except RPCError as exc:
            log.error("Telegram RPC error in %s: %s", func.__name__, exc, exc_info=True)
            await _safe_notify(_extract_update((update,) + args), tr("errors.telegram"))
            return None

        except asyncio.CancelledError:
            raise

        except Exception:
            log.exception("Unhandled exception in handler %s", func.__name__)
            await _safe_notify(_extract_update((update,) + args), tr("errors.generic"))
            return None

    return wrapper


def owner_only(func: Handler) -> Handler:
    """Restrict a handler to the configured owners (multi-owner aware)."""

    @functools.wraps(func)
    async def wrapper(client: Client, message: Message, *args: Any, **kwargs: Any) -> Any:
        user = getattr(message, "from_user", None)
        if user is None or not get_config().is_owner(user.id):
            log.info(
                "Rejected owner-only command %s from user %s",
                func.__name__,
                getattr(user, "id", "unknown"),
            )
            await message.reply_text(tr("errors.owner_only"), quote=True)
            return None
        return await func(client, message, *args, **kwargs)

    return wrapper


def group_only(func: Handler) -> Handler:
    """Restrict a handler to groups and supergroups."""

    @functools.wraps(func)
    async def wrapper(client: Client, message: Message, *args: Any, **kwargs: Any) -> Any:
        chat = getattr(message, "chat", None)
        if chat is None or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
            await message.reply_text(tr("errors.group_only"), quote=True)
            return None
        return await func(client, message, *args, **kwargs)

    return wrapper


def private_only(func: Handler) -> Handler:
    @functools.wraps(func)
    async def wrapper(client: Client, message: Message, *args: Any, **kwargs: Any) -> Any:
        chat = getattr(message, "chat", None)
        if chat is None or chat.type != ChatType.PRIVATE:
            await message.reply_text(tr("errors.private_only"), quote=True)
            return None
        return await func(client, message, *args, **kwargs)

    return wrapper


def anti_spam(period: float = 2.5, silent: bool = True) -> Callable[[Handler], Handler]:
    """Throttle a command per (chat, user) pair to protect against flooding."""

    guard = _spam_guard if period == 2.5 else MemoryCooldown(period=period)

    def decorator(func: Handler) -> Handler:
        @functools.wraps(func)
        async def wrapper(client: Client, message: Message, *args: Any, **kwargs: Any) -> Any:
            user = getattr(message, "from_user", None)
            chat = getattr(message, "chat", None)
            if user is not None and chat is not None:
                key = (chat.id, user.id, func.__name__)
                allowed, remaining = guard.check(key)
                if not allowed:
                    log.debug("Throttled %s for user %s (%.1fs left)", func.__name__, user.id, remaining)
                    if not silent:
                        await message.reply_text(
                            tr("errors.too_fast", seconds=int(remaining) + 1), quote=True
                        )
                    return None
            return await func(client, message, *args, **kwargs)

        return wrapper

    return decorator
