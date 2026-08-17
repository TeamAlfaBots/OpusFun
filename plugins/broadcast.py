"""Owner-only broadcast.

The owner replies to any message — text, photo, video, document, animation,
audio, sticker, voice … — and it is copied to every registered user and group.

Design notes
------------
* ``copy_message`` is used so the broadcast is not tagged "Forwarded from" and
  preserves media and captions; ``-f``/``--forward`` opts into real forwarding.
* Delivery runs through a bounded worker pool (``BROADCAST_CONCURRENCY``) with
  a small inter-send delay, which keeps throughput high without tripping
  Telegram's flood limits.
* Every failure is classified: permanent ones (blocked, deleted, kicked)
  deactivate the record so later broadcasts skip them.
* One failing destination can never abort the run.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import List, Literal, Optional, Tuple

from pyrogram import Client, filters
from pyrogram.errors import (
    ChannelPrivate,
    ChatWriteForbidden,
    FloodWait,
    InputUserDeactivated,
    PeerIdInvalid,
    RPCError,
    SlowmodeWait,
    UserIsBlocked,
)
from pyrogram.types import Message

from core.helpers import escape_html, format_duration, format_number
from core.i18n import tr
from utils.decorators import handle_errors, owner_only

log = logging.getLogger(__name__)

Destination = Tuple[Literal["user", "group"], int]

#: Errors meaning the destination is gone for good.
_PERMANENT_ERRORS = (
    UserIsBlocked,
    InputUserDeactivated,
    PeerIdInvalid,
    ChatWriteForbidden,
    ChannelPrivate,
)

#: Progress edit interval (seconds) — avoids spamming editMessageText.
_PROGRESS_INTERVAL = 6.0

#: A single broadcast at a time; a second attempt is rejected politely.
_broadcast_lock = asyncio.Lock()


@dataclass
class BroadcastStats:
    success: int = 0
    failed: int = 0
    skipped: int = 0
    flood_waits: int = 0
    dead_users: List[int] = field(default_factory=list)
    dead_groups: List[int] = field(default_factory=list)
    started_at: float = field(default_factory=time.monotonic)

    @property
    def total(self) -> int:
        return self.success + self.failed + self.skipped

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at


async def _deliver(
    client: Client,
    source: Message,
    kind: str,
    chat_id: int,
    stats: BroadcastStats,
    *,
    forward: bool,
    pin: bool,
) -> None:
    """Send one copy, retrying once through a FloodWait."""
    for attempt in range(2):
        try:
            if forward:
                sent = await source.forward(chat_id)
            else:
                sent = await source.copy(chat_id)

            stats.success += 1

            if pin and sent is not None:
                try:
                    await client.pin_chat_message(
                        chat_id, sent.id, disable_notification=True
                    )
                except RPCError:
                    pass  # Pinning is a bonus, not a requirement.
            return

        except FloodWait as exc:
            wait = int(getattr(exc, "value", 0) or 0)
            stats.flood_waits += 1
            log.warning("Broadcast FloodWait %ss (destination %s)", wait, chat_id)
            if wait > 300 or attempt == 1:
                stats.failed += 1
                return
            await asyncio.sleep(wait + 1)
            continue

        except SlowmodeWait as exc:
            wait = int(getattr(exc, "value", 0) or 0)
            if wait > 60 or attempt == 1:
                stats.skipped += 1
                return
            await asyncio.sleep(wait + 1)
            continue

        except _PERMANENT_ERRORS as exc:
            stats.failed += 1
            if kind == "user":
                stats.dead_users.append(chat_id)
            else:
                stats.dead_groups.append(chat_id)
            log.debug("Permanent broadcast failure for %s: %s", chat_id, type(exc).__name__)
            return

        except RPCError as exc:
            stats.failed += 1
            log.debug("Broadcast RPC failure for %s: %s", chat_id, exc)
            return

        except Exception:
            stats.failed += 1
            log.exception("Unexpected broadcast failure for %s", chat_id)
            return


async def _collect_destinations(
    client: Client, *, users: bool, groups: bool
) -> List[Destination]:
    destinations: List[Destination] = []
    if users:
        async for user_id in client.db.iter_user_ids(active_only=True):
            destinations.append(("user", user_id))
    if groups:
        async for chat_id in client.db.iter_group_ids(active_only=True):
            destinations.append(("group", chat_id))
    return destinations


def _parse_flags(message: Message) -> dict:
    """Parse optional flags from the command line."""
    args = [a.lower() for a in (getattr(message, "command", []) or [])[1:]]
    users_only = any(a in ("-u", "--users") for a in args)
    groups_only = any(a in ("-g", "--groups") for a in args)
    return {
        "forward": any(a in ("-f", "--forward") for a in args),
        "pin": any(a in ("-p", "--pin") for a in args),
        "users": not groups_only or users_only,
        "groups": not users_only or groups_only,
    }


@Client.on_message(filters.command(["broadcast", "gcast"]) & ~filters.via_bot, group=0)
@handle_errors
@owner_only
async def broadcast_command(client: Client, message: Message) -> None:
    source: Optional[Message] = getattr(message, "reply_to_message", None)
    if source is None:
        await message.reply_text(tr("broadcast.no_reply"), quote=True)
        return

    if _broadcast_lock.locked():
        await message.reply_text(tr("broadcast.already_running"), quote=True)
        return

    async with _broadcast_lock:
        flags = _parse_flags(message)
        destinations = await _collect_destinations(
            client, users=flags["users"], groups=flags["groups"]
        )

        if not destinations:
            await message.reply_text(tr("broadcast.no_targets"), quote=True)
            return

        stats = BroadcastStats()
        total = len(destinations)
        status = await message.reply_text(
            tr("broadcast.started", total=format_number(total)), quote=True
        )
        log.info("Broadcast started by %s to %d destination(s)", message.from_user.id, total)

        semaphore = asyncio.Semaphore(client.config.broadcast_concurrency)
        delay = client.config.broadcast_sleep
        progress_state = {"last": time.monotonic()}

        async def worker(dest: Destination) -> None:
            kind, chat_id = dest
            async with semaphore:
                await _deliver(
                    client,
                    source,
                    kind,
                    chat_id,
                    stats,
                    forward=flags["forward"],
                    pin=flags["pin"],
                )
                if delay:
                    await asyncio.sleep(delay)

            now = time.monotonic()
            if now - progress_state["last"] >= _PROGRESS_INTERVAL:
                progress_state["last"] = now
                await _update_progress(status, stats, total)

        # ``return_exceptions=True`` guarantees the gather itself cannot blow up.
        results = await asyncio.gather(
            *(worker(dest) for dest in destinations), return_exceptions=True
        )
        for result in results:
            if isinstance(result, Exception):
                log.error("Broadcast worker crashed: %s", result)

        # Persist the destinations that are permanently unreachable.
        await client.db.bulk_mark_inactive("users", stats.dead_users)
        await client.db.bulk_mark_inactive("groups", stats.dead_groups)

        report = tr(
            "broadcast.completed",
            success=format_number(stats.success),
            failed=format_number(stats.failed),
            skipped=format_number(stats.skipped),
            total=format_number(total),
            elapsed=format_duration(stats.elapsed),
            flood_waits=format_number(stats.flood_waits),
        )
        try:
            await status.edit_text(report)
        except RPCError:
            await message.reply_text(report, quote=True)

        log.info(
            "Broadcast finished: %d ok, %d failed, %d skipped in %.1fs (%d flood waits)",
            stats.success,
            stats.failed,
            stats.skipped,
            stats.elapsed,
            stats.flood_waits,
        )


async def _update_progress(status: Message, stats: BroadcastStats, total: int) -> None:
    """Edit the status message; failures here are irrelevant to the run."""
    try:
        await status.edit_text(
            tr(
                "broadcast.progress",
                done=format_number(stats.total),
                total=format_number(total),
                success=format_number(stats.success),
                failed=format_number(stats.failed),
            )
        )
    except (RPCError, Exception):  # noqa: B014 - progress must never raise
        pass


@Client.on_message(filters.command("cleanup") & ~filters.via_bot, group=0)
@handle_errors
@owner_only
async def cleanup_command(client: Client, message: Message) -> None:
    """Report how many records are marked inactive (broadcast hygiene)."""
    stats = await client.db.stats()
    await message.reply_text(
        tr(
            "broadcast.cleanup",
            users=format_number(stats["users"]),
            active_users=format_number(stats["active_users"]),
            dead_users=format_number(stats["users"] - stats["active_users"]),
            groups=format_number(stats["groups"]),
            active_groups=format_number(stats["active_groups"]),
            dead_groups=format_number(stats["groups"] - stats["active_groups"]),
        ),
        quote=True,
    )
