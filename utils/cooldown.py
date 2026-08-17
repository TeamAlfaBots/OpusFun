"""Cooldown primitives.

Two layers are used by OpusFun:

* :class:`MemoryCooldown` — a lightweight anti-spam throttle for high-frequency
  reaction commands. Losing it on restart is harmless, so it stays in memory.
* :class:`CoupleCooldown` — the persistent 6-hour ``/couple`` gate, backed by
  MongoDB so a restart cannot reset it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, Hashable, Tuple

from core.database import Database


class MemoryCooldown:
    """Per-key rate limiting using a monotonic clock (immune to clock skew)."""

    __slots__ = ("_period", "_hits", "_max_entries")

    def __init__(self, period: float = 3.0, max_entries: int = 20000) -> None:
        self._period = float(period)
        self._hits: Dict[Hashable, float] = {}
        self._max_entries = max_entries

    def check(self, key: Hashable) -> Tuple[bool, float]:
        """Return ``(allowed, remaining_seconds)`` and record the hit if allowed."""
        now = time.monotonic()
        last = self._hits.get(key)
        if last is not None:
            elapsed = now - last
            if elapsed < self._period:
                return False, self._period - elapsed

        if len(self._hits) >= self._max_entries:
            self._prune(now)
        self._hits[key] = now
        return True, 0.0

    def reset(self, key: Hashable) -> None:
        self._hits.pop(key, None)

    def _prune(self, now: float) -> None:
        stale = [key for key, ts in self._hits.items() if now - ts > self._period]
        for key in stale:
            self._hits.pop(key, None)
        if len(self._hits) >= self._max_entries:
            # Pathological case: drop the oldest half.
            ordered = sorted(self._hits.items(), key=lambda kv: kv[1])
            for key, _ in ordered[: len(ordered) // 2]:
                self._hits.pop(key, None)


@dataclass(slots=True)
class CooldownState:
    allowed: bool
    remaining: float = 0.0


class CoupleCooldown:
    """Persistent, per-chat cooldown for ``/couple`` stored in MongoDB."""

    def __init__(self, database: Database, cooldown_seconds: int) -> None:
        self._db = database
        self._seconds = int(cooldown_seconds)

    @property
    def seconds(self) -> int:
        return self._seconds

    async def acquire(self, chat_id: int) -> CooldownState:
        """Atomically claim the slot for ``chat_id``.

        Returns ``allowed=True`` only when the cooldown had expired; the
        timestamp is written in the same operation, so concurrent commands in
        one group cannot both pass.
        """
        acquired, remaining = await self._db.try_acquire_couple_cooldown(chat_id, self._seconds)
        return CooldownState(allowed=acquired, remaining=remaining)

    async def peek(self, chat_id: int) -> CooldownState:
        remaining = await self._db.couple_cooldown_remaining(chat_id, self._seconds)
        return CooldownState(allowed=remaining <= 0, remaining=remaining)

    async def release(self, chat_id: int) -> None:
        """Give the slot back when the announcement could not be delivered."""
        await self._db.release_couple_cooldown(chat_id)
