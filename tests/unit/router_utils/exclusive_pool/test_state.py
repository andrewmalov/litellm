from typing import Final

from litellm.router_utils.exclusive_pool.models import Admission, Limits, PoolConfig, Resource
from litellm.router_utils.exclusive_pool.state import acquire, complete, empty_state, start


def config() -> PoolConfig:
    return PoolConfig(
        pool_id="pool",
        models=("model",),
        primaries=(Resource(id="a", upstream_id="physical-a"), Resource(id="b", upstream_id="physical-b")),
        fallback=Resource(id="f", upstream_id="physical-f", limits=Limits(parallel=2, rpm=100, tpm=10000)),
        client_limits=Limits(parallel=2, rpm=100, tpm=10000),
    )


def request(key: str, device: str = "device", operation: str | None = None) -> Admission:
    return Admission(client=key, device=device, operation=operation or key, tokens=10, eligible=("a", "b", "f"))


def test_distinct_keys_receive_ordered_exclusive_primaries_then_shared_fallback() -> None:
    cfg: Final = config()
    first: Final = acquire(empty_state(cfg), cfg, request("one"), now=0)
    second: Final = acquire(first.state, cfg, request("two"), now=0)
    third: Final = acquire(second.state, cfg, request("three"), now=0)
    fourth: Final = acquire(third.state, cfg, request("four"), now=0)
    assert tuple(result.resource for result in (first, second, third, fourth)) == ("a", "b", "f", "f")
    assert fourth.state.cursor == "b"
    assert acquire(fourth.state, cfg, request("five"), now=0).failure.code == "endpoint_pool_exhausted"


def test_conflicts_and_limits_do_not_refresh_a_binding() -> None:
    cfg: Final = config()
    bound: Final = acquire(empty_state(cfg), cfg, request("one"), now=0)
    finished: Final = complete(start(bound.state, "one", now=0), cfg, "one", now=10, actual_tokens=5)
    conflict: Final = acquire(finished, cfg, request("one", "other", "conflict"), now=900)
    assert conflict.failure.code == "key_bound_to_another_device"
    assert conflict.failure.retry_after == 10
    assert conflict.state.sessions == finished.sessions
    assert conflict.state.charges == finished.charges
    rebound: Final = acquire(conflict.state, cfg, request("one", "other", "new"), now=910)
    assert rebound.resource == "b"


def test_live_request_holds_binding_beyond_idle_and_late_completion_is_idempotent() -> None:
    cfg: Final = config()
    allocated: Final = acquire(empty_state(cfg), cfg, request("one"), now=0)
    active: Final = start(allocated.state, "one", now=0)
    conflict: Final = acquire(active, cfg, request("one", "other", "conflict"), now=1000)
    assert conflict.failure.code == "key_bound_to_another_device"
    ended: Final = complete(active, cfg, "one", now=1100, actual_tokens=5)
    assert complete(ended, cfg, "one", now=1500, actual_tokens=5) == ended
    assert acquire(ended, cfg, request("one", "other", "new"), now=1999).failure.code == "key_bound_to_another_device"
    assert acquire(ended, cfg, request("one", "other", "new"), now=2000).resource == "b"


def test_same_device_parallelism_is_shared_across_primary_and_fallback() -> None:
    cfg: Final = config()
    first: Final = acquire(empty_state(cfg), cfg, request("one"), now=0)
    second: Final = acquire(first.state, cfg, request("one", operation="parallel"), now=1)
    assert second.resource == first.resource
    limited: Final = acquire(second.state, cfg, request("one", operation="third"), now=2)
    assert limited.failure.code == "client_rate_limit_exceeded"
    assert limited.state.sessions == second.state.sessions
    assert limited.state.charges == second.state.charges


def test_unsent_failure_rolls_back_new_bindings_without_refreshing_existing_bindings() -> None:
    from litellm.router_utils.exclusive_pool.state import abort

    cfg: Final = config()
    first: Final = acquire(empty_state(cfg), cfg, request("one"), now=0)
    assert abort(first.state, cfg, "one", now=100).sessions == ()
    accepted: Final = complete(start(first.state, "one", now=0), cfg, "one", now=10)
    pending: Final = acquire(accepted, cfg, request("one", operation="unsent"), now=100)
    rejected: Final = abort(pending.state, cfg, "unsent", now=200)
    assert rejected.sessions == accepted.sessions
    assert rejected.operations == ()


