"""Tests for helpers, i18n, media selection, keyboards and the registry."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from core.helpers import (
    escape_html,
    format_duration,
    format_number,
    is_valid_http_url,
    mention_id,
    mention_user,
    truncate,
    user_display_name,
)
from core.i18n import Translator
from core.keyboards import ButtonStyle, build_markup, start_keyboard, styled_button
from core.reactions import (
    REACTIONS,
    REGISTRY,
    ReactionSpec,
    all_media_folders,
    build_registry,
    get_spec,
)
from utils.cooldown import MemoryCooldown
from utils.random_media import MediaKind, MediaPool


class FakeUser:
    def __init__(self, uid=1, first="Ann", last=None, username=None, is_bot=False):
        self.id = uid
        self.first_name = first
        self.last_name = last
        self.username = username
        self.is_bot = is_bot


# --------------------------------------------------------------------- HTML
def test_escape_html_neutralises_injection():
    assert escape_html("<b>bold</b>") == "&lt;b&gt;bold&lt;/b&gt;"
    assert escape_html('a"b') == "a&quot;b"
    assert escape_html("a&b") == "a&amp;b"
    assert escape_html(None) == ""


def test_mention_escapes_malicious_display_name():
    user = FakeUser(uid=42, first='<a href="x">hax</a>')
    mention = mention_user(user)
    assert mention.startswith('<a href="tg://user?id=42">')
    # The dangerous markup must be escaped, leaving exactly one real anchor.
    assert mention.count("<a href") == 1
    assert "&lt;a href" in mention


def test_mention_falls_back_without_name():
    assert "Someone" in mention_user(FakeUser(uid=7, first=""), fallback="Someone")
    assert mention_user(None) == "Someone"


def test_mention_id_escapes_name():
    assert mention_id(5, "<script>") == '<a href="tg://user?id=5">&lt;script&gt;</a>'


def test_user_display_name_combines_and_truncates():
    assert user_display_name(FakeUser(first="Ann", last="Lee")) == "Ann Lee"
    long_name = user_display_name(FakeUser(first="x" * 200))
    assert len(long_name) <= 64


# ----------------------------------------------------------------- format
@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "0s"),
        (45, "45s"),
        (90, "1m 30s"),
        (750, "12m 30s"),
        (16020, "4h 27m"),   # the "/couple cooldown" example from the spec
        (90000, "1d 1h"),
    ],
)
def test_format_duration(seconds, expected):
    assert format_duration(seconds) == expected


def test_format_number_and_truncate():
    assert format_number(1245) == "1,245"
    assert len(truncate("a" * 5000)) == 4096


def test_url_validation():
    assert is_valid_http_url("https://example.com/a.gif")
    assert not is_valid_http_url("javascript:alert(1)")
    assert not is_valid_http_url("")
    assert not is_valid_http_url(None)


# ------------------------------------------------------------------- i18n
def test_locale_file_loads_and_has_every_required_key(translator: Translator):
    required = [
        "start.text",
        "help.text",
        "couple.announcement",
        "couple.cooldown",
        "broadcast.completed",
        "broadcast.no_reply",
        "errors.no_reply",
        "errors.owner_only",
        "errors.group_only",
        "errors.generic",
        "buttons.support",
        "buttons.help",
    ]
    for key in required:
        assert translator.has(key), f"missing locale key: {key}"


def test_every_reaction_has_locale_strings(translator: Translator):
    for spec in REACTIONS:
        assert translator.has(spec.message_key), f"missing {spec.message_key}"
        assert translator.has(f"help.desc.{spec.command}"), f"missing help.desc.{spec.command}"
        if spec.solo_message_key:
            assert translator.has(spec.solo_message_key)


def test_placeholders_render(translator: Translator):
    text = translator.get("couple.cooldown", remaining="4h 27m")
    assert "4h 27m" in text and "{" not in text


def test_random_variant_selection(translator: Translator):
    seen = {translator.get("reactions.slap", user1="A", user2="B") for _ in range(60)}
    assert len(seen) > 1, "list-valued strings should vary"


def test_missing_key_does_not_raise(translator: Translator):
    assert "missing string" in translator.get("nope.not.here")


def test_bad_placeholder_does_not_raise(translator: Translator):
    # Omitting a placeholder returns the raw template rather than crashing.
    assert translator.get("couple.cooldown") is not None


# ------------------------------------------------------------------ media
async def test_media_scan_filters_invalid_files(media_pool: MediaPool):
    items = await media_pool.get_items("slap")
    names = {Path(i.value).name for i in items}
    assert "notes.txt" not in names        # unsupported extension
    assert "broken.gif" not in names       # too small / corrupt
    assert "slap0.gif" in names
    assert "photo.jpg" in names


async def test_media_kind_classification(media_pool: MediaPool):
    items = await media_pool.get_items("slap")
    kinds = {Path(i.value).suffix: i.kind for i in items}
    assert kinds[".gif"] == MediaKind.ANIMATION
    assert kinds[".jpg"] == MediaKind.PHOTO


async def test_empty_folder_returns_none_not_crash(media_pool: MediaPool):
    assert await media_pool.get_items("empty") == []
    assert await media_pool.choose("empty") is None


async def test_missing_folder_returns_none_not_crash(media_pool: MediaPool):
    assert await media_pool.choose("does_not_exist") is None


async def test_links_file_parsed_as_remote(media_pool: MediaPool):
    items = await media_pool.get_items("linked")
    assert len(items) == 3
    assert all(i.is_remote for i in items)
    assert {i.kind for i in items} == {MediaKind.ANIMATION, MediaKind.PHOTO}


async def test_selection_is_random_and_avoids_repeats(media_pool: MediaPool):
    picks = [(await media_pool.choose("slap")).value for _ in range(12)]
    assert len(set(picks)) > 1, "selection must vary"
    # With 5 usable files and an 8-slot history, no immediate repeats.
    assert all(a != b for a, b in zip(picks, picks[1:]))


async def test_path_traversal_is_rejected(media_pool: MediaPool):
    assert media_pool.resolve_folder("../../etc") is None
    assert await media_pool.choose("../../etc") is None


async def test_cache_avoids_repeated_filesystem_scans(media_pool: MediaPool, monkeypatch):
    await media_pool.get_items("slap")
    calls = {"n": 0}
    original = media_pool._scan

    def counting_scan(folder):
        calls["n"] += 1
        return original(folder)

    monkeypatch.setattr(media_pool, "_scan", counting_scan)
    for _ in range(25):
        await media_pool.get_items("slap")
    assert calls["n"] == 0, "cached folder must not be rescanned within its TTL"


async def test_cache_invalidation_picks_up_new_files(media_pool: MediaPool, media_dir: Path):
    before = len(await media_pool.get_items("slap"))
    (media_dir / "slap" / "extra.gif").write_bytes(b"GIF89a" + b"\x00" * 200)
    await media_pool.invalidate("slap")
    assert len(await media_pool.get_items("slap")) == before + 1


async def test_concurrent_access_is_safe(media_pool: MediaPool):
    results = await asyncio.gather(*(media_pool.choose("slap") for _ in range(40)))
    assert all(r is not None for r in results)


async def test_warmup_and_ensure_folders(tmp_path: Path):
    pool = MediaPool(tmp_path / "assist", cache_ttl=0)
    pool.ensure_folders(["hug", "slap"])
    assert (tmp_path / "assist" / "hug").is_dir()
    report = await pool.warmup(["hug", "slap"])
    assert report == {"hug": 0, "slap": 0}


# --------------------------------------------------------------- registry
def test_registry_covers_all_required_commands():
    required = {
        "slap", "hug", "dance", "marriage", "kill", "beep", "laughing",
        "perpose", "sleeping", "goodnight", "goodmorning", "welcome",
        "prank", "fight",
    }
    assert required.issubset(set(REGISTRY))


def test_registry_rejects_duplicate_commands():
    with pytest.raises(ValueError):
        build_registry(
            [
                ReactionSpec(command="x", folder="x", message_key="a"),
                ReactionSpec(command="y", folder="y", message_key="b", aliases=("x",)),
            ]
        )


def test_spec_lookup_normalises_input():
    assert get_spec("/SLAP") is get_spec("slap")
    assert get_spec("marry") is get_spec("marriage")
    assert get_spec("unknown") is None


def test_media_folders_exist_in_repo(project_root: Path):
    for folder in all_media_folders():
        assert (project_root / "assist" / folder).is_dir(), f"assist/{folder} missing"


# -------------------------------------------------------------- keyboards
def test_start_keyboard_layout(config, translator):
    markup = start_keyboard(config, translator.get)
    rows = markup.inline_keyboard
    assert [len(r) for r in rows] == [2, 1, 2]
    assert rows[0][0].url == config.support_url
    assert rows[0][1].url == config.update_url
    assert rows[1][0].callback_data == "help:open"
    assert rows[2][0].url == config.owner_url
    assert rows[2][1].url == config.developer_url


def test_buttons_use_only_supported_parameters():
    """Guards against passing a parameter the installed Pyrofork rejects."""
    for style in ButtonStyle:
        button = styled_button("Test", style=style, url="https://t.me/x")
        assert button.text
        assert button.url == "https://t.me/x"


def test_build_markup_drops_empty_rows():
    button = styled_button("A", callback_data="a")
    markup = build_markup([[button], [], [button]])
    assert len(markup.inline_keyboard) == 2


@pytest.mark.parametrize("bad_url", ["", "   ", "not-a-url", "javascript:alert(1)", "t.me/x"])
def test_styled_button_drops_invalid_urls(bad_url):
    """Telegram rejects the whole markup on a blank/invalid URL button."""
    assert styled_button("Support", url=bad_url) is None


def test_build_markup_filters_dropped_buttons():
    """A row whose buttons were all dropped must disappear entirely."""
    markup = build_markup(
        [
            [styled_button("Support", url=""), styled_button("Update", url="")],
            [styled_button("Help", callback_data="help:open")],
        ]
    )
    assert len(markup.inline_keyboard) == 1
    assert markup.inline_keyboard[0][0].callback_data == "help:open"


def test_start_keyboard_survives_unconfigured_urls(config, translator):
    """cp .env.example .env with blank URLs must still yield a valid keyboard."""
    blank = replace(config, support_url="", update_url="", owner_url="", developer_url="")
    markup = start_keyboard(blank, translator)

    # Only the callback-driven Help button remains, and it is still usable.
    assert len(markup.inline_keyboard) == 1
    assert markup.inline_keyboard[0][0].callback_data == "help:open"

    # Nothing that would trigger BUTTON_URL_INVALID survived.
    for row in markup.inline_keyboard:
        assert row, "empty rows are rejected by Telegram"
        for button in row:
            assert button.url is None or button.url.startswith("http")


def test_start_keyboard_keeps_only_configured_links(config, translator):
    """A partially configured deployment keeps the links it does have."""
    partial = replace(config, update_url="", owner_url="", developer_url="")
    rows = start_keyboard(partial, translator).inline_keyboard

    urls = [b.url for row in rows for b in row if b.url]
    assert urls == [config.support_url]


# --------------------------------------------------------------- cooldown
def test_memory_cooldown_blocks_then_expires():
    cd = MemoryCooldown(period=0.05)
    allowed, _ = cd.check("k")
    assert allowed
    blocked, remaining = cd.check("k")
    assert not blocked and remaining > 0
    import time as _t
    _t.sleep(0.06)
    assert cd.check("k")[0]


def test_memory_cooldown_is_per_key():
    cd = MemoryCooldown(period=10)
    assert cd.check("a")[0]
    assert cd.check("b")[0]
