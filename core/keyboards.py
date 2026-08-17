"""Inline keyboard construction with forward-compatible button styling.

Bot API 9.4 introduced background colors for keyboard/inline buttons. Support
for that field has to come from the client library, so this module *detects*
what the installed Pyrofork build accepts instead of assuming:

``_SUPPORTED_STYLE_KWARG`` is resolved once by inspecting
``InlineKeyboardButton.__init__``.  When a build exposes a colour/style
parameter it is passed through natively; on builds that do not (Pyrofork
2.3.69 and earlier), the style is expressed through the button label emoji so
the keyboard still renders correctly and **no unsupported parameter is ever
sent**.
"""

from __future__ import annotations

import inspect
import logging
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from core.helpers import is_valid_http_url

log = logging.getLogger(__name__)


class ButtonStyle(str, Enum):
    """Semantic button styles as described by Bot API 9.4."""

    DEFAULT = "default"
    PRIMARY = "primary"
    SUCCESS = "success"
    DANGER = "danger"


#: Bot API 9.4 background colour values, used only if the library accepts them.
_STYLE_COLORS: Dict[ButtonStyle, int] = {
    ButtonStyle.PRIMARY: 0x3390EC,
    ButtonStyle.SUCCESS: 0x4DB6AC,
    ButtonStyle.DANGER: 0xE15052,
}

#: Emoji fallback so styles remain visually distinct on clients/libraries
#: without colour support.
_STYLE_GLYPHS: Dict[ButtonStyle, str] = {
    ButtonStyle.PRIMARY: "🔷",
    ButtonStyle.SUCCESS: "✅",
    ButtonStyle.DANGER: "🔶",
    ButtonStyle.DEFAULT: "",
}


def _detect_style_kwarg() -> Optional[str]:
    """Find the styling parameter name supported by the installed Pyrofork."""
    try:
        params = inspect.signature(InlineKeyboardButton.__init__).parameters
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return None
    for candidate in ("background_color", "color", "style", "button_color"):
        if candidate in params:
            return candidate
    return None


_SUPPORTED_STYLE_KWARG: Optional[str] = _detect_style_kwarg()

if _SUPPORTED_STYLE_KWARG:
    log.info("Inline button styling enabled via '%s'", _SUPPORTED_STYLE_KWARG)
else:
    log.debug(
        "Installed Pyrofork exposes no button colour parameter; "
        "using emoji styling fallback."
    )


def styled_button(
    text: str,
    *,
    style: ButtonStyle = ButtonStyle.DEFAULT,
    url: Optional[str] = None,
    callback_data: Optional[str] = None,
    user_id: Optional[int] = None,
    copy_text: Optional[str] = None,
    switch_inline_query_current_chat: Optional[str] = None,
) -> Optional[InlineKeyboardButton]:
    """Create an inline button, applying styling only where it is supported.

    Returns ``None`` when a URL button was requested but the configured URL is
    empty or malformed.  Telegram rejects the *entire* markup with
    ``BUTTON_URL_INVALID`` if any button carries a blank/invalid URL, so an
    unconfigured link must drop its button rather than break the keyboard.
    ``build_markup`` filters these out.
    """
    if url is not None and not is_valid_http_url(url):
        if url.strip():
            log.warning("Dropping button %r: invalid URL %r", text, url)
        return None

    label = text
    if not _SUPPORTED_STYLE_KWARG:
        glyph = _STYLE_GLYPHS.get(style, "")
        if glyph and not text.strip().startswith(glyph):
            label = f"{glyph} {text}"

    kwargs: Dict[str, Any] = {"text": label}
    if url is not None:
        kwargs["url"] = url
    if callback_data is not None:
        kwargs["callback_data"] = callback_data
    if user_id is not None:
        kwargs["user_id"] = user_id
    if copy_text is not None:
        kwargs["copy_text"] = copy_text
    if switch_inline_query_current_chat is not None:
        kwargs["switch_inline_query_current_chat"] = switch_inline_query_current_chat

    if _SUPPORTED_STYLE_KWARG and style is not ButtonStyle.DEFAULT:
        color = _STYLE_COLORS.get(style)
        if color is not None:
            kwargs[_SUPPORTED_STYLE_KWARG] = color

    try:
        return InlineKeyboardButton(**kwargs)
    except TypeError as exc:
        # A library update changed the signature: drop styling and continue.
        log.warning("Falling back to plain inline button (%s)", exc)
        kwargs.pop(_SUPPORTED_STYLE_KWARG or "", None)
        return InlineKeyboardButton(**kwargs)


def build_markup(
    rows: Sequence[Sequence[Optional[InlineKeyboardButton]]],
) -> InlineKeyboardMarkup:
    """Build a markup, dropping unavailable buttons and any row left empty.

    ``styled_button`` returns ``None`` for URL buttons whose link is not
    configured; those are removed here so Telegram never sees an invalid URL.
    """
    cleaned: List[List[InlineKeyboardButton]] = []
    for row in rows:
        kept = [button for button in row if button is not None]
        if kept:
            cleaned.append(kept)
    return InlineKeyboardMarkup(cleaned)


def start_keyboard(config: Any, translator: Any) -> InlineKeyboardMarkup:
    """The /start keyboard.

    Layout::

        [ Support ] [ Update ]
        [        Help        ]
        [ Owner ]  [ Developer ]
    """
    t = translator
    return build_markup(
        [
            [
                styled_button(
                    t("buttons.support"), style=ButtonStyle.PRIMARY, url=config.support_url
                ),
                styled_button(
                    t("buttons.update"), style=ButtonStyle.PRIMARY, url=config.update_url
                ),
            ],
            [
                styled_button(
                    t("buttons.help"), style=ButtonStyle.SUCCESS, callback_data="help:open"
                ),
            ],
            [
                styled_button(t("buttons.owner"), style=ButtonStyle.DANGER, url=config.owner_url),
                styled_button(
                    t("buttons.developer"), style=ButtonStyle.DANGER, url=config.developer_url
                ),
            ],
        ]
    )


def help_keyboard(config: Any, translator: Any) -> InlineKeyboardMarkup:
    """Keyboard shown under /help."""
    t = translator
    return build_markup(
        [
            [
                styled_button(
                    t("buttons.fun"), style=ButtonStyle.PRIMARY, callback_data="help:fun"
                ),
                styled_button(
                    t("buttons.owner_commands"),
                    style=ButtonStyle.DANGER,
                    callback_data="help:owner",
                ),
            ],
            [
                styled_button(
                    t("buttons.support"), style=ButtonStyle.SUCCESS, url=config.support_url
                ),
            ],
            [
                styled_button(t("buttons.back"), callback_data="help:back"),
                styled_button(t("buttons.close"), style=ButtonStyle.DANGER, callback_data="help:close"),
            ],
        ]
    )


def help_section_keyboard(translator: Any) -> InlineKeyboardMarkup:
    t = translator
    return build_markup(
        [
            [
                styled_button(t("buttons.back"), style=ButtonStyle.PRIMARY, callback_data="help:open"),
                styled_button(t("buttons.close"), style=ButtonStyle.DANGER, callback_data="help:close"),
            ]
        ]
    )


def add_to_group_keyboard(bot_username: str, translator: Any) -> InlineKeyboardMarkup:
    t = translator
    return build_markup(
        [
            [
                styled_button(
                    t("buttons.add_to_group"),
                    style=ButtonStyle.SUCCESS,
                    url=f"https://t.me/{bot_username}?startgroup=true",
                )
            ]
        ]
    )