def test_full_fallback_does_not_migrate_an_idle_bound_client_to_a_free_primary() -> None:
    cfg: Final = config()
    first: Final = acquire(empty_state(cfg), cfg, request("one"), now=0)
    second: Final = acquire(first.state, cfg, request("two"), now=0)
    fallback: Final = acquire(second.state, cfg, request("three"), now=100)
    settled: Final = complete(complete(fallback.state, cfg, "one", now=0), cfg, "two", now=0)
    other: Final = acquire(settled, cfg, request("four"), now=100)
    full: Final = acquire(other.state, cfg, request("three", operation="again"), now=901)
    assert full.failure.code == "endpoint_pool_exhausted"


def test_quarantined_upstream_cannot_be_reassigned_even_after_idle_expiry() -> None:
    from litellm.router_utils.exclusive_pool.state import quarantine

    cfg: Final = config()
    pending: Final = acquire(empty_state(cfg), cfg, request("one"), now=0)
    uncertain: Final = quarantine(start(pending.state, "one", now=0), "one")
    next_client: Final = acquire(uncertain, cfg, request("two"), now=5000)
    assert next_client.resource == "b"
    assert next_client.state.operations[0].id == "one"


def test_conservative_token_reservations_are_reconciled_from_usage() -> None:
    cfg: Final = config().model_copy(update={"client_limits": Limits(parallel=2, rpm=100, tpm=15)})
    pending: Final = acquire(empty_state(cfg), cfg, request("one"), now=0)
    denied: Final = acquire(pending.state, cfg, request("one", operation="two"), now=1)
    assert denied.failure.code == "client_rate_limit_exceeded"
    ended: Final = complete(start(pending.state, "one", now=0), cfg, "one", now=1, actual_tokens=5)
    assert acquire(ended, cfg, request("one", operation="three"), now=2).resource == "a"


def test_inflight_limit_and_client_rpm_cover_aliases_and_retries_share_one_rpm_charge() -> None:
    cfg: Final = config().model_copy(update={"client_limits": Limits(parallel=2, rpm=1, tpm=10000)})
    pending: Final = acquire(empty_state(cfg), cfg, request("one"), now=0)
    ended: Final = complete(start(pending.state, "one", now=0), cfg, "one", now=1, actual_tokens=5)
    retry: Final = acquire(ended, cfg, request("one", operation="retry").model_copy(update={"logical": "one"}), now=2)
    assert retry.resource == "a"
    assert (
        acquire(retry.state, cfg, request("one", operation="new"), now=2).failure.code == "client_rate_limit_exceeded"
    )


def test_device_conflicts_are_observable_without_refreshing_the_lease() -> None:
    cfg: Final = config()
    bound: Final = acquire(empty_state(cfg), cfg, request("key-digest"), now=0)
    rejected: Final = acquire(bound.state, cfg, request("key-digest", "other-device-digest", "denied"), now=100)
    assert rejected.state.sessions == bound.state.sessions
    assert rejected.state.events[-1].kind == "device_conflict"
    assert rejected.state.events[-1].client == "key-digest"


def test_dead_unsent_reservations_are_reclaimed_but_started_work_is_quarantined() -> None:
    from litellm.router_utils.exclusive_pool.state import sweep

    cfg: Final = config()
    pending: Final = acquire(empty_state(cfg), cfg, request("one"), now=0)
    unsent: Final = sweep(pending.state, cfg, now=cfg.heartbeat_timeout)
    assert unsent.operations == ()
    assert unsent.sessions == ()
    started: Final = start(pending.state, "one", now=0)
    uncertain: Final = sweep(started, cfg, now=cfg.heartbeat_timeout)
    assert uncertain.operations[0].quarantined
    assert acquire(uncertain, cfg, request("two"), now=10000).resource == "b"


def test_sweep_removes_expired_events_and_idle_bindings() -> None:
    from litellm.router_utils.exclusive_pool.models import Event
    from litellm.router_utils.exclusive_pool.state import record_event, sweep

    cfg: Final = config().model_copy(update={"event_retention_seconds": 10})
    initial: Final = acquire(empty_state(cfg), cfg, request("one"), now=0)
    ended: Final = complete(start(initial.state, "one", now=0), cfg, "one", now=1)
    audited: Final = record_event(ended, cfg, Event(time=1, kind="test"))
    swept: Final = sweep(audited, cfg, now=902)
    assert swept.events == ()
    assert swept.sessions == ()


