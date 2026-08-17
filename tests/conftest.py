"""Shared test fixtures.

The suite exercises the real code paths: the real i18n loader, the real media
pool against a temporary asset tree, and the real database layer running on an
in-memory Mongo double (``mongomock_motor``) that speaks the same async API.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import pytest_asyncio

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.config import Config, set_config  # noqa: E402
from core.database import Database, set_database  # noqa: E402
from core.i18n import Translator, set_translator  # noqa: E402
from core.reactions import all_media_folders  # noqa: E402
from utils.random_media import MediaPool, set_media_pool  # noqa: E402


@pytest.fixture(scope="session")
def project_root() -> Path:
    return ROOT


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    cfg = Config(
        api_id=12345,
        api_hash="0123456789abcdef0123456789abcdef",
        bot_token="123456789:TEST-TOKEN-VALUE-FOR-UNIT-TESTS-ONLY",
        mongo_uri="mongodb://127.0.0.1:27017",
        mongo_db_name="opusfun_test",
        owner_ids=(111, 222),
        bot_name="OpusFun",
        start_img_url="",
        support_url="https://t.me/support",
        update_url="https://t.me/updates",
        owner_url="https://t.me/owner",
        developer_url="https://t.me/dev",
        couple_cooldown_hours=6,
        broadcast_concurrency=4,
        broadcast_sleep=0.0,
        media_cache_ttl=300,
        log_level="INFO",
        log_file="",
        workers=4,
        default_language="en",
        base_dir=ROOT,
    )
    # Handlers and decorators read the module-level singleton, so install it.
    set_config(cfg)
    return cfg


@pytest.fixture()
def translator() -> Translator:
    t = Translator(ROOT / "locales", "en")
    set_translator(t)
    return t


@pytest.fixture()
def media_dir(tmp_path: Path) -> Path:
    """A temporary assist/ tree with valid, invalid and unsupported files."""
    root = tmp_path / "assist"

    slap = root / "slap"
    slap.mkdir(parents=True)
    for i in range(4):
        (slap / f"slap{i}.gif").write_bytes(b"GIF89a" + b"\x00" * 200)
    (slap / "notes.txt").write_text("not media")          # unsupported
    (slap / "broken.gif").write_bytes(b"x")               # too small -> skipped
    (slap / "photo.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 200)

    (root / "empty").mkdir()                              # empty folder

    linked = root / "linked"
    linked.mkdir()
    (linked / "links.txt").write_text(
        "# comment\n\nhttps://example.com/a.gif\nhttps://example.com/b.mp4\n"
        "https://example.com/c.jpg\n"
    )

    # Every folder the bot actually ships with, so handler tests find media.
    for folder in all_media_folders():
        target = root / folder
        target.mkdir(parents=True, exist_ok=True)
        for i in range(3):
            (target / f"{folder}{i}.gif").write_bytes(b"GIF89a" + b"\x00" * 200)

    return root


@pytest.fixture()
def media_pool(media_dir: Path) -> MediaPool:
    pool = MediaPool(media_dir, cache_ttl=300)
    set_media_pool(pool)
    return pool


@pytest_asyncio.fixture()
async def database() -> Database:
    """Real Database class, in-memory Mongo backend."""
    from mongomock_motor import AsyncMongoMockClient

    client = AsyncMongoMockClient()
    db = Database("mongodb://mock", "opusfun_test", client=client)
    await db.ensure_indexes()
    set_database(db)
    return db
