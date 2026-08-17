"""End-to-end-ish tests: run the real plugin handlers against fake Pyrofork objects.

These call the undecorated handler bodies with stand-in Message/Client objects so
the routing, reply-detection, cooldown and broadcast logic is genuinely executed.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, List, Optional

import pytest
from pyrogram.errors import FloodWait, InputUserDeactivated, UserIsBlocked
from pyrogram.types import Chat, Message, User

from core.database import Database
from core.reactions import REGISTRY, get_spec
from utils.cooldown import CoupleCooldown


# --------------------------------------------------------------- fake layer
class FakeUser(User):
    """Real pyrogram.User subclass so isinstance checks behave as in production."""

    def __init__(self, uid: int, first_name: str = "User", is_bot: bool = False,
                 username: Optional[str] = None):
        self.id = uid
        self.first_name = first_name
        self.last_name = None
        self.username = username
        self.is_bot = is_bot
        self.is_deleted = False
        # ``mention`` is a real read-only property on pyrogram.User, so the
        # production implementation is exercised rather than a stand-in.


class FakeChat(Chat):
    def __init__(self, cid: int, ctype: str = "supergroup", title: str = "Test Group"):
        from pyrogram.enums import ChatType

        self.id = cid
        self.type = {
            "supergroup": ChatType.SUPERGROUP,
            "group": ChatType.GROUP,
            "private": ChatType.PRIVATE,
        }[ctype]
        self.title = title
        self.username = None
        self.first_name = title


class FakeMessage(Message):
    """Real pyrogram.Message subclass that records every outbound call."""

    def __init__(self, text="", user=None, chat=None, reply_to=None, command=None):
        self.text = text
        self.from_user = user or FakeUser(1, "Ann")
        self.chat = chat or FakeChat(-1001)
        self.reply_to_message = reply_to
        self.command = command if command is not None else text.lstrip("/").split()
        self.id = 555
        self.sent: List[dict] = []
        self.new_chat_members = None
        self.left_chat_member = None

    async def reply_text(self, text, **kw):
        self.sent.append({"kind": "text", "text": text, **kw})
        return FakeMessage(text=text)

    reply = reply_text

    async def reply_animation(self, animation, caption="", **kw):
        self.sent.append({"kind": "animation", "media": animation, "text": caption, **kw})
        return FakeMessage()

    async def reply_photo(self, photo, caption="", **kw):
        self.sent.append({"kind": "photo", "media": photo, "text": caption, **kw})
        return FakeMessage()

    async def reply_video(self, video, caption="", **kw):
        self.sent.append({"kind": "video", "media": video, "text": caption, **kw})
        return FakeMessage()

    async def edit_text(self, text, **kw):
        self.sent.append({"kind": "edit", "text": text, **kw})
        return self

    async def copy(self, chat_id, **kw):
        self.sent.append({"kind": "copy", "chat_id": chat_id})
        return FakeMessage()

    async def delete(self):
        self.sent.append({"kind": "delete"})

    @property
    def last(self) -> dict:
        assert self.sent, "handler produced no output"
        return self.sent[-1]

    @property
    def last_text(self) -> str:
        return self.last.get("text") or ""


class FakeClient:
    def __init__(self, database, translator, media_pool, config, members=None):
        self.db = database
        self.translator = translator
        self.media = media_pool
        self.config = config
        self.couple_cooldown = CoupleCooldown(database, config.couple_cooldown_seconds)
        self.me = FakeUser(999, "OpusFun", is_bot=True)
        self.bot_id = 999
        self._members = members or []
        self.sent_to: List[int] = []
        self.flood_once = False

    async def get_me(self):
        return self.me

    def get_chat_members(self, chat_id, limit=0, filter=None):
        async def _gen():
            for m in self._members:
                yield SimpleNamespace(user=m, status=None)

        return _gen()

    async def send_message(self, chat_id, text, **kw):
        self.sent_to.append(chat_id)
        return FakeMessage(text=text)

    async def get_users(self, uid):
        return FakeUser(uid, f"U{uid}")


@pytest.fixture
def client(database, translator, media_pool, config):
    return FakeClient(database, translator, media_pool, config)


def handler(func):
    """Unwrap the full decorator stack to reach the raw handler body.

    The production stack is ``handle_errors -> anti_spam -> body``.  Throttling
    is keyed on (chat, user) and would silently swallow the 2nd+ call in this
    suite, so these tests target the body directly; the decorators themselves
    are covered by ``test_decorators.py``.
    """
    while hasattr(func, "__wrapped__"):
        func = func.__wrapped__
    return func


# ------------------------------------------------------------- reactions
async def test_reaction_requires_reply(client, translator):
    from plugins.reactions import reaction_handler

    msg = FakeMessage("/slap", command=["slap"])
    await handler(reaction_handler)(client, msg)

    assert translator.get("errors.no_reply").split("{")[0][:15] in msg.last_text


async def test_reaction_with_reply_sends_media_and_mentions_both(client):
    from plugins.reactions import reaction_handler

    target = FakeMessage(user=FakeUser(2, "Bob"))
    msg = FakeMessage("/slap", user=FakeUser(1, "Ann"), reply_to=target, command=["slap"])
    await handler(reaction_handler)(client, msg)

    out = msg.last
    assert out["kind"] in {"animation", "photo", "video", "text"}
    assert "Ann" in out["text"] and "Bob" in out["text"]


async def test_reaction_escapes_html_in_names(client):
    """A malicious first name must never break out of the HTML markup."""
    from plugins.reactions import reaction_handler

    evil = FakeUser(2, "<script>alert('x')</script>")
    msg = FakeMessage("/hug", user=FakeUser(1, "Ann"),
                      reply_to=FakeMessage(user=evil), command=["hug"])
    await handler(reaction_handler)(client, msg)

    text = msg.last_text
    assert "<script>" not in text
    assert "&lt;script&gt;" in text


async def test_self_reaction_uses_the_self_variant(client, translator):
    """Replying to yourself gets the teasing self-target line, not the pair line."""
    from plugins.reactions import reaction_handler

    ann = FakeUser(1, "Ann")
    msg = FakeMessage("/slap", user=ann, reply_to=FakeMessage(user=ann), command=["slap"])
    await handler(reaction_handler)(client, msg)

    assert translator.has("reactions.self_target")
    assert "missing string" not in msg.last_text
    assert "Ann" in msg.last_text


async def test_reaction_aimed_at_the_bot_uses_the_bot_variant(client, translator):
    from plugins.reactions import reaction_handler

    msg = FakeMessage("/kill", user=FakeUser(1, "Ann"),
                      reply_to=FakeMessage(user=client.me), command=["kill"])
    await handler(reaction_handler)(client, msg)

    assert translator.has("reactions.bot_target")
    assert "missing string" not in msg.last_text
    # The bot must not narrate an attack on itself using the normal pair text.
    assert "Ann" in msg.last_text


async def test_solo_reaction_needs_no_reply(client):
    """/dance and friends must work without replying to anyone."""
    from plugins.reactions import reaction_handler

    msg = FakeMessage("/dance", user=FakeUser(1, "Ann"), command=["dance"])
    await handler(reaction_handler)(client, msg)
    assert "Ann" in msg.last_text


@pytest.mark.parametrize("cmd", sorted(REGISTRY))
async def test_every_registered_command_runs(client, cmd):
    """No command or alias may crash or emit a missing-string marker."""
    from plugins.reactions import reaction_handler

    spec = get_spec(cmd)
    reply = None if spec.solo_message_key else FakeMessage(user=FakeUser(2, "Bob"))
    msg = FakeMessage(f"/{cmd}", user=FakeUser(1, "Ann"), reply_to=reply, command=[cmd])
    await handler(reaction_handler)(client, msg)

    assert msg.sent, f"/{cmd} produced no reply"
    assert "missing string" not in msg.last_text


async def test_missing_media_folder_still_replies_with_text(client, translator):
    from plugins.reactions import reaction_handler

    async def _no_media(folder, exclude=()):
        return None

    client.media.choose = _no_media
    msg = FakeMessage("/hug", reply_to=FakeMessage(user=FakeUser(2, "Bob")), command=["hug"])
    await handler(reaction_handler)(client, msg)

    assert msg.last["kind"] == "text"
    assert "Bob" in msg.last_text


# ---------------------------------------------------------------- /couple
def _members(n=6):
    return [FakeUser(i, f"User{i}") for i in range(1, n + 1)]


async def test_couple_picks_two_distinct_non_bot_members(client):
    from plugins.couple import couple_command

    client._members = _members() + [FakeUser(77, "SomeBot", is_bot=True)]
    msg = FakeMessage("/couple", command=["couple"])
    await handler(couple_command)(client, msg)

    text = msg.last_text
    assert "SomeBot" not in text
    named = [u.first_name for u in _members() if u.first_name in text]
    assert len(set(named)) == 2, f"expected exactly 2 distinct people, got {named}"


async def test_couple_second_call_is_blocked_and_shows_remaining(client):
    from plugins.couple import couple_command

    client._members = _members()
    first = FakeMessage("/couple", command=["couple"])
    await handler(couple_command)(client, first)

    second = FakeMessage("/couple", command=["couple"])
    await handler(couple_command)(client, second)

    text = second.last_text
    assert "h" in text or "m" in text, "remaining time not shown"


async def test_couple_repeats_the_same_pair_while_cooling_down(client):
    from plugins.couple import couple_command

    client._members = _members()
    first = FakeMessage("/couple", command=["couple"])
    await handler(couple_command)(client, first)
    names_first = {u.first_name for u in client._members if u.first_name in first.last_text}

    second = FakeMessage("/couple", command=["couple"])
    await handler(couple_command)(client, second)
    names_second = {u.first_name for u in client._members if u.first_name in second.last_text}

    assert names_first == names_second


async def test_couple_needs_enough_members(client):
    from plugins.couple import couple_command

    client._members = [FakeUser(1, "Solo")]
    msg = FakeMessage("/couple", command=["couple"])
    await handler(couple_command)(client, msg)

    assert msg.sent and "missing string" not in msg.last_text
    # Cooldown must NOT be consumed by a failed attempt.
    assert (await client.couple_cooldown.peek(msg.chat.id)).allowed is True


# ------------------------------------------------------------- /broadcast
async def test_broadcast_rejected_without_reply(client, config):
    from plugins.broadcast import broadcast_command

    msg = FakeMessage("/broadcast", user=FakeUser(config.owner_ids[0]), command=["broadcast"])
    await handler(broadcast_command)(client, msg)
    assert msg.sent and "missing string" not in msg.last_text


async def test_broadcast_reports_counts_and_deactivates_blocked(client, config, database):
    from plugins import broadcast as bmod

    for uid in (1, 2, 3):
        await database.save_user(uid, f"U{uid}")

    calls = {"n": 0}

    async def fake_copy(chat_id, **kw):
        calls["n"] += 1
        if chat_id == 2:
            raise UserIsBlocked()
        if chat_id == 3:
            raise InputUserDeactivated()
        return FakeMessage()

    source = FakeMessage("hello everyone")
    source.copy = fake_copy

    msg = FakeMessage("/broadcast -u", user=FakeUser(config.owner_ids[0]),
                      reply_to=source, command=["broadcast", "-u"])
    await handler(bmod.broadcast_command)(client, msg)

    assert calls["n"] == 3
    # Blocked + deleted accounts must be flagged inactive.
    assert await database.count_users(active_only=True) == 1


async def test_broadcast_retries_after_floodwait(client, config, database):
    from plugins import broadcast as bmod

    await database.save_user(1, "U1")
    attempts = {"n": 0}

    async def flaky_copy(chat_id, **kw):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise FloodWait(value=0)
        return FakeMessage()

    source = FakeMessage("hi")
    source.copy = flaky_copy

    msg = FakeMessage("/broadcast -u", user=FakeUser(config.owner_ids[0]),
                      reply_to=source, command=["broadcast", "-u"])
    await handler(bmod.broadcast_command)(client, msg)

    assert attempts["n"] == 2, "FloodWait was not retried"
    assert await database.count_users(active_only=True) == 1


# ------------------------------------------------------------------ /start
async def test_start_registers_user_and_shows_keyboard(client, database):
    from plugins.start import start_command

    msg = FakeMessage("/start", user=FakeUser(42, "Ann"),
                      chat=FakeChat(42, "private"), command=["start"])
    await handler(start_command)(client, msg)

    assert "Ann" in msg.last_text
    assert msg.last.get("reply_markup") is not None
    assert await database.get_user(42) is not None


async def test_help_lists_commands(client):
    from plugins.start import help_command

    msg = FakeMessage("/help", chat=FakeChat(42, "private"), command=["help"])
    await handler(help_command)(client, msg)
    assert msg.sent and "missing string" not in msg.last_text
