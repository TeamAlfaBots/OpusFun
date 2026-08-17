"""The OpusFun Pyrofork client and its lifecycle."""

from __future__ import annotations

import logging
import time
from typing import Optional

from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.errors import RPCError
from pyrogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
)

from config import Config, get_config
from core.database import Database, set_database
from core.i18n import Translator, get_translator, set_translator
from core.reactions import REACTIONS, all_media_folders
from utils.cooldown import CoupleCooldown
from utils.random_media import MediaPool, set_media_pool

log = logging.getLogger(__name__)


class OpusFun(Client):
    """Pyrofork client wired up with the bot's services.

    Services (database, translator, media pool, cooldown) are attached to the
    client instance so plugins can reach them through ``client.db`` etc.
    without importing globals or opening new connections per command.
    """

    def __init__(self, config: Optional[Config] = None) -> None:
        self.config: Config = config or get_config()

        super().__init__(
            name="opusfun",
            api_id=self.config.api_id,
            api_hash=self.config.api_hash,
            bot_token=self.config.bot_token,
            workers=self.config.workers,
            workdir=str(self.config.base_dir),
            parse_mode=ParseMode.HTML,
            plugins={"root": "plugins"},
            sleep_threshold=60,
            max_concurrent_transmissions=4,
        )

        self.db: Database = Database(self.config.mongo_uri, self.config.mongo_db_name)
        self.translator: Translator = Translator(
            self.config.locales_dir, self.config.default_language
        )
        self.media: MediaPool = MediaPool(self.config.assets_dir, self.config.media_cache_ttl)
        self.couple_cooldown: CoupleCooldown = CoupleCooldown(
            self.db, self.config.couple_cooldown_seconds
        )

        self.start_time: float = time.monotonic()
        self.bot_username: str = ""
        self.bot_id: int = 0

        # Publish the services as module singletons for helpers such as
        # ``tr()`` that are called from deep inside utility code.
        set_database(self.db)
        set_translator(self.translator)
        set_media_pool(self.media)

    # ------------------------------------------------------------- lifecycle
    async def start(self) -> None:  # type: ignore[override]
        log.info("Starting %s …", self.config.bot_name)

        await self.db.connect()

        self.media.ensure_folders(all_media_folders())
        report = await self.media.warmup(all_media_folders())
        total = sum(report.values())
        empty = [name for name, count in report.items() if count == 0]
        log.info("Media warm-up complete: %d file(s) across %d folder(s)", total, len(report))
        if empty:
            log.warning(
                "These media folders are empty and their commands will reply with a "
                "friendly notice until you add GIFs: %s",
                ", ".join(sorted(empty)),
            )

        await super().start()

        me = await self.get_me()
        self.bot_id = me.id
        self.bot_username = me.username or ""
        self.start_time = time.monotonic()

        await self._publish_commands()

        log.info(
            "%s is online as @%s (id=%s) with %d reaction command(s)",
            self.config.bot_name,
            self.bot_username,
            self.bot_id,
            len(REACTIONS),
        )
        await self._notify_owners_startup()

    async def stop(self, *args, **kwargs) -> None:  # type: ignore[override]
        log.info("Stopping %s …", self.config.bot_name)
        try:
            await super().stop()
        except (ConnectionError, RPCError) as exc:
            log.warning("Error while stopping Telegram client: %s", exc)
        finally:
            await self.db.close()
        log.info("%s stopped cleanly", self.config.bot_name)

    # --------------------------------------------------------------- helpers
    @property
    def uptime(self) -> float:
        return time.monotonic() - self.start_time

    def is_owner(self, user_id: int | None) -> bool:
        """Centralised owner check used by every owner-only command."""
        return self.config.is_owner(user_id)

    async def _publish_commands(self) -> None:
        """Register the command list shown in Telegram's UI."""
        base = [
            BotCommand("start", "Start the bot and see the intro"),
            BotCommand("help", "Show every available command"),
            BotCommand("ping", "Check that the bot is alive"),
        ]
        group_commands = base + [BotCommand("couple", "Pick a random couple of the moment")]
        for spec in REACTIONS:
            group_commands.append(
                BotCommand(spec.command, f"{spec.emoji} {spec.command.capitalize()} reaction")
            )

        try:
            await self.set_bot_commands(base, scope=BotCommandScopeAllPrivateChats())
            # Telegram caps the command list at 100 entries.
            await self.set_bot_commands(
                group_commands[:100], scope=BotCommandScopeAllGroupChats()
            )
            log.info("Published %d group command(s) to Telegram", len(group_commands[:100]))
        except RPCError as exc:
            log.warning("Could not publish bot commands: %s", exc)

    async def _notify_owners_startup(self) -> None:
        """Ping owners that the bot restarted; failures are non-fatal."""
        text = self.translator.get(
            "system.started",
            bot_name=self.config.bot_name,
            username=self.bot_username or "unknown",
        )
        for owner_id in self.config.owner_ids:
            try:
                await self.send_message(owner_id, text, disable_web_page_preview=True)
            except RPCError as exc:
                # Owner has not started the bot yet — perfectly normal.
                log.debug("Could not send startup notice to owner %s: %s", owner_id, exc)


def get_client_translator() -> Translator:
    return get_translator()
