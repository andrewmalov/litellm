from __future__ import annotations

import asyncio
import secrets
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final

from .models import Failure, Granted
from .recovery import claim_probe, classify, fail, probe_success
from .request import Ticket
from .state import abort, sweep
from .store import RedisPoolStore


@dataclass(frozen=True, slots=True)
class RecoveryWorker:
    store: RedisPoolStore
    run_probe: Callable[[Ticket], Awaitable[bool]]
    refresh_catalog: Callable[[], Awaitable[None]] | None = None

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.store.config.heartbeat_seconds)
            await self.cycle()

    async def cycle(self) -> None:
        if self.refresh_catalog is not None:
            await self.refresh_catalog()
        swept: Final = await self.store.transact(
            lambda snapshot, now: sweep(snapshot, self.store.config.resolve(snapshot), now)
        )
        if isinstance(swept, Failure):
            return
        for resource in self.store.config.resolve(swept).selectable:
            await self.tick(resource.id)

    async def tick(self, resource: str) -> None:
        operation: Final = uuid.uuid4().hex
        claim: Final = await self.store.transact(
            lambda snapshot, now: claim_probe(snapshot, self.store.config.resolve(snapshot), resource, operation, now)
        )
        if not isinstance(claim, Granted):
            return
        ticket: Final = Ticket(operation=operation, client="", device="", resource=resource, probe=True)
        try:
            ran: Final = await self.run_probe(ticket)
        except Exception as exception:  # noqa: BLE001  # Router surfaces provider-specific exception classes
            policy: Final = classify(exception)
            jitter: Final = (secrets.randbelow(999) + 1) / 10000
            await self.store.transact(
                lambda snapshot, now: fail(
                    snapshot, self.store.config.resolve(snapshot), operation, policy, now, jitter
                )
            )
            return
        if ran:
            await self.store.transact(
                lambda snapshot, now: probe_success(snapshot, self.store.config.resolve(snapshot), operation, now)
            )
        else:
            await self.store.transact(
                lambda snapshot, now: abort(snapshot, self.store.config.resolve(snapshot), operation, now)
            )
