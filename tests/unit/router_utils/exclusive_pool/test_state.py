from functools import reduce
from typing import Final

import pytest

from litellm.router_utils.exclusive_pool.models import Admission, Denied, Granted, Limits, PoolConfig, Resource
from litellm.router_utils.exclusive_pool.state import (
    acquire,
    complete,
    empty_state,
    heartbeat,
    quarantine,
    start,
    sweep,
)


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


def test_changed_device_reuses_key_assignment_without_refreshing_until_completion() -> None:
    cfg: Final = config()
    bound: Final = acquire(empty_state(cfg), cfg, request("one"), now=0)
    finished: Final = complete(start(bound.state, "one", now=0), cfg, "one", now=10, actual_tokens=5)
    changed: Final = acquire(finished, cfg, request("one", "other", "changed"), now=900)
    assert changed.resource == bound.resource
    assert changed.state.sessions[0].generation == bound.state.sessions[0].generation
    assert changed.state.sessions[0].last_activity == finished.sessions[0].last_activity
    ended: Final = complete(start(changed.state, "changed", now=900), cfg, "changed", now=901)
    assert ended.sessions[0].last_activity == 901
    assert acquire(ended, cfg, request("one", "third", "new"), now=1801).resource == "b"


def test_live_request_holds_binding_beyond_idle_and_late_completion_is_idempotent() -> None:
    cfg: Final = config()
    allocated: Final = acquire(empty_state(cfg), cfg, request("one"), now=0)
    active: Final = start(allocated.state, "one", now=0)
    fresh: Final = reduce(lambda snapshot, now: heartbeat(snapshot, cfg, "one", now), range(50, 1001, 50), active)
    parallel: Final = acquire(fresh, cfg, request("one", "other", "parallel"), now=1001)
    assert parallel.resource == allocated.resource
    assert parallel.state.sessions[0].generation == allocated.state.sessions[0].generation
    ended: Final = complete(heartbeat(fresh, cfg, "one", now=1050), cfg, "one", now=1100, actual_tokens=5)
    assert complete(ended, cfg, "one", now=1500, actual_tokens=5) == ended
    assert acquire(ended, cfg, request("one", "other", "new"), now=1999).resource == allocated.resource
    assert acquire(ended, cfg, request("one", "other", "new"), now=2000).resource == "b"


def test_key_parallelism_is_shared_across_devices() -> None:
    cfg: Final = config()
    first: Final = acquire(empty_state(cfg), cfg, request("one"), now=0)
    second: Final = acquire(first.state, cfg, request("one", "other", operation="parallel"), now=1)
    assert second.resource == first.resource
    limited: Final = acquire(second.state, cfg, request("one", "third", operation="third"), now=2)
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


def test_backup_client_returns_to_a_free_primary_after_its_last_request() -> None:
    cfg: Final = config()
    first: Final = acquire(empty_state(cfg), cfg, request("one"), now=0)
    second: Final = acquire(first.state, cfg, request("two"), now=0)
    settled: Final = complete(complete(second.state, cfg, "one", now=0), cfg, "two", now=0)
    fallback: Final = acquire(settled, cfg, request("three"), now=880)
    assert isinstance(fallback, Granted) and fallback.resource == "f"
    concurrent: Final = acquire(fallback.state, cfg, request("three", operation="parallel"), now=901)
    assert isinstance(concurrent, Granted) and concurrent.resource == "f"
    ended: Final = complete(complete(concurrent.state, cfg, "three", now=902), cfg, "parallel", now=903)
    promoted: Final = acquire(ended, cfg, request("three", operation="again"), now=904)
    assert isinstance(promoted, Granted) and promoted.resource == "a"
    other: Final = acquire(promoted.state, cfg, request("four").model_copy(update={"eligible": ("a", "f")}), now=904)
    assert isinstance(other, Granted) and other.resource == "f"


def test_backup_server_reference_stays_on_its_original_upstream() -> None:
    cfg: Final = config()
    initial: Final = request("one").model_copy(update={"eligible": ("f",), "pin_upstream": True})
    backup: Final = acquire(empty_state(cfg), cfg, initial, now=0)
    assert isinstance(backup, Granted) and backup.resource == "f"
    ended: Final = complete(start(backup.state, "one", now=0), cfg, "one", now=1)
    followup: Final = request("one", operation="next").model_copy(update={"requires_binding": True})
    retained: Final = acquire(ended, cfg, followup, now=2)
    assert isinstance(retained, Granted) and retained.resource == "f"


