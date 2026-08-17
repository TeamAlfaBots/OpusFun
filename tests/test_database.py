"""Tests for the MongoDB layer, focused on the persistent couple cooldown."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from core.database import Database, utcnow
from utils.cooldown import CoupleCooldown

SIX_HOURS = 6 * 3600


# ------------------------------------------------------------------- users
async def test_save_user_upserts_and_reports_creation(database: Database):
    assert await database.save_user(1, "Ann", "ann") is True
    assert await database.save_user(1, "Ann Updated", "ann2") is False

    doc = await database.get_user(1)
    assert doc["first_name"] == "Ann Updated"
    assert doc["username"] == "ann2"
    assert doc["joined_at"] is not None
    assert doc["active"] is True


async def test_user_counting_and_iteration(database: Database):
    for uid in range(1, 6):
        await database.save_user(uid, f"User{uid}")
    assert await database.count_users() == 5

    await database.mark_user_inactive(3, reason="blocked")
    assert await database.count_users(active_only=True) == 4
    assert await database.count_users(active_only=False) == 5

    ids = [uid async for uid in database.iter_user_ids(active_only=True)]
    assert 3 not in ids and len(ids) == 4


# ------------------------------------------------------------------ groups
async def test_group_registration_and_deactivation(database: Database):
    assert await database.save_group(-100123, "My Group", "mygroup") is True
    assert await database.save_group(-100123, "Renamed") is False
    assert await database.count_groups() == 1

    await database.mark_group_inactive(-100123, reason="bot_removed")
    assert await database.count_groups(active_only=True) == 0
    assert await database.count_groups(active_only=False) == 1


async def test_bulk_mark_inactive(database: Database):
    for uid in range(1, 11):
        await database.save_user(uid)
    await database.bulk_mark_inactive("users", [1, 2, 3])
    assert await database.count_users(active_only=True) == 7


# ---------------------------------------------------------- couple cooldown
async def test_first_couple_use_is_allowed(database: Database):
    acquired, remaining = await database.try_acquire_couple_cooldown(-100, SIX_HOURS)
    assert acquired is True
    assert remaining == 0.0


async def test_second_use_is_blocked_with_remaining_time(database: Database):
    await database.try_acquire_couple_cooldown(-100, SIX_HOURS)
    acquired, remaining = await database.try_acquire_couple_cooldown(-100, SIX_HOURS)

    assert acquired is False
    assert 0 < remaining <= SIX_HOURS
    # Should be very close to the full window.
    assert remaining > SIX_HOURS - 10


async def test_cooldown_expires_after_the_window(database: Database):
    await database.try_acquire_couple_cooldown(-100, SIX_HOURS)
    # Simulate the passage of 6h 1m by rewriting the stored timestamp.
    await database.couple_cooldowns.update_one(
        {"chat_id": -100},
        {"$set": {"last_used": utcnow() - timedelta(hours=6, minutes=1)}},
    )
    acquired, _ = await database.try_acquire_couple_cooldown(-100, SIX_HOURS)
    assert acquired is True


async def test_cooldown_is_isolated_per_group(database: Database):
    assert (await database.try_acquire_couple_cooldown(-1, SIX_HOURS))[0] is True
    assert (await database.try_acquire_couple_cooldown(-2, SIX_HOURS))[0] is True
    assert (await database.try_acquire_couple_cooldown(-1, SIX_HOURS))[0] is False


async def test_cooldown_survives_a_simulated_restart(database: Database):
    """A fresh Database object over the same store must see the cooldown."""
    await database.try_acquire_couple_cooldown(-500, SIX_HOURS)

    restarted = Database("mongodb://mock", "opusfun_test", client=database._client)
    acquired, remaining = await restarted.try_acquire_couple_cooldown(-500, SIX_HOURS)
    assert acquired is False
    assert remaining > 0


async def test_concurrent_couple_requests_only_one_wins(database: Database):
    """Two simultaneous /couple calls in one group must not both succeed."""
    results = await asyncio.gather(
        *(database.try_acquire_couple_cooldown(-900, SIX_HOURS) for _ in range(8))
    )
    assert sum(1 for acquired, _ in results if acquired) == 1


async def test_release_restores_the_slot(database: Database):
    await database.try_acquire_couple_cooldown(-700, SIX_HOURS)
    await database.release_couple_cooldown(-700)
    acquired, _ = await database.try_acquire_couple_cooldown(-700, SIX_HOURS)
    assert acquired is True


async def test_remaining_time_is_timezone_safe(database: Database):
    """Stored timestamps must be interpreted as UTC even if returned naive."""
    await database.try_acquire_couple_cooldown(-800, SIX_HOURS)
    doc = await database.couple_cooldowns.find_one({"chat_id": -800})
    naive = doc["last_used"].replace(tzinfo=None)
    await database.couple_cooldowns.update_one(
        {"chat_id": -800}, {"$set": {"last_used": naive}}
    )
    remaining = await database.couple_cooldown_remaining(-800, SIX_HOURS)
    # Would be wildly wrong (or raise) if the naive value were mishandled.
    assert SIX_HOURS - 60 < remaining <= SIX_HOURS


# ------------------------------------------------------- CoupleCooldown API
async def test_couple_cooldown_wrapper(database: Database):
    cooldown = CoupleCooldown(database, SIX_HOURS)

    first = await cooldown.acquire(-42)
    assert first.allowed is True

    second = await cooldown.acquire(-42)
    assert second.allowed is False
    assert second.remaining > 0

    peek = await cooldown.peek(-42)
    assert peek.allowed is False

    await cooldown.release(-42)
    assert (await cooldown.acquire(-42)).allowed is True


# ------------------------------------------------------------------ couples
async def test_couple_history_round_trip(database: Database):
    await database.record_couple(-1, {"id": 1, "name": "Ann"}, {"id": 2, "name": "Bob"})
    stored = await database.get_last_couple(-1)
    assert stored["first"]["name"] == "Ann"
    assert stored["second"]["id"] == 2


async def test_stats_aggregates(database: Database):
    await database.save_user(1)
    await database.save_group(-1)
    stats = await database.stats()
    assert stats["users"] == 1 and stats["groups"] == 1
