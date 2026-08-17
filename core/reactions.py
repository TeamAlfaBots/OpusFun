"""Declarative registry of every GIF reaction command.

Adding a new reaction is a three-line change here plus a media folder and a
locale string — no new handler code:

>>> ReactionSpec(command="poke", folder="poke", message_key="poke")

The pipeline that consumes these specs lives in ``plugins/reactions.py``:

    command -> resolve target -> pick media -> render locale string -> send
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple


@dataclass(frozen=True, slots=True)
class ReactionSpec:
    """Configuration for a single reaction command."""

    #: Primary command name (without the leading slash).
    command: str
    #: Folder under ``assist/`` holding this reaction's media.
    folder: str
    #: Dotted key in the locale file, e.g. ``reactions.slap``.
    message_key: str
    #: Extra command aliases.
    aliases: Tuple[str, ...] = ()
    #: Emoji shown in the /help listing.
    emoji: str = "🎉"
    #: When ``False`` the command also works without replying to anyone
    #: (self-directed moods such as /sleeping or /goodnight).
    require_reply: bool = True
    #: Locale key used when the command is sent without a reply and
    #: ``require_reply`` is ``False``.
    solo_message_key: Optional[str] = None
    #: Restrict to groups (welcome only makes sense there).
    group_only: bool = False

    @property
    def commands(self) -> List[str]:
        return [self.command, *self.aliases]

    @property
    def folder_path(self) -> str:
        return self.folder


#: The 14 reaction commands required by the spec.
#: Wording is deliberately playful and non-graphic.
REACTIONS: Tuple[ReactionSpec, ...] = (
    ReactionSpec(command="slap", folder="slap", message_key="reactions.slap", emoji="🖐️"),
    ReactionSpec(command="hug", folder="hug", message_key="reactions.hug", emoji="🤗"),
    ReactionSpec(
        command="dance",
        folder="dance",
        message_key="reactions.dance",
        emoji="💃",
        require_reply=False,
        solo_message_key="reactions.dance_solo",
    ),
    ReactionSpec(
        command="marriage",
        folder="marriage",
        message_key="reactions.marriage",
        aliases=("marry",),
        emoji="💍",
    ),
    ReactionSpec(command="kill", folder="kill", message_key="reactions.kill", emoji="☠️"),
    ReactionSpec(
        command="beep",
        folder="beep",
        message_key="reactions.beep",
        aliases=("rona", "cry"),
        emoji="😭",
        require_reply=False,
        solo_message_key="reactions.beep_solo",
    ),
    ReactionSpec(
        command="laughing",
        folder="laughing",
        message_key="reactions.laughing",
        aliases=("laugh", "hasi"),
        emoji="😂",
        require_reply=False,
        solo_message_key="reactions.laughing_solo",
    ),
    ReactionSpec(
        command="perpose",
        folder="perpose",
        message_key="reactions.perpose",
        aliases=("propose",),
        emoji="💐",
    ),
    ReactionSpec(
        command="sleeping",
        folder="sleeping",
        message_key="reactions.sleeping",
        aliases=("sleep",),
        emoji="😴",
        require_reply=False,
        solo_message_key="reactions.sleeping_solo",
    ),
    ReactionSpec(
        command="goodnight",
        folder="goodnight",
        message_key="reactions.goodnight",
        aliases=("gn",),
        emoji="🌙",
        require_reply=False,
        solo_message_key="reactions.goodnight_solo",
    ),
    ReactionSpec(
        command="goodmorning",
        folder="goodmorning",
        message_key="reactions.goodmorning",
        aliases=("gm",),
        emoji="🌅",
        require_reply=False,
        solo_message_key="reactions.goodmorning_solo",
    ),
    ReactionSpec(
        command="welcome",
        folder="welcome",
        message_key="reactions.welcome",
        emoji="👋",
        require_reply=False,
        solo_message_key="reactions.welcome_solo",
        group_only=True,
    ),
    ReactionSpec(command="prank", folder="prank", message_key="reactions.prank", emoji="🃏"),
    ReactionSpec(command="fight", folder="fight", message_key="reactions.fight", emoji="🥊"),
    # Bonus reaction — the assist/kick folder ships with the project.
    # Purely a comedic "boot out of the chat" GIF; it never touches
    # Telegram's real ban/kick API.
    ReactionSpec(command="kick", folder="kick", message_key="reactions.kick", emoji="🦵"),
)

#: Folder used by /couple (not a reaction command, but part of the media set).
COUPLE_FOLDER = "cpl"

#: Optional shared folder for generic GIFs.
GENERIC_GIF_FOLDER = "gif"


def all_media_folders() -> List[str]:
    """Every folder the bot expects to exist under ``assist/``."""
    folders = [spec.folder for spec in REACTIONS]
    folders.extend([COUPLE_FOLDER, GENERIC_GIF_FOLDER])
    return list(dict.fromkeys(folders))


def build_registry(specs: Sequence[ReactionSpec] = REACTIONS) -> Dict[str, ReactionSpec]:
    """Map every command *and alias* to its spec, rejecting duplicates."""
    registry: Dict[str, ReactionSpec] = {}
    for spec in specs:
        for name in spec.commands:
            key = name.lower()
            if key in registry:
                raise ValueError(f"Duplicate reaction command registered: /{key}")
            registry[key] = spec
    return registry


REGISTRY: Dict[str, ReactionSpec] = build_registry()


def all_commands() -> List[str]:
    """Flat list of every command string handled by the reaction system."""
    return list(REGISTRY.keys())


def get_spec(command: str) -> Optional[ReactionSpec]:
    return REGISTRY.get(str(command).lstrip("/").lower())
