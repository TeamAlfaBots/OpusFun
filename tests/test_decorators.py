"""Tests for the decorator stack: authorisation, scope, throttling, errors."""

from __future__ import annotations

import asyncio

import pytest
from pyrogram.errors import ChatWriteForbidden, FloodWait

from utils.decorators import anti_spam, group_only, handle_errors, owner_only, private_only

from test_handlers import FakeChat, FakeClient, FakeMessage, FakeUser  # noqa: E402


@pytest.fixture
def client(database, translator, media_pool, config):
    return FakeClient(database, translator, media_pool, config)


# ------------------------------------------------------------- owner_only
async def test_owner_only_allows_every_configured_owner(client, config):
    calls = []

    @owner_only
    async def h(c, m):
        calls.append(m.from_user.id)

    for owner_id in config.owner_ids:
        await h(client, FakeMessage(user=FakeUser(owner_id)))

    assert calls == list(config.owner_ids)
    assert len(config.owner_ids) >= 2, "multi-owner support must be exercised"


async def test_owner_only_blocks_strangers(client, translator):
    calls = []

    @owner_only
    async def h(c, m):
        calls.append(1)

    msg = FakeMessage(user=FakeUser(4040404))
    await h(client, msg)

    assert not calls, "non-owner reached an owner-only handler"
    assert "missing string" not in msg.last_text


async def test_owner_only_blocks_anonymous_sender(client):
    calls = []

    @owner_only
    async def h(c, m):
        calls.append(1)

    msg = FakeMessage()
    msg.from_user = None
    await h(client, msg)
    assert not calls


# -------------------------------------------------------------- chat scope
async def test_group_only_rejects_private_chats(client):
    calls = []

    @group_only
    async def h(c, m):
        calls.append(1)

    await h(client, FakeMessage(chat=FakeChat(7, "private")))
    assert not calls

    await h(client, FakeMessage(chat=FakeChat(-100, "supergroup")))
    assert calls == [1]


async def test_private_only_rejects_groups(client):
    calls = []

    @private_only
    async def h(c, m):
        calls.append(1)

    await h(client, FakeMessage(chat=FakeChat(-100, "supergroup")))
    assert not calls


# -------------------------------------------------------------- anti_spam
async def test_anti_spam_throttles_repeat_calls(client):
    calls = []

    @anti_spam(period=60.0)
    async def h(c, m):
        calls.append(1)

    user, chat = FakeUser(1), FakeChat(-5)
    for _ in range(4):
        await h(client, FakeMessage(user=user, chat=chat))

    assert len(calls) == 1, "throttle let a burst through"


async def test_anti_spam_is_per_user_and_per_chat(client):
    calls = []

    @anti_spam(period=60.0)
    async def h(c, m):
        calls.append(1)

    await h(client, FakeMessage(user=FakeUser(1), chat=FakeChat(-5)))
    await h(client, FakeMessage(user=FakeUser(2), chat=FakeChat(-5)))   # other user
    await h(client, FakeMessage(user=FakeUser(1), chat=FakeChat(-6)))   # other chat

    assert len(calls) == 3


async def test_anti_spam_allows_again_after_the_period(client):
    calls = []

    @anti_spam(period=0.05)
    async def h(c, m):
        calls.append(1)

    msg = FakeMessage(user=FakeUser(1), chat=FakeChat(-7))
    await h(client, msg)
    await asyncio.sleep(0.08)
    await h(client, msg)

    assert len(calls) == 2


# ----------------------------------------------------------- handle_errors
async def test_handle_errors_swallows_unexpected_exceptions(client):
    @handle_errors
    async def h(c, m):
        raise ValueError("boom")

    msg = FakeMessage()
    assert await h(client, msg) is None      # must not propagate
    assert msg.sent, "user was not told anything went wrong"


async def test_handle_errors_retries_short_floodwait(client):
    attempts = []

    @handle_errors
    async def h(c, m):
        attempts.append(1)
        if len(attempts) == 1:
            raise FloodWait(value=0)
        return "ok"

    assert await h(client, FakeMessage()) == "ok"
    assert len(attempts) == 2


async def test_handle_errors_stays_silent_on_lost_permissions(client):
    @handle_errors
    async def h(c, m):
        raise ChatWriteForbidden()

    msg = FakeMessage()
    assert await h(client, msg) is None
    # Cannot write in the chat, so it must not try to reply.
    assert msg.sent == []


async def test_handle_errors_never_swallows_cancellation(client):
    @handle_errors
    async def h(c, m):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await h(client, FakeMessage())