def test_unlimited_backup_admits_multiple_keys_even_with_quarantine() -> None:
    defaults: Final = config()
    cfg: Final = PoolConfig.model_validate(
        {**defaults.model_dump(), "fallback": {"id": "f", "upstream_id": "physical-f", "limits": {"parallel": None}}}
    )
    on_backup: Final = request("first").model_copy(update={"eligible": ("f",)})
    first: Final = acquire(empty_state(cfg), cfg, on_backup, now=0)
    assert isinstance(first, Granted)
    quarantined: Final = quarantine(start(first.state, "first", now=0), "first")
    replies: Final = tuple(
        request(key).model_copy(update={"eligible": ("f",)}) for key in ("second", "third", "fourth")
    )
    first_shared: Final = acquire(quarantined, cfg, replies[0], now=20)
    second_shared: Final = acquire(first_shared.state, cfg, replies[1], now=20)
    third_shared: Final = acquire(second_shared.state, cfg, replies[2], now=20)
    assert all(
        isinstance(result, Granted) and result.resource == "f" for result in (first_shared, second_shared, third_shared)
    )


def test_stale_operation_allows_the_owner_before_expiry_and_reassignment_after_expiry() -> None:
    cfg: Final = config().model_copy(update={"client_limits": Limits(parallel=1)})
    initial: Final = acquire(empty_state(cfg), cfg, request("one"), now=0)
    assert isinstance(initial, Granted)
    active: Final = heartbeat(start(initial.state, "one", now=0), cfg, "one", now=50)
    retry: Final = request("one", operation="retry")
    before_expiry: Final = sweep(active, cfg, now=349)
    assert isinstance(acquire(before_expiry, cfg, retry, now=349), Granted)
    other: Final = request("two").model_copy(update={"eligible": (initial.resource,)})
    assert isinstance(acquire(before_expiry, cfg, other, now=349), Denied)
    after_expiry: Final = sweep(before_expiry, cfg, now=350)
    idle: Final = sweep(after_expiry, cfg, now=900)
    granted: Final = acquire(idle, cfg, other, now=900)
    assert isinstance(granted, Granted)
    assert granted.resource == initial.resource


@pytest.mark.parametrize("explicit", [False, True])
def test_uncertain_fallback_work_blocks_handoff_but_frees_the_owners_parallel_slot(explicit: bool) -> None:
    cfg: Final = config().model_copy(
        update={
            "client_limits": Limits(parallel=1),
            "fallback": Resource(id="f", upstream_id="physical-f", limits=Limits(parallel=1)),
        }
    )
    owner: Final = request("one").model_copy(update={"eligible": ("f",)})
    initial: Final = acquire(empty_state(cfg), cfg, owner, now=0)
    assert isinstance(initial, Granted)
    running: Final = start(initial.state, "one", now=0)
    uncertain: Final = quarantine(running, "one") if explicit else running
    now: Final = cfg.heartbeat_timeout

    assert isinstance(acquire(uncertain, cfg, owner, now=now), Denied)
    other: Final = request("two").model_copy(update={"eligible": ("f",)})
    assert isinstance(acquire(uncertain, cfg, other, now=now), Denied)
    another: Final = acquire(uncertain, cfg, owner.model_copy(update={"operation": "next"}), now=now)
    assert isinstance(another, Granted)
    assert another.resource == "f"
    assert isinstance(acquire(another.state, cfg, owner.model_copy(update={"operation": "extra"}), now=now), Denied)
    late_pulse: Final = heartbeat(another.state, cfg, "one", now=now + 1)
    ended: Final = complete(late_pulse, cfg, "next", now=now + 2)
    extra: Final = acquire(ended, cfg, owner.model_copy(update={"operation": "extra"}), now=now + 3)
    assert isinstance(extra, Granted) and extra.resource == "f"


