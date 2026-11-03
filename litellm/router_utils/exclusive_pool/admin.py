from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from pathlib import Path
from typing import Final

from redis.asyncio import Redis

from .models import Denied, Failure, FrozenModel, Health, PoolConfig, Snapshot
from .recovery import enable
from .state import abort, complete, unavailable
from .store import RedisPoolStore


def confirm_idle(snapshot: Snapshot, config: PoolConfig, operation: str, now: float) -> Snapshot | Denied:
    active: Final = next((op for op in snapshot.operations if op.id == operation), None)
    if active is None:
        return snapshot
    if not active.quarantined and now - active.heartbeat < config.heartbeat_timeout:
        return Denied(
            state=snapshot,
            failure=Failure(
                code="routing_state_unavailable", status=409, message="The operation still has a live heartbeat"
            ),
        )
    return complete(snapshot, config, operation, now) if active.started else abort(snapshot, config, operation, now)


def reconfigure(snapshot: Snapshot, config: PoolConfig) -> Snapshot | Denied:
    if snapshot.operations:
        return Denied(state=snapshot, failure=unavailable())
    ids: Final = frozenset(r.id for r in config.resources)
    return snapshot.model_copy(
        update={
            "fingerprint": config.fingerprint,
            "health": tuple(
                next((h for h in snapshot.health if h.resource == resource.id), Health(resource=resource.id))
                for resource in config.resources
            ),
            "sessions": tuple(
                s.model_copy(update={"resource": None}) if s.resource not in ids else s for s in snapshot.sessions
            ),
            "cursor": snapshot.cursor if snapshot.cursor in ids else None,
        }
    )


async def run(command: str, operation: str, resource: str) -> int:
    config: Final = PoolConfig.model_validate_json(Path(os.environ["EXCLUSIVE_POOL_CONFIG"]).read_bytes())
    client: Final[Redis[bytes]] = Redis.from_url(
        os.environ["EXCLUSIVE_POOL_REDIS_URL"], socket_timeout=3, socket_connect_timeout=3
    )
    store: Final = RedisPoolStore(client, config)
    try:
        result: Final = await _command(store, command, operation, resource)
        match result:
            case Failure():
                sys.stdout.write(result.model_dump_json() + "\n")
                return 1
            case Denied(failure=failure):
                sys.stdout.write(failure.model_dump_json() + "\n")
                return 1
            case Snapshot():
                sys.stdout.write(result.model_dump_json() + "\n")
                return 0
    finally:
        await client.close()


async def _command(store: RedisPoolStore, command: str, operation: str, resource: str) -> Snapshot | Denied | Failure:
    match command:
        case "init":
            return await store.initialize()
        case "status":
            return await store.read()
        case "confirm":
            return await store.transact(lambda snapshot, now: confirm_idle(snapshot, store.config, operation, now))
        case "enable":
            return await store.transact(lambda snapshot, now: enable(snapshot, resource, uuid.uuid4().hex))
        case "reconfigure":
            return await store.transact(
                lambda snapshot, now: reconfigure(snapshot, store.config), allow_config_change=True
            )
        case _:
            return unavailable()


class _Arguments(FrozenModel):
    command: str
    operation: str = ""
    resource: str = ""


def main() -> int:
    parser: Final = argparse.ArgumentParser(description="Manage exclusive endpoint pool coordination state")
    commands: Final = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("init").add_argument("--confirm-upstream-idle", required=True, action="store_true")
    confirm: Final = commands.add_parser("confirm")
    confirm.add_argument("--confirm-upstream-idle", required=True, action="store_true")
    confirm.add_argument("--operation", required=True)
    commands.add_parser("enable").add_argument("--resource", required=True)
    commands.add_parser("reconfigure")
    arguments: Final = parser.parse_args()
    parsed: Final = _Arguments.model_validate(arguments, from_attributes=True)
    return asyncio.run(run(parsed.command, parsed.operation, parsed.resource))


if __name__ == "__main__":
    raise SystemExit(main())
