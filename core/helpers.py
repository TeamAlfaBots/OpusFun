"""Shared helpers: HTML escaping, user mentions, formatting and validation."""

from __future__ import annotations

import html
import re
from typing import Any, Optional

from pyrogram.types import Chat, User

#: Telegram rejects messages longer than 4096 characters (1024 for captions).
MAX_MESSAGE_LENGTH = 4096
MAX_CAPTION_LENGTH = 1024

_WHITESPACE_RE = re.compile(r"\s+")


def escape_html(text: Any) -> str:
    """Escape a value so it is safe to embed inside Telegram HTML.

    ``html.escape`` with ``quote=True`` covers ``& < > " '`` which is exactly
    what Telegram's HTML parser cares about.  Every user-controlled string
    (first names, usernames, chat titles, custom text) must pass through here.
    """
    if text is None:
        return ""
    return html.escape(str(text), quote=True)


def clean_name(name: Any, fallback: str = "Someone", limit: int = 64) -> str:
    """Normalise a display name: collapse whitespace, trim, apply a fallback."""
    if name is None:
        return fallback
    collapsed = _WHITESPACE_RE.sub(" ", str(name)).strip()
    if not collapsed:
        return fallback
    if len(collapsed) > limit:
        collapsed = collapsed[: limit - 1].rstrip() + "…"
    return collapsed


def user_display_name(user: Optional[User], fallback: str = "Someone") -> str:
    """Best-effort human readable name for a user (unescaped)."""
    if user is None:
        return fallback
    first = getattr(user, "first_name", None) or ""
    last = getattr(user, "last_name", None) or ""
    full = f"{first} {last}".strip()
    if not full:
        full = getattr(user, "username", None) or fallback
    return clean_name(full, fallback)


def mention_user(user: Optional[User], fallback: str = "Someone") -> str:
    """Return an HTML mention link for a user, safely escaped.

    ``tg://user?id=`` works for every user, including those without a username
    and those who hide their account from search.
    """
    if user is None:
        return escape_html(fallback)
    name = escape_html(user_display_name(user, fallback))
    user_id = getattr(user, "id", None)
    if not user_id:
        return name
    return f'<a href="tg://user?id={int(user_id)}">{name}</a>'


def mention_id(user_id: int, name: str) -> str:
    """Build a mention from a raw id + name pair."""
    return f'<a href="tg://user?id={int(user_id)}">{escape_html(clean_name(name))}</a>'


def chat_display_title(chat: Optional[Chat], fallback: str = "this chat") -> str:
    if chat is None:
        return fallback
    title = getattr(chat, "title", None) or getattr(chat, "first_name", None)
    return clean_name(title, fallback, limit=128)


def format_duration(seconds: float) -> str:
    """Human friendly duration: ``4h 27m``, ``12m 30s``, ``45s``."""
    total = int(max(0, round(seconds)))
    if total < 60:
        return f"{total}s"

    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and not days:
        parts.append(f"{minutes}m")
    if secs and not days and not hours:
        parts.append(f"{secs}s")
    return " ".join(parts) or "0s"


def format_number(value: int) -> str:
    """``1245`` -> ``1,245`` for readable reports."""
    return f"{int(value):,}"


def truncate(text: str, limit: int = MAX_MESSAGE_LENGTH) -> str:
    """Hard-limit outgoing text so Telegram never rejects the message."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def is_group_chat(chat: Optional[Chat]) -> bool:
    from pyrogram.enums import ChatType

    if chat is None:
        return False
    return getattr(chat, "type", None) in {ChatType.GROUP, ChatType.SUPERGROUP}


def is_valid_http_url(url: str | None) -> bool:
    """Light validation for configured URLs / remote media links."""
    if not url or not isinstance(url, str):
        return False
    return url.startswith(("http://", "https://")) and len(url) > 10 and " " not in url