def test_quarantine_reservation_counts_only_in_its_normal_quota_window() -> None:
    limits: Final = Limits(parallel=1, rpm=1, tpm=10)
    cfg: Final = config().model_copy(
        update={"client_limits": limits, "fallback": Resource(id="f", upstream_id="physical-f", limits=limits)}
    )
    owner: Final = request("one").model_copy(update={"eligible": ("f",)})
    initial: Final = acquire(empty_state(cfg), cfg, owner, now=0)
    assert isinstance(initial, Granted)
    uncertain: Final = quarantine(start(initial.state, "one", now=0), "one")
    retry: Final = owner.model_copy(update={"operation": "next"})

    assert isinstance(acquire(uncertain, cfg, retry, now=59), Denied)
    assert isinstance(acquire(uncertain, cfg, retry, now=60), Granted)
    assert isinstance(acquire(sweep(uncertain, cfg, now=300), cfg, retry, now=300), Granted)


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


def test_device_changes_cannot_bypass_key_rpm_limit() -> None:
    cfg: Final = config().model_copy(update={"client_limits": Limits(parallel=2, rpm=1, tpm=10000)})
    bound: Final = acquire(empty_state(cfg), cfg, request("one"), now=0)
    ended: Final = complete(start(bound.state, "one", now=0), cfg, "one", now=1)
    limited: Final = acquire(ended, cfg, request("one", "other", "new"), now=2)
    assert limited.failure.code == "client_rate_limit_exceeded"
    assert limited.state.sessions == ended.sessions
    assert limited.state.charges == ended.charges


def test_dead_unsent_reservations_are_reclaimed_but_started_work_is_quarantined() -> None:
    cfg: Final = config()
    pending: Final = acquire(empty_state(cfg), cfg, request("one"), now=0)
    unsent: Final = sweep(pending.state, cfg, now=cfg.heartbeat_timeout)
    assert unsent.operations == ()
    assert unsent.sessions == ()
    started: Final = start(pending.state, "one", now=0)
    uncertain: Final = sweep(started, cfg, now=cfg.heartbeat_timeout)
    assert uncertain.operations[0].quarantined
    assert acquire(uncertain, cfg, request("two"), now=299).resource == "b"


def test_sweep_removes_expired_events_and_idle_bindings() -> None:
    from litellm.router_utils.exclusive_pool.models import Event
    from litellm.router_utils.exclusive_pool.state import record_event

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
    assert "Начните новый диалог" in outcome.failure.message
    assert outcome.state == initial


def test_pinned_account_only_blocks_replay_of_server_references() -> None:
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
    moved: Final = acquire(broken, cfg, request("one", operation="next"), now=2)
    assert isinstance(moved, Granted) and moved.resource == "b"
    denied: Final = acquire(
        moved.state,
        cfg,
        request("one", operation="reference").model_copy(update={"requires_binding": True, "model": "model"}),
        now=2,
    )
    assert denied.kind == "denied"
    assert denied.failure.code == "pool_reference_upstream_unavailable"
    assert denied.failure.status == 409
    assert "Начните новый диалог" in denied.failure.message
    assert denied.state == moved.state
    expired: Final = acquire(
        broken, cfg, request("one", operation="late").model_copy(update={"requires_binding": True}), now=1000
    )
    assert expired.kind == "denied"
    assert expired.failure.code == "pool_reference_binding_required"


def test_a_failed_pinned_dialogue_does_not_block_another_dialogue_on_the_same_key() -> None:
    from litellm.router_utils.exclusive_pool.recovery import ErrorPolicy, fail

    cfg: Final = config()
    first: Final = acquire(
        empty_state(cfg), cfg, request("one").model_copy(update={"dialogue": "original", "pin_upstream": True}), now=0
    )
    broken: Final = fail(
        start(first.state, "one", now=0),
        cfg,
        "one",
        ErrorPolicy(kind="cooldown", confirmed_end=True, base_seconds=10),
        now=1,
        jitter=0,
    )
    other: Final = request("one", operation="new").model_copy(update={"dialogue": "other"})
    moved: Final = acquire(broken, cfg, other, now=2)
    assert isinstance(moved, Granted) and moved.resource == "b"
    finished: Final = complete(start(moved.state, "new", now=2), cfg, "new", now=3)
    reference: Final = request("one", operation="ref").model_copy(
        update={"dialogue": "original", "requires_binding": True}
    )
    denied: Final = acquire(finished, cfg, reference, now=4)
    assert isinstance(denied, Denied) and denied.failure.code == "pool_reference_upstream_unavailable"
    independent: Final = acquire(
        finished, cfg, reference.model_copy(update={"client": "two", "dialogue": "original"}), now=4
    )
    assert isinstance(independent, Denied) and independent.failure.code == "pool_reference_binding_required"
    recovered: Final = finished.model_copy(update={"health": empty_state(cfg).health})
    returning: Final = acquire(recovered, cfg, reference, now=12)
    assert isinstance(returning, Granted) and returning.resource == "a"


