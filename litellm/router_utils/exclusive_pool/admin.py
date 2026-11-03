from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from pathlib import Path
from typing import Final

from redis.asyncio import Redis

from .models import CatalogEntry, Denied, Failure, FrozenModel, Health, PoolConfig, Snapshot
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


def reconfigure(snapshot: Snapshot, config: PoolConfig, previous: PoolConfig | None = None) -> Snapshot | Denied:
    if snapshot.operations:
        return Denied(state=snapshot, failure=unavailable())
    prepared: Final = _prepare_catalog(snapshot, config, previous)
    if isinstance(prepared, Denied):
        return prepared
    resolved: Final = config.resolve(prepared)
    ids: Final = frozenset(r.id for r in resolved.resources)
    return prepared.model_copy(
        update={
            "fingerprint": config.fingerprint,
            "health": tuple(
                next((h for h in snapshot.health if h.resource == resource.id), Health(resource=resource.id))
                for resource in resolved.resources
            ),
            "sessions": tuple(
                s.model_copy(update={"resource": None}) if s.resource not in ids else s for s in snapshot.sessions
            ),
            "cursor": snapshot.cursor if snapshot.cursor in ids else None,
        }
    )


def _prepare_catalog(snapshot: Snapshot, config: PoolConfig, previous: PoolConfig | None) -> Snapshot | Denied:
    if not config.discovery or snapshot.catalog or not snapshot.health:
        return snapshot
    if (
        previous is None
        or previous.discovery
        or previous.pool_id != config.pool_id
        or previous.fingerprint != snapshot.fingerprint
        or len(previous.models) != 1
        or frozenset(h.resource for h in snapshot.health) != frozenset(r.id for r in previous.resources)
    ):
        return Denied(state=snapshot, failure=unavailable())
    return snapshot.model_copy(
        update={
            "catalog": tuple(
                CatalogEntry(
                    resource=resource,
                    model=previous.models[0],
                    role="fallback" if previous.is_fallback(resource.id) else "primary",
                    active=False,
                )
                for resource in previous.resources
            ),
        }
    )


async def run(command: str, operation: str, resource: str, previous_config: str = "") -> int:
    config: Final = PoolConfig.model_validate_json(Path(os.environ["EXCLUSIVE_POOL_CONFIG"]).read_bytes())
    previous: Final = PoolConfig.model_validate_json(Path(previous_config).read_bytes()) if previous_config else None
    client: Final[Redis[bytes]] = Redis.from_url(
        os.environ["EXCLUSIVE_POOL_REDIS_URL"], socket_timeout=3, socket_connect_timeout=3
    )
    store: Final = RedisPoolStore(client, config)
    try:
        result: Final = await _command(store, command, operation, resource, previous)
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


async def _command(
    store: RedisPoolStore, command: str, operation: str, resource: str, previous: PoolConfig | None = None
) -> Snapshot | Denied | Failure:
    match command:
        case "init":
            return await store.initialize()
        case "status":
            return await store.read()
        case "confirm":
            return await store.transact(
                lambda snapshot, now: confirm_idle(snapshot, store.config.resolve(snapshot), operation, now)
            )
        case "enable":
            return await store.transact(lambda snapshot, now: enable(snapshot, resource, uuid.uuid4().hex))
        case "reconfigure":
            return await store.transact(
                lambda snapshot, now: reconfigure(snapshot, store.config, previous), allow_config_change=True
            )
        case _:
            return unavailable()


class _Arguments(FrozenModel):
    command: str
    operation: str = ""
    resource: str = ""
    previous_config: str = ""


def main() -> int:
    parser: Final = argparse.ArgumentParser(description="Manage exclusive endpoint pool coordination state")
    commands: Final = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("init").add_argument("--confirm-upstream-idle", required=True, action="store_true")
    confirm: Final = commands.add_parser("confirm")
    confirm.add_argument("--confirm-upstream-idle", required=True, action="store_true")
    confirm.add_argument("--operation", required=True)
    commands.add_parser("enable").add_argument("--resource", required=True)
    commands.add_parser("reconfigure").add_argument("--previous-config", default="")
    arguments: Final = parser.parse_args()
    parsed: Final = _Arguments.model_validate(arguments, from_attributes=True)
    return asyncio.run(run(parsed.command, parsed.operation, parsed.resource, parsed.previous_config))


if __name__ == "__main__":
    raise SystemExit(main())