def test_account_reference_without_live_session_fails_before_any_reservation() -> None:
    cfg: Final = config()
    initial: Final = empty_state(cfg)
    outcome: Final = acquire(initial, cfg, request("one").model_copy(update={"requires_binding": True}), now=0)
    assert outcome.kind == "denied"
    assert outcome.failure.code == "pool_reference_binding_required"
    assert outcome.state == initial


def test_pinned_account_cannot_fallback_after_failure_even_for_plain_text() -> None:
    from litellm.router_utils.exclusive_pool.recovery import ErrorPolicy, fail

    cfg: Final = config()
    first: Final = acquire(empty_state(cfg), cfg, request("one").model_copy(update={"pin_upstream": True}), now=0)
    broken: Final = fail(
        start(first.state, "one", now=0),
        cfg,
        "one",
        ErrorPolicy(kind="cooldown", confirmed_end=True, base_seconds=10),
        now=1,
        jitter=0,
    )
    denied: Final = acquire(broken, cfg, request("one", operation="next"), now=2)
    assert denied.kind == "denied"
    assert denied.failure.code == "endpoint_pool_exhausted"
    assert denied.state.charges == broken.charges
    expired: Final = acquire(
        broken, cfg, request("one", operation="late").model_copy(update={"requires_binding": True}), now=1000
    )
    assert expired.kind == "denied"
    assert expired.failure.code == "pool_reference_binding_required"


def test_provider_count_reservation_is_atomic_and_checks_both_quota_scopes() -> None:
    from litellm.router_utils.exclusive_pool.state import reserve_counted_tokens

    cfg: Final = config()
    initial: Final = acquire(empty_state(cfg), cfg, request("one").model_copy(update={"needs_count": True}), now=0)
    counted: Final = reserve_counted_tokens(initial.state, cfg, "one", 9000, None, now=1)
    assert counted.kind == "granted"
    assert all(charge.tokens == 9000 for charge in counted.state.charges)
    assert counted.state.operations[0].counted
    denied: Final = reserve_counted_tokens(initial.state, cfg, "one", cfg.client_limits.tpm + 1, None, now=1)
    assert denied.kind == "denied"
    assert denied.failure.code == "client_rate_limit_exceeded"
    assert denied.state == initial.state
    resource_denied: Final = reserve_counted_tokens(initial.state, cfg, "one", 60001, Limits(tpm=100000), now=1)
    assert resource_denied.kind == "denied"
    assert resource_denied.failure.code == "endpoint_pool_exhausted"
    assert resource_denied.state == initial.state


def test_unsent_server_context_does_not_pin_an_established_text_session() -> None:
    from litellm.router_utils.exclusive_pool.state import abort

    cfg: Final = config()
    first: Final = acquire(empty_state(cfg), cfg, request("one"), now=0)
    established: Final = complete(start(first.state, "one", now=0), cfg, "one", now=1)
    pending: Final = acquire(
        established,
        cfg,
        request("one", operation="tools").model_copy(update={"pin_upstream": True, "needs_count": True}),
        now=2,
    )
    aborted: Final = abort(pending.state, cfg, "tools", now=3)
    assert aborted.sessions == established.sessions
    assert not start(pending.state, "tools", now=3).operations[0].started


def test_pending_server_context_cannot_start_after_parallel_failure_invalidates_assignment() -> None:
    from litellm.router_utils.exclusive_pool.recovery import ErrorPolicy, fail
    from litellm.router_utils.exclusive_pool.state import reserve_counted_tokens

    cfg: Final = config()
    first: Final = acquire(empty_state(cfg), cfg, request("one"), now=0)
    established: Final = complete(start(first.state, "one", now=0), cfg, "one", now=1)
    text: Final = acquire(established, cfg, request("one", operation="text"), now=2)
    pending: Final = acquire(
        start(text.state, "text", now=2),
        cfg,
        request("one", operation="tools").model_copy(update={"needs_count": True, "pin_upstream": True}),
        now=3,
    )
    failed: Final = fail(
        pending.state, cfg, "text", ErrorPolicy(kind="cooldown", confirmed_end=True, base_seconds=10), now=4, jitter=0
    )
    counted: Final = reserve_counted_tokens(failed, cfg, "tools", 100, None, now=5)
    assert counted.kind == "granted"
    assert start(counted.state, "tools", now=6) == counted.state
    assert counted.state.sessions[0].pinned_upstream is None