def test_reference_to_a_busy_upstream_can_be_retried_after_the_current_request() -> None:
    cfg: Final = config()
    first: Final = acquire(
        empty_state(cfg), cfg, request("one").model_copy(update={"dialogue": "original", "pin_upstream": True}), now=0
    )
    pinned: Final = complete(start(first.state, "one", now=0), cfg, "one", now=1)
    next_dialogue: Final = request("one", operation="other").model_copy(
        update={"eligible": ("b",), "dialogue": "other"}
    )
    alternate: Final = acquire(
        pinned.model_copy(update={"sessions": (pinned.sessions[0].model_copy(update={"resource": None}),)}),
        cfg,
        next_dialogue,
        now=2,
    )
    assert isinstance(alternate, Granted) and alternate.resource == "b"
    prior_dialogue: Final = request("one", operation="ref").model_copy(
        update={"dialogue": "original", "requires_binding": True, "model": "model"}
    )
    refused: Final = acquire(alternate.state, cfg, prior_dialogue, now=3)
    assert isinstance(refused, Denied) and refused.failure.code == "pool_reference_upstream_busy"
    assert refused.failure.status == 503
    assert "Повторите запрос позже" in refused.failure.message
    assert refused.state == alternate.state
    settled: Final = complete(start(alternate.state, "other", now=2), cfg, "other", now=4)
    accepted: Final = acquire(settled, cfg, prior_dialogue, now=5)
    assert isinstance(accepted, Granted) and accepted.resource == "a"


def test_references_are_rejected_if_a_dialogue_created_server_state_on_two_accounts() -> None:
    from litellm.router_utils.exclusive_pool.recovery import ErrorPolicy, fail

    cfg: Final = config()
    creator: Final = request("one").model_copy(update={"dialogue": "original", "pin_upstream": True})
    first: Final = acquire(empty_state(cfg), cfg, creator, now=0)
    broken: Final = fail(
        start(first.state, "one", now=0),
        cfg,
        "one",
        ErrorPolicy(kind="cooldown", confirmed_end=True, base_seconds=10),
        now=1,
        jitter=0,
    )
    second: Final = acquire(broken, cfg, creator.model_copy(update={"operation": "second"}), now=2)
    assert isinstance(second, Granted) and second.resource == "b"
    finished: Final = complete(start(second.state, "second", now=2), cfg, "second", now=3)
    followup: Final = creator.model_copy(update={"operation": "ref", "requires_binding": True})
    denied: Final = acquire(finished, cfg, followup, now=4)
    assert isinstance(denied, Denied) and denied.failure.code == "pool_reference_binding_required"


def test_other_dialogue_activity_does_not_extend_a_server_binding() -> None:
    cfg: Final = config()
    initial: Final = Admission(
        client="one",
        device="device",
        operation="create",
        tokens=10,
        eligible=("a", "b"),
        dialogue="original",
        pin_upstream=True,
    )
    created: Final = acquire(empty_state(cfg), cfg, initial, now=0)
    pinned: Final = complete(start(created.state, "create", now=0), cfg, "create", now=1)
    other: Final = Admission(
        client="one", device="device", operation="new", tokens=10, eligible=("a", "b"), dialogue="other"
    )
    accepted: Final = acquire(pinned, cfg, other, now=899)
    renewed: Final = complete(start(accepted.state, "new", now=899), cfg, "new", now=900)
    reference: Final = initial.model_copy(update={"operation": "ref", "requires_binding": True})
    still_live: Final = acquire(renewed, cfg, reference, now=900)
    assert isinstance(still_live, Granted) and still_live.resource == "a"
    expired: Final = acquire(renewed, cfg, reference, now=901)
    assert isinstance(expired, Denied) and expired.failure.code == "pool_reference_binding_required"


