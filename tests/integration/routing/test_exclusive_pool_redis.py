import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from typing import Final

import pytest
import pytest_asyncio
from redis.asyncio import Redis

from litellm.router_utils.exclusive_pool.models import Admission, Denied, Failure, Granted, Limits, PoolConfig, Resource
from litellm.router_utils.exclusive_pool.store import RedisPoolStore


@pytest_asyncio.fixture(loop_scope="function")
async def store() -> AsyncIterator[RedisPoolStore]:
    config: Final = PoolConfig(
        pool_id=f"test-{uuid.uuid4().hex}",
        models=("model",),
        primaries=(Resource(id="a", upstream_id="a"), Resource(id="b", upstream_id="b")),
        fallback=Resource(id="f", upstream_id="f", limits=Limits(parallel=20)),
        client_limits=Limits(parallel=2),
    )
    client: Final = Redis(host=os.environ["REDIS_HOST"], port=int(os.environ["REDIS_PORT"]), socket_timeout=2)
    pool: Final = RedisPoolStore(client, config)
    assert not isinstance(await pool.initialize(), Failure)
    try:
        yield pool
    finally:
        await client.delete(pool.key)
        await client.aclose()


@pytest.mark.asyncio
async def test_independent_coordinators_cannot_give_a_primary_to_two_keys(store: RedisPoolStore) -> None:
    peer: Final = RedisPoolStore(store.redis, store.config)
    replies: Final = await asyncio.gather(
        *(
            coordinator.acquire(
                Admission(client=str(index), device="d", operation=f"r{index}", tokens=10, eligible=("a", "b", "f"))
            )
            for index, coordinator in enumerate((store, peer) * 6)
        )
    )
    assert all(isinstance(reply, Granted) for reply in replies)
    assert sum(isinstance(reply, Granted) and reply.resource == "a" for reply in replies) == 1
    assert sum(isinstance(reply, Granted) and reply.resource == "b" for reply in replies) == 1
    assert sum(isinstance(reply, Granted) and reply.resource == "f" for reply in replies) == 10


@pytest.mark.asyncio
async def test_first_device_race_has_one_winner_and_lost_state_never_bootstraps_itself(store: RedisPoolStore) -> None:
    replies: Final = await asyncio.gather(
        *(
            store.acquire(
                Admission(client="same-key", device=device, operation=device, tokens=10, eligible=("a", "b", "f"))
            )
            for device in ("left", "right")
        )
    )
    assert sum(isinstance(reply, Granted) for reply in replies) == 1
    assert (
        sum(isinstance(reply, Denied) and reply.failure.code == "key_bound_to_another_device" for reply in replies) == 1
    )
    await store.redis.delete(store.key)
    lost: Final = await store.acquire(
        Admission(client="new", device="d", operation="new", tokens=10, eligible=("a", "b", "f"))
    )
    assert isinstance(lost, Failure) and lost.code == "routing_state_unavailable"
    assert await store.redis.exists(store.key) == 0


@pytest.mark.asyncio
async def test_only_one_recovery_worker_can_generate_a_probe(store: RedisPoolStore) -> None:
    from litellm.router_utils.exclusive_pool.request import Ticket
    from litellm.router_utils.exclusive_pool.worker import RecoveryWorker

    entered: Final = asyncio.Event()
    release: Final = asyncio.Event()
    await store.transact(
        lambda snapshot, now: snapshot.model_copy(
            update={
                "health": tuple(
                    h.model_copy(update={"status": "cooldown"}) if h.resource == "a" else h for h in snapshot.health
                )
            }
        )
    )

    async def probe(ticket: Ticket) -> bool:
        assert ticket.resource == "a"
        assert not entered.is_set(), "two independent workers generated on the same resource"
        entered.set()
        await release.wait()
        return True

    left: Final = RecoveryWorker(store, probe)
    right: Final = RecoveryWorker(RedisPoolStore(store.redis, store.config), probe)
    running: Final = asyncio.create_task(left.tick("a"))
    await asyncio.wait_for(entered.wait(), timeout=5)
    try:
        await right.tick("a")
    finally:
        release.set()
        await running
    recovered: Final = await store.read()
    assert not isinstance(recovered, Failure)
    assert recovered.health[0].status == "ready"


@pytest.mark.asyncio
async def test_incomplete_snapshot_fails_closed_instead_of_allocating(store: RedisPoolStore) -> None:
    from litellm.router_utils.exclusive_pool.models import Snapshot

    await store.redis.set(store.key, Snapshot(fingerprint=store.config.fingerprint).model_dump_json())
    result: Final = await store.acquire(
        Admission(client="c", device="d", operation="one", tokens=1, eligible=("a", "b", "f"))
    )
    assert isinstance(result, Failure) and result.code == "routing_state_unavailable"


