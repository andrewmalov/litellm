from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .state import sweep
from .store import RedisPoolStore


@dataclass(frozen=True, slots=True)
class RecoveryWorker:
    store: RedisPoolStore
    refresh_catalog: Callable[[], Awaitable[None]] | None = None

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.store.config.heartbeat_seconds)
            await self.cycle()

    async def cycle(self) -> None:
        if self.refresh_catalog is not None:
            await self.refresh_catalog()
        await self.store.transact(lambda snapshot, now: sweep(snapshot, self.store.config.resolve(snapshot), now))