def test_legacy_pin_does_not_block_portable_requests() -> None:
    from litellm.router_utils.exclusive_pool.models import Session

    cfg: Final = config()
    legacy: Final = Session.model_validate(
        {
            "client": "one",
            "device": "device",
            "generation": "legacy",
            "resource": "a",
            "last_activity": 0,
            "established": True,
            "pinned_upstream": "physical-a",
        }
    )
    initial: Final = empty_state(cfg)
    cooling: Final = initial.health[0].model_copy(update={"status": "cooldown"})
    migrated: Final = initial.model_copy(update={"sessions": (legacy,), "health": (cooling, *initial.health[1:])})
    anonymous: Final = acquire(migrated, cfg, request("one"), now=1)
    assert isinstance(anonymous, Granted) and anonymous.resource == "b"
    identified: Final = acquire(migrated, cfg, request("one").model_copy(update={"dialogue": "new"}), now=1)
    assert isinstance(identified, Granted) and identified.resource == "b"


def test_provider_count_reservation_is_atomic_and_checks_both_quota_scopes() -> None:
    from litellm.router_utils.exclusive_pool.state import reserve_counted_tokens

    cfg: Final = config()
    initial: Final = acquire(empty_state(cfg), cfg, request("one").model_copy(update={"needs_count": True}), now=0)
    counted: Final = reserve_counted_tokens(initial.state, cfg, "one", 9000, None, now=1)
    assert counted.kind == "granted"
    assert all(charge.tokens == 9000 for charge in counted.state.charges)
    assert counted.state.operations[0].counted
    parallel: Final = acquire(
        counted.state,
        cfg,
        request("one", "other", "parallel").model_copy(update={"needs_count": True}),
        now=2,
    )
    fits: Final = reserve_counted_tokens(parallel.state, cfg, "parallel", cfg.client_limits.tpm - 9000, None, now=3)
    assert fits.kind == "granted"
    combined: Final = reserve_counted_tokens(
        parallel.state, cfg, "parallel", cfg.client_limits.tpm - 9000 + 1, None, now=3
    )
    assert combined.kind == "denied"
    assert combined.failure.code == "client_rate_limit_exceeded"
    assert combined.state == parallel.state
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


def test_pool_metrics_track_capacity_and_routing_outcomes() -> None:
    from litellm.router_utils.exclusive_pool.metrics import render_metrics

    cfg: Final = config()
    first: Final = acquire(empty_state(cfg), cfg, request("one"), now=0)
    first_done: Final = complete(start(first.state, "one", now=0), cfg, "one", now=1)
    second: Final = acquire(first_done, cfg, request("two"), now=1)
    second_done: Final = complete(start(second.state, "two", now=1), cfg, "two", now=2)
    fallback: Final = acquire(second_done, cfg, request("three").model_copy(update={"model": "model"}), now=2)
    before_start: Final = render_metrics(fallback.state, cfg, 2, 10)
    assert 'exclusive_pool_fallback_transitions_total{model="model"} 0' in before_start

    started: Final = start(fallback.state, "three", now=2)
    repeated: Final = start(started, "three", now=3)
    refused: Final = acquire(
        repeated, cfg, request("four").model_copy(update={"eligible": ("a", "b"), "model": "model"}), now=4
    )
    assert isinstance(refused, Denied)
    metrics: Final = render_metrics(refused.state, cfg, 4, 10)
    assert 'exclusive_pool_primary_available{model="model"} 0' in metrics
    assert 'exclusive_pool_fallback_available{model="model"} 1' in metrics
    assert 'exclusive_pool_fallback_transitions_total{model="model"} 1' in metrics
    assert 'exclusive_pool_endpoint_pool_exhausted_total{model="model"} 1' in metrics
    assert 'exclusive_pool_primary_available{model="model"} 2' in render_metrics(refused.state, cfg, 903, 10)
