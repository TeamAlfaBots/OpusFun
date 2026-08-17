"""Asynchronous MongoDB layer for OpusFun.

A single :class:`Database` instance owns one Motor client for the whole
process; connections are pooled by Motor itself, so handlers never create
their own.  All timestamps are stored as timezone-aware UTC datetimes so the
6-hour couple cooldown behaves identically regardless of server timezone.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from motor.motor_asyncio import (
    AsyncIOMotorClient,
    AsyncIOMotorCollection,
    AsyncIOMotorDatabase,
)
from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import PyMongoError

log = logging.getLogger(__name__)

# Deactivation updates are chunked to keep each query small.
_BULK_CHUNK = 500


def utcnow() -> datetime:
    """Timezone-aware current UTC time (never naive)."""
    return datetime.now(timezone.utc)


def _as_utc(value: Any) -> Optional[datetime]:
    """Normalise a value read back from Mongo into an aware UTC datetime.

    PyMongo returns naive datetimes by default; treating them as UTC keeps the
    cooldown maths correct across restarts and hosts.
    """
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class DatabaseError(RuntimeError):
    """Raised when the database is unusable (connection/auth failures)."""


class Database:
    """Thin, typed async wrapper around the collections OpusFun needs."""

    def __init__(
        self,
        uri: str,
        db_name: str,
        *,
        client: Optional[AsyncIOMotorClient] = None,
        server_selection_timeout_ms: int = 8000,
    ) -> None:
        self._uri = uri
        self._db_name = db_name
        self._server_selection_timeout_ms = server_selection_timeout_ms
        self._client: Optional[AsyncIOMotorClient] = client
        self._db: Optional[AsyncIOMotorDatabase] = None
        if client is not None:
            self._db = client[db_name]

    # ------------------------------------------------------------- lifecycle
    @property
    def db(self) -> AsyncIOMotorDatabase:
        if self._db is None:
            raise DatabaseError("Database is not connected. Call connect() first.")
        return self._db

    @property
    def users(self) -> AsyncIOMotorCollection:
        return self.db["users"]

    @property
    def groups(self) -> AsyncIOMotorCollection:
        return self.db["groups"]

    @property
    def couple_cooldowns(self) -> AsyncIOMotorCollection:
        return self.db["couple_cooldowns"]

    @property
    def couples(self) -> AsyncIOMotorCollection:
        return self.db["couples"]

    async def connect(self) -> None:
        """Open the connection, verify it with a ping and ensure indexes."""
        if self._client is None:
            self._client = AsyncIOMotorClient(
                self._uri,
                serverSelectionTimeoutMS=self._server_selection_timeout_ms,
                tz_aware=True,
                appname="OpusFun",
                retryWrites=True,
            )
            self._db = self._client[self._db_name]

        try:
            await self._client.admin.command("ping")
        except PyMongoError as exc:
            # Never echo the URI: it usually embeds the password.
            raise DatabaseError(
                f"Could not connect to MongoDB ({type(exc).__name__}). "
                "Check MONGO_URI, network access and IP allow-list."
            ) from exc

        log.info("Connected to MongoDB database '%s'", self._db_name)
        await self.ensure_indexes()

    async def close(self) -> None:
        if self._client is not None:
            self._client.close()
            log.info("MongoDB connection closed")

    async def ensure_indexes(self) -> None:
        """Create the indexes the query patterns rely on (idempotent)."""
        try:
            await self.users.create_index([("user_id", ASCENDING)], unique=True, name="user_id_uq")
            await self.users.create_index([("last_seen", DESCENDING)], name="user_last_seen")
            await self.users.create_index([("active", ASCENDING)], name="user_active")

            await self.groups.create_index([("chat_id", ASCENDING)], unique=True, name="chat_id_uq")
            await self.groups.create_index([("last_seen", DESCENDING)], name="group_last_seen")
            await self.groups.create_index([("active", ASCENDING)], name="group_active")

            await self.couple_cooldowns.create_index(
                [("chat_id", ASCENDING)], unique=True, name="cooldown_chat_uq"
            )
            await self.couples.create_index(
                [("chat_id", ASCENDING), ("created_at", DESCENDING)], name="couple_history"
            )
            log.info("MongoDB indexes verified")
        except PyMongoError as exc:
            # Index creation can legitimately fail on restricted shared clusters;
            # that must not stop the bot from serving.
            log.warning("Could not create one or more indexes: %s", exc)

    async def health_check(self) -> bool:
        try:
            await self.db.command("ping")
            return True
        except PyMongoError as exc:
            log.error("MongoDB health check failed: %s", exc)
            return False

    # ----------------------------------------------------------------- users
    async def save_user(
        self,
        user_id: int,
        first_name: str | None = None,
        username: str | None = None,
    ) -> bool:
        """Upsert a user. Returns ``True`` when the user was newly created."""
        now = utcnow()
        try:
            result = await self.users.find_one_and_update(
                {"user_id": int(user_id)},
                {
                    "$set": {
                        "first_name": first_name,
                        "username": username,
                        "last_seen": now,
                        "active": True,
                    },
                    "$setOnInsert": {"user_id": int(user_id), "joined_at": now},
                },
                upsert=True,
                return_document=ReturnDocument.BEFORE,
            )
            return result is None
        except PyMongoError as exc:
            log.error("save_user(%s) failed: %s", user_id, exc)
            return False

    async def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        try:
            return await self.users.find_one({"user_id": int(user_id)})
        except PyMongoError as exc:
            log.error("get_user(%s) failed: %s", user_id, exc)
            return None

    async def mark_user_inactive(self, user_id: int, reason: str = "unknown") -> None:
        """Flag a user we can no longer message (blocked/deleted account)."""
        try:
            await self.users.update_one(
                {"user_id": int(user_id)},
                {"$set": {"active": False, "inactive_reason": reason, "inactive_at": utcnow()}},
            )
        except PyMongoError as exc:
            log.error("mark_user_inactive(%s) failed: %s", user_id, exc)

    async def count_users(self, active_only: bool = True) -> int:
        query: Dict[str, Any] = {"active": {"$ne": False}} if active_only else {}
        try:
            return await self.users.count_documents(query)
        except PyMongoError as exc:
            log.error("count_users failed: %s", exc)
            return 0

    async def iter_user_ids(self, active_only: bool = True) -> AsyncIterator[int]:
        query: Dict[str, Any] = {"active": {"$ne": False}} if active_only else {}
        cursor = self.users.find(query, {"user_id": 1, "_id": 0})
        async for doc in cursor:
            user_id = doc.get("user_id")
            if isinstance(user_id, int):
                yield user_id

    # ---------------------------------------------------------------- groups
    async def save_group(
        self,
        chat_id: int,
        title: str | None = None,
        username: str | None = None,
    ) -> bool:
        """Upsert a group. Returns ``True`` when newly registered."""
        now = utcnow()
        try:
            result = await self.groups.find_one_and_update(
                {"chat_id": int(chat_id)},
                {
                    "$set": {
                        "title": title,
                        "username": username,
                        "last_seen": now,
                        "active": True,
                    },
                    "$setOnInsert": {"chat_id": int(chat_id), "registered_at": now},
                },
                upsert=True,
                return_document=ReturnDocument.BEFORE,
            )
            return result is None
        except PyMongoError as exc:
            log.error("save_group(%s) failed: %s", chat_id, exc)
            return False

    async def mark_group_inactive(self, chat_id: int, reason: str = "unknown") -> None:
        try:
            await self.groups.update_one(
                {"chat_id": int(chat_id)},
                {"$set": {"active": False, "inactive_reason": reason, "inactive_at": utcnow()}},
            )
        except PyMongoError as exc:
            log.error("mark_group_inactive(%s) failed: %s", chat_id, exc)

    async def count_groups(self, active_only: bool = True) -> int:
        query: Dict[str, Any] = {"active": {"$ne": False}} if active_only else {}
        try:
            return await self.groups.count_documents(query)
        except PyMongoError as exc:
            log.error("count_groups failed: %s", exc)
            return 0

    async def iter_group_ids(self, active_only: bool = True) -> AsyncIterator[int]:
        query: Dict[str, Any] = {"active": {"$ne": False}} if active_only else {}
        cursor = self.groups.find(query, {"chat_id": 1, "_id": 0})
        async for doc in cursor:
            chat_id = doc.get("chat_id")
            if isinstance(chat_id, int):
                yield chat_id

    # ------------------------------------------------------- couple cooldown
    async def get_couple_cooldown(self, chat_id: int) -> Optional[datetime]:
        """Return the aware UTC timestamp of the last successful /couple."""
        try:
            doc = await self.couple_cooldowns.find_one({"chat_id": int(chat_id)})
        except PyMongoError as exc:
            log.error("get_couple_cooldown(%s) failed: %s", chat_id, exc)
            return None
        if not doc:
            return None
        return _as_utc(doc.get("last_used"))

    async def couple_cooldown_remaining(self, chat_id: int, cooldown_seconds: int) -> float:
        """Seconds left before /couple may be used again (0 when ready)."""
        last_used = await self.get_couple_cooldown(chat_id)
        if last_used is None:
            return 0.0
        elapsed = (utcnow() - last_used).total_seconds()
        return max(0.0, cooldown_seconds - elapsed)

    async def try_acquire_couple_cooldown(
        self, chat_id: int, cooldown_seconds: int
    ) -> Tuple[bool, float]:
        """Atomically claim the cooldown slot for a chat.

        Returns ``(acquired, remaining_seconds)``.

        The claim is a single atomic ``find_one_and_update`` that always writes
        the new timestamp and hands back the *previous* one.  Whoever observes
        an expired (or absent) previous timestamp is the one true winner; a
        loser simply restores the value it just read, which is exactly the
        timestamp the winner wrote, so the rollback can never clear a live
        cooldown.  This deliberately does not depend on a unique index, because
        index creation is allowed to fail on restricted clusters.
        """
        now = utcnow()
        chat_id = int(chat_id)
        try:
            before = await self.couple_cooldowns.find_one_and_update(
                {"chat_id": chat_id},
                {
                    "$set": {"last_used": now},
                    "$setOnInsert": {"chat_id": chat_id},
                    "$inc": {"uses": 1},
                },
                upsert=True,
                return_document=ReturnDocument.BEFORE,
            )
        except PyMongoError as exc:
            # With the unique index in place, a concurrent first-ever call can
            # lose the upsert race with a duplicate-key error: that means the
            # other caller won and the cooldown is now active.
            if "duplicate key" in str(exc).lower():
                remaining = await self.couple_cooldown_remaining(chat_id, cooldown_seconds)
                return False, max(remaining, 1.0)
            log.error("try_acquire_couple_cooldown(%s) failed: %s", chat_id, exc)
            raise DatabaseError("Could not read couple cooldown") from exc

        previous = _as_utc(before.get("last_used")) if before else None
        if previous is None:
            return True, 0.0

        elapsed = (now - previous).total_seconds()
        if elapsed >= cooldown_seconds:
            return True, 0.0

        # Lost the race (or still cooling down): put the old timestamp back.
        try:
            await self.couple_cooldowns.update_one(
                {"chat_id": chat_id},
                {"$set": {"last_used": previous}, "$inc": {"uses": -1}},
            )
        except PyMongoError as exc:
            log.error("couple cooldown rollback for %s failed: %s", chat_id, exc)

        return False, max(0.0, cooldown_seconds - elapsed)

    async def release_couple_cooldown(self, chat_id: int) -> None:
        """Roll the cooldown back if sending the announcement failed."""
        try:
            await self.couple_cooldowns.update_one(
                {"chat_id": int(chat_id)},
                {"$unset": {"last_used": ""}, "$inc": {"uses": -1}},
            )
        except PyMongoError as exc:
            log.error("release_couple_cooldown(%s) failed: %s", chat_id, exc)

    async def record_couple(
        self,
        chat_id: int,
        first: Dict[str, Any],
        second: Dict[str, Any],
    ) -> None:
        """Persist the chosen pair so the group can be shown the same couple."""
        try:
            await self.couples.insert_one(
                {
                    "chat_id": int(chat_id),
                    "first": first,
                    "second": second,
                    "created_at": utcnow(),
                }
            )
        except PyMongoError as exc:
            log.error("record_couple(%s) failed: %s", chat_id, exc)

    async def get_last_couple(self, chat_id: int) -> Optional[Dict[str, Any]]:
        try:
            cursor = (
                self.couples.find({"chat_id": int(chat_id)})
                .sort("created_at", DESCENDING)
                .limit(1)
            )
            docs = await cursor.to_list(length=1)
            return docs[0] if docs else None
        except PyMongoError as exc:
            log.error("get_last_couple(%s) failed: %s", chat_id, exc)
            return None

    # ------------------------------------------------------------ bulk utils
    async def bulk_mark_inactive(self, collection: str, ids: List[int]) -> None:
        """Batch-deactivate destinations that failed during a broadcast."""
        if not ids:
            return
        key = "user_id" if collection == "users" else "chat_id"
        target = self.users if collection == "users" else self.groups
        unique = [int(_id) for _id in dict.fromkeys(ids)]
        update = {"$set": {"active": False, "inactive_at": utcnow()}}

        # Chunked so a very large broadcast cannot build an oversized query.
        for start in range(0, len(unique), _BULK_CHUNK):
            chunk = unique[start : start + _BULK_CHUNK]
            try:
                await target.update_many({key: {"$in": chunk}}, update)
            except PyMongoError as exc:
                log.error("bulk_mark_inactive(%s) failed: %s", collection, exc)

    async def stats(self) -> Dict[str, int]:
        return {
            "users": await self.count_users(active_only=False),
            "active_users": await self.count_users(active_only=True),
            "groups": await self.count_groups(active_only=False),
            "active_groups": await self.count_groups(active_only=True),
        }


_database: Optional[Database] = None


def get_database() -> Database:
    if _database is None:
        raise DatabaseError("Database has not been initialised yet.")
    return _database


def set_database(database: Database) -> None:
    global _database
    _database = database
