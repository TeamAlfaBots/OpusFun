"""Random media selection with caching and graceful degradation.

Each reaction owns a directory under ``assist/``.  A directory may contain:

* real media files (``.gif``, ``.mp4``, ``.webm``, ``.jpg``, ``.png`` …), and/or
* a ``links.txt`` file listing one remote media URL per line (``#`` = comment).

Both sources are merged into one pool, so a deployment can ship local GIFs,
Telegram ``file_id``s / URLs, or a mix.  Directory listings are cached with a
TTL to avoid hitting the filesystem on every single command, and a per-folder
recent-history window prevents the same GIF being sent twice in a row.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, Dict, Iterable, List, Optional, Sequence

log = logging.getLogger(__name__)

#: Extensions Telegram can ingest as animation/photo/video.
ANIMATION_EXTENSIONS = frozenset({".gif", ".mp4", ".webm", ".webp"})
PHOTO_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png"})
VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".mkv", ".webm"})
SUPPORTED_EXTENSIONS = ANIMATION_EXTENSIONS | PHOTO_EXTENSIONS | VIDEO_EXTENSIONS

#: Filename holding remote URLs / file_ids for a folder.
LINKS_FILENAME = "links.txt"

#: Files smaller than this are treated as corrupt placeholders and skipped.
MIN_FILE_SIZE_BYTES = 64


class MediaKind:
    ANIMATION = "animation"
    PHOTO = "photo"
    VIDEO = "video"


@dataclass(frozen=True, slots=True)
class MediaItem:
    """A single sendable media reference."""

    value: str
    kind: str
    is_remote: bool

    @property
    def path(self) -> Optional[Path]:
        return None if self.is_remote else Path(self.value)


#: How many recent picks are remembered per folder to avoid repeats.
RECENT_HISTORY = 8


@dataclass
class _CacheEntry:
    items: List[MediaItem]
    loaded_at: float
    recent: Deque[str] = field(default_factory=lambda: deque(maxlen=RECENT_HISTORY))


def classify_extension(suffix: str) -> Optional[str]:
    """Map a file suffix to the Telegram send method it belongs to."""
    suffix = suffix.lower()
    if suffix in ANIMATION_EXTENSIONS:
        # .mp4/.webm are sent as animations so they loop silently like GIFs.
        return MediaKind.ANIMATION
    if suffix in PHOTO_EXTENSIONS:
        return MediaKind.PHOTO
    if suffix in VIDEO_EXTENSIONS:
        return MediaKind.VIDEO
    return None


def _classify_remote(value: str) -> str:
    lowered = value.lower().split("?", 1)[0]
    for ext in PHOTO_EXTENSIONS:
        if lowered.endswith(ext):
            return MediaKind.PHOTO
    for ext in ANIMATION_EXTENSIONS:
        if lowered.endswith(ext):
            return MediaKind.ANIMATION
    # Unknown remote targets (incl. Telegram file_ids) default to animation,
    # which is the dominant media type for reactions.
    return MediaKind.ANIMATION


class MediaPool:
    """Caching random-media provider shared by every reaction command."""

    def __init__(self, base_dir: Path, cache_ttl: int = 300) -> None:
        self.base_dir = Path(base_dir).resolve()
        self.cache_ttl = max(0, int(cache_ttl))
        self._cache: Dict[str, _CacheEntry] = {}
        self._lock = asyncio.Lock()

    # -------------------------------------------------------------- security
    def resolve_folder(self, folder: str) -> Optional[Path]:
        """Resolve a folder name to a path strictly inside ``base_dir``.

        Folder names come from the bot's own reaction registry, never from user
        input, but the containment check is kept as defence in depth against
        path traversal should that ever change.
        """
        candidate = (self.base_dir / str(folder).strip()).resolve()
        try:
            candidate.relative_to(self.base_dir)
        except ValueError:
            log.error("Rejected media folder outside of assets dir: %s", folder)
            return None
        return candidate

    # ------------------------------------------------------------- scanning
    def _scan(self, folder_key: str) -> List[MediaItem]:
        """Blocking directory scan (run in a worker thread)."""
        directory = self.resolve_folder(folder_key)
        if directory is None:
            return []

        if not directory.is_dir():
            log.warning("Media folder is missing: %s", directory)
            return []

        items: List[MediaItem] = []
        skipped = 0

        try:
            entries = sorted(directory.iterdir())
        except OSError as exc:
            log.error("Cannot read media folder %s: %s", directory, exc)
            return []

        for entry in entries:
            try:
                if entry.name == LINKS_FILENAME and entry.is_file():
                    items.extend(self._read_links(entry))
                    continue
                if not entry.is_file():
                    continue
                kind = classify_extension(entry.suffix)
                if kind is None:
                    skipped += 1
                    continue
                if entry.stat().st_size < MIN_FILE_SIZE_BYTES:
                    log.warning("Skipping suspiciously small media file: %s", entry.name)
                    skipped += 1
                    continue
                items.append(MediaItem(value=str(entry), kind=kind, is_remote=False))
            except OSError as exc:
                # A single unreadable file must not break the whole folder.
                log.warning("Skipping unreadable media file %s: %s", entry, exc)
                skipped += 1

        if skipped:
            log.debug("Folder %s: skipped %d unsupported/invalid file(s)", folder_key, skipped)
        if not items:
            log.warning("Media folder '%s' contains no usable media", folder_key)
        else:
            log.debug("Folder %s: cached %d media item(s)", folder_key, len(items))
        return items

    @staticmethod
    def _read_links(path: Path) -> List[MediaItem]:
        items: List[MediaItem] = []
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for raw_line in fh:
                    line = raw_line.strip()
                    if not line or line.startswith("#"):
                        continue
                    items.append(
                        MediaItem(value=line, kind=_classify_remote(line), is_remote=True)
                    )
        except OSError as exc:
            log.warning("Could not read %s: %s", path, exc)
        return items

    # ---------------------------------------------------------------- access
    async def get_items(self, folder: str, *, force_refresh: bool = False) -> List[MediaItem]:
        """Return the cached media list for a folder, refreshing when stale."""
        key = str(folder).strip().strip("/")
        now = time.monotonic()

        entry = self._cache.get(key)
        if (
            not force_refresh
            and entry is not None
            and (self.cache_ttl == 0 or now - entry.loaded_at < self.cache_ttl)
        ):
            return entry.items

        async with self._lock:
            # Re-check: another coroutine may have refreshed while we waited.
            entry = self._cache.get(key)
            if (
                not force_refresh
                and entry is not None
                and (self.cache_ttl == 0 or time.monotonic() - entry.loaded_at < self.cache_ttl)
            ):
                return entry.items

            items = await asyncio.to_thread(self._scan, key)
            recent = entry.recent if entry is not None else deque(maxlen=RECENT_HISTORY)
            self._cache[key] = _CacheEntry(items=items, loaded_at=time.monotonic(), recent=recent)
            return items

    async def choose(self, folder: str, *, exclude: Sequence[str] = ()) -> Optional[MediaItem]:
        """Pick a random item, avoiding recently used ones where possible."""
        items = await self.get_items(folder)
        if not items:
            return None

        key = str(folder).strip().strip("/")
        entry = self._cache.get(key)
        recent = entry.recent if entry is not None else deque(maxlen=RECENT_HISTORY)
        excluded = set(exclude)

        # Never remember more than the folder can offer, otherwise the history
        # blocks every candidate and the anti-repeat guarantee is lost.
        last = recent[-1] if recent else None
        usable_history = max(0, min(len(recent), len(items) - 1))
        blocked = set(list(recent)[len(recent) - usable_history :]) | excluded

        pool = [item for item in items if item.value not in blocked]
        if not pool:
            # Small folder: forget the history but still avoid an instant repeat.
            recent.clear()
            pool = [
                item
                for item in items
                if item.value not in excluded and item.value != last
            ]
            pool = pool or [item for item in items if item.value not in excluded] or items

        chosen = random.choice(pool)
        recent.append(chosen.value)
        return chosen

    async def invalidate(self, folder: Optional[str] = None) -> None:
        async with self._lock:
            if folder is None:
                self._cache.clear()
            else:
                self._cache.pop(str(folder).strip().strip("/"), None)

    async def warmup(self, folders: Iterable[str]) -> Dict[str, int]:
        """Pre-scan folders at startup and report how many items each holds."""
        report: Dict[str, int] = {}
        for folder in folders:
            items = await self.get_items(folder, force_refresh=True)
            report[folder] = len(items)
        return report

    def ensure_folders(self, folders: Iterable[str]) -> None:
        """Create any missing media folders so a fresh clone runs immediately."""
        for folder in folders:
            directory = self.resolve_folder(folder)
            if directory is None:
                continue
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                log.warning("Could not create media folder %s: %s", directory, exc)


_pool: Optional[MediaPool] = None


def get_media_pool() -> MediaPool:
    global _pool
    if _pool is None:
        from config import get_config

        config = get_config()
        _pool = MediaPool(config.assets_dir, config.media_cache_ttl)
    return _pool


def set_media_pool(pool: MediaPool) -> None:
    global _pool
    _pool = pool
