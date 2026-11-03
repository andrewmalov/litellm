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
