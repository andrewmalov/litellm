from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final, TypeVar

from pydantic import TypeAdapter, ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError, WatchError

from . import state as transitions
from .discovery import CatalogRow, discover, reconcile
from .models import Admission, CatalogEntry, Failure, Outcome, PoolConfig, Snapshot

T: Final = TypeVar("T", bound=Outcome | Snapshot)
_TEXT: Final = TypeAdapter[bytes | str | None](bytes | str | None)
_TIME: Final = TypeAdapter(tuple[int, int])


@dataclass(frozen=True, slots=True)
class RedisPoolStore:
    redis: Redis[bytes]
    config: PoolConfig

    @property
    def key(self) -> str:
        return f"litellm:exclusive-pool:{{{self.config.pool_id}}}:state"

    def _valid(self, snapshot: Snapshot) -> bool:
        expected: Final = frozenset(r.id for r in self.config.resolve(snapshot).resources)
        return (
            snapshot.fingerprint == self.config.fingerprint
            and len(snapshot.health) == len(expected)
            and frozenset(h.resource for h in snapshot.health) == expected
            and all(op.resource in expected for op in snapshot.operations)
            and all(s.resource is None or s.resource in expected for s in snapshot.sessions)
        )

    async def read(self) -> Snapshot | Failure:
        try:
            raw: Final = _TEXT.validate_python(await self.redis.get(self.key))
            if raw is None:
                return transitions.unavailable()
            parsed: Final = Snapshot.model_validate_json(raw)
            return parsed if self._valid(parsed) else transitions.unavailable()
        except (RedisError, ValidationError):
            return transitions.unavailable()

    async def initialize(self) -> Snapshot | Failure:
        try:
            await self.redis.set(self.key, transitions.empty_state(self.config).model_dump_json(), nx=True)
        except RedisError:
            return transitions.unavailable()
        return await self.read()

    async def acquire(self, request: Admission) -> Outcome | Failure:
        return await self.transact(
            lambda snapshot, now: transitions.acquire(snapshot, self.config.resolve(snapshot), request, now)
        )

    async def transact(
        self, change: Callable[[Snapshot, float], T], *, allow_config_change: bool = False
    ) -> T | Failure:
        for _ in range(64):
            match await self._attempt(change, allow_config_change):
                case _Contended():
                    await asyncio.sleep(0)
                case result:
                    return result
        return transitions.unavailable()

    async def synchronize(
        self, load: Callable[[], Awaitable[tuple[CatalogRow, ...] | Failure]], *, force: bool = False
    ) -> Snapshot | Failure:
        return await self._synchronize(load, force, None, 64)

    async def _synchronize(
        self,
        load: Callable[[], Awaitable[tuple[CatalogRow, ...] | Failure]],
        force: bool,
        cached: _CatalogCache | None,
        remaining: int,
    ) -> Snapshot | Failure:
        if remaining == 0:
            return transitions.unavailable()
        result: Final = await self._catalog_attempt(load, force, cached)
        if isinstance(result, _CatalogContended):
            await asyncio.sleep(0)
            return await self._synchronize(load, force, result.cached, remaining - 1)
        return result

    async def _catalog_read(
        self, load: Callable[[], Awaitable[tuple[CatalogRow, ...] | Failure]], generation: int
    ) -> _CatalogCache | Failure:
        try:
            rows: Final = await asyncio.wait_for(load(), timeout=3)
        except TimeoutError:
            return transitions.unavailable()
        if isinstance(rows, Failure):
            return rows
        catalog: Final = discover(rows, self.config)
        return catalog if isinstance(catalog, Failure) else _CatalogCache(generation, catalog)

    async def _catalog_attempt(
        self,
        load: Callable[[], Awaitable[tuple[CatalogRow, ...] | Failure]],
        force: bool,
        cached: _CatalogCache | None,
    ) -> Snapshot | Failure | _CatalogContended:
        try:
            async with self.redis.pipeline(transaction=True) as transaction:
                await transaction.watch(self.key)
                raw: Final = _TEXT.validate_python(await transaction.get(self.key))
                if raw is None:
                    return transitions.unavailable()
                snapshot: Final = Snapshot.model_validate_json(raw)
                if not self._valid(snapshot):
                    return transitions.unavailable()
                seconds, microseconds = _TIME.validate_python(await transaction.time())
                now: Final = seconds + microseconds / 1000000
                if (
                    not force
                    and snapshot.discovered_at is not None
                    and now - snapshot.discovered_at < self.config.heartbeat_seconds
                ):
                    return snapshot
                loaded: Final = (
                    cached
                    if cached is not None and cached.generation == snapshot.catalog_generation
                    else await self._catalog_read(load, snapshot.catalog_generation)
                )
                if isinstance(loaded, Failure):
                    return loaded
                updated: Final = reconcile(snapshot, self.config, loaded.catalog, now)
                if isinstance(updated, Failure):
                    return updated
                try:
                    transaction.multi()
                    transaction.set(self.key, updated.model_dump_json())
                    await transaction.execute()  # pyright: ignore[reportUnknownMemberType]  # redis pipeline stubs omit the result type
                except WatchError:
                    return _CatalogContended(loaded)
                return updated
        except WatchError:
            return _CatalogContended(cached)
        except (RedisError, ValidationError):
            return transitions.unavailable()

    async def _attempt(
        self, change: Callable[[Snapshot, float], T], allow_config_change: bool
    ) -> T | Failure | _Contended:
        try:
            async with self.redis.pipeline(transaction=True) as transaction:
                await transaction.watch(self.key)
                raw: Final = _TEXT.validate_python(await transaction.get(self.key))
                if raw is None:
                    return transitions.unavailable()
                snapshot: Final = Snapshot.model_validate_json(raw)
                if not allow_config_change and not self._valid(snapshot):
                    return transitions.unavailable()
                seconds, microseconds = _TIME.validate_python(await transaction.time())
                result: Final = change(snapshot, seconds + microseconds / 1000000)
                updated: Final = result if isinstance(result, Snapshot) else result.state
                if updated == snapshot:
                    return result
                transaction.multi()
                transaction.set(self.key, updated.model_dump_json())
                await transaction.execute()  # pyright: ignore[reportUnknownMemberType]  # redis pipeline stubs omit the result type
                return result
        except WatchError:
            return _Contended()
        except (RedisError, ValidationError):
            return transitions.unavailable()


@dataclass(frozen=True, slots=True)
class _Contended:
    pass


@dataclass(frozen=True, slots=True)
class _CatalogCache:
    generation: int
    catalog: tuple[CatalogEntry, ...]


@dataclass(frozen=True, slots=True)
class _CatalogContended:
    cached: _CatalogCache | None