@pytest.mark.asyncio
async def test_discovery_is_shared_and_retired_work_stays_visible_to_other_workers(store: RedisPoolStore) -> None:
    from litellm.router_utils.exclusive_pool.discovery import CatalogRow
    from litellm.router_utils.exclusive_pool.state import complete, start, unavailable

    config: Final = PoolConfig(pool_id=store.config.pool_id + "-dynamic", discovery=True, client_limits=Limits())
    dynamic: Final = RedisPoolStore(store.redis, config)
    peer: Final = RedisPoolStore(store.redis, config)
    row: Final = CatalogRow(
        model_id="a",
        model_name="model",
        model_info={"exclusive_pool": config.pool_id, "exclusive_pool_role": "primary"},
    )

    async def initial() -> tuple[CatalogRow, ...]:
        return (row,)

    async def empty() -> tuple[CatalogRow, ...]:
        return ()

    async def unavailable_source() -> Failure:
        return unavailable()

    try:
        assert not isinstance(await dynamic.initialize(), Failure)
        assert not isinstance(await dynamic.synchronize(initial, force=True), Failure)
        admitted: Final = await peer.acquire(
            Admission(client="c", device="d", operation="op", tokens=7, eligible=("a",))
        )
        assert isinstance(admitted, Granted) and admitted.resource == "a"
        await peer.transact(lambda snapshot, now: start(snapshot, "op", now))
        before_failure: Final = await store.redis.get(dynamic.key)
        assert isinstance(await dynamic.synchronize(unavailable_source, force=True), Failure)
        assert await store.redis.get(dynamic.key) == before_failure
        removed: Final = await dynamic.synchronize(empty, force=True)
        assert not isinstance(removed, Failure) and removed.operations[0].started
        denied: Final = await peer.acquire(
            Admission(client="new", device="d", operation="new", tokens=1, eligible=("a",))
        )
        assert isinstance(denied, Denied)
        await peer.transact(lambda snapshot, now: complete(snapshot, config.resolve(snapshot), "op", now))
        ended: Final = await dynamic.read()
        assert not isinstance(ended, Failure) and not ended.operations
        assert len(ended.charges) == 2
        await store.redis.delete(dynamic.key)
        assert isinstance(await dynamic.synchronize(initial, force=True), Failure)
        assert not await store.redis.exists(dynamic.key)
    finally:
        await store.redis.delete(dynamic.key)


@pytest.mark.asyncio
async def test_watch_retries_database_read_instead_of_publishing_a_stale_catalog(store: RedisPoolStore) -> None:
    from litellm.router_utils.exclusive_pool.discovery import CatalogRow

    config: Final = PoolConfig(pool_id=store.config.pool_id + "-race", discovery=True, client_limits=Limits())
    left: Final = RedisPoolStore(store.redis, config)
    right: Final = RedisPoolStore(store.redis, config)
    entered: Final = asyncio.Event()
    resume: Final = asyncio.Event()
    old: Final = CatalogRow(
        model_id="old",
        model_name="model",
        model_info={"exclusive_pool": config.pool_id, "exclusive_pool_role": "primary"},
    )
    new: Final = old.model_copy(update={"model_id": "new"})

    async def delayed_source() -> tuple[CatalogRow, ...]:
        if entered.is_set():
            return (new,)
        entered.set()
        await resume.wait()
        return (old,)

    async def fresh_source() -> tuple[CatalogRow, ...]:
        return (new,)

    try:
        await left.initialize()
        delayed: Final = asyncio.create_task(left.synchronize(delayed_source, force=True))
        await asyncio.wait_for(entered.wait(), timeout=5)
        try:
            assert not isinstance(await right.synchronize(fresh_source, force=True), Failure)
        finally:
            resume.set()
            await delayed
        final: Final = await left.read()
        assert not isinstance(final, Failure)
        assert tuple(entry.resource.id for entry in final.catalog) == ("new",)
        assert isinstance(
            await left.acquire(Admission(client="c", device="d", operation="op", tokens=1, eligible=("new",))), Granted
        )
    finally:
        await store.redis.delete(left.key)


@pytest.mark.asyncio
async def test_state_writes_during_every_database_read_do_not_starve_catalog_refresh(store: RedisPoolStore) -> None:
    from litellm.router_utils.exclusive_pool.discovery import CatalogRow
    from litellm.router_utils.exclusive_pool.models import Event

    config: Final = PoolConfig(pool_id=store.config.pool_id + "-busy", discovery=True, client_limits=Limits())
    dynamic: Final = RedisPoolStore(store.redis, config)
    peer: Final = RedisPoolStore(store.redis, config)

    async def load_during_traffic() -> tuple[CatalogRow, ...]:
        await peer.transact(
            lambda snapshot, now: snapshot.model_copy(
                update={"events": (*snapshot.events, Event(time=now, kind="traffic"))}
            )
        )
        return (
            CatalogRow(
                model_id="a",
                model_name="model",
                model_info={"exclusive_pool": config.pool_id, "exclusive_pool_role": "primary"},
            ),
        )

    try:
        await dynamic.initialize()
        refreshed: Final = await dynamic.synchronize(load_during_traffic, force=True)
        assert not isinstance(refreshed, Failure)
        assert refreshed.events[0].kind == "traffic"
        admitted: Final = await peer.acquire(
            Admission(client="c", device="d", operation="op", tokens=1, eligible=("a",))
        )
        assert isinstance(admitted, Granted) and admitted.resource == "a"
    finally:
        await store.redis.delete(dynamic.key)
