from collections.abc import Mapping
from typing import Final

import pytest

from litellm.router_utils.exclusive_pool.discovery import CatalogRow, discover, reconcile
from litellm.router_utils.exclusive_pool.models import (
    Admission,
    Failure,
    Granted,
    Limits,
    Outcome,
    PoolConfig,
    Snapshot,
)
from litellm.router_utils.exclusive_pool.recovery import ErrorPolicy, claim_probe, enable, fail, probe_success
from litellm.router_utils.exclusive_pool.state import acquire, complete, empty_state, quarantine, start


def _upstream_rows(
    upstream: str, role: str = "primary", metadata: Mapping[str, object] | None = None
) -> tuple[CatalogRow, ...]:
    return tuple(
        CatalogRow(
            model_id=f"{upstream}-{model}",
            model_name=model,
            model_info={
                "exclusive_pool": "main",
                "exclusive_pool_role": role,
                "exclusive_upstream_id": upstream,
                **(metadata or {}),
            },
        )
        for model in ("opus", "sonnet", "haiku")
    )


def _admit(snapshot: Snapshot, client: str, model: str, operation: str, now: float = 2) -> Outcome:
    eligible: Final = tuple(e.resource.id for e in snapshot.catalog if e.active and e.model == model)
    return acquire(
        snapshot,
        _config().resolve(snapshot),
        Admission(client=client, device="device", operation=operation, tokens=10, eligible=eligible),
        now,
    )


def test_model_switches_share_one_exclusive_physical_upstream() -> None:
    config: Final = _config()
    snapshot: Final = _reconcile(empty_state(config), config, (*_upstream_rows("a"), *_upstream_rows("b")), 1)
    first: Final = _admit(snapshot, "one", "opus", "first")
    assert isinstance(first, Granted) and first.resource == "a-opus"
    switched: Final = _admit(first.state, "one", "sonnet", "second")
    assert isinstance(switched, Granted) and switched.resource == "a-sonnet"
    other: Final = _admit(switched.state, "two", "haiku", "third")
    assert isinstance(other, Granted) and other.resource == "b-haiku"
    third: Final = _admit(other.state, "three", "opus", "fourth")
    assert not isinstance(third, Granted)
    finished: Final = complete(switched.state, config.resolve(switched.state), "first", 3)
    idle: Final = complete(finished, config.resolve(finished), "second", 4)
    retained: Final = _admit(idle, "one", "haiku", "fifth", 5)
    assert isinstance(retained, Granted) and retained.resource == "a-haiku"
    intruder: Final = _admit(idle, "two", "sonnet", "sixth", 5)
    assert isinstance(intruder, Granted) and intruder.resource == "b-sonnet"


def test_discovery_state_from_the_single_model_release_accepts_model_aliases() -> None:
    config: Final = _config()
    previous: Final = Snapshot(fingerprint="572cefb5c8141bc110d5048a6a0119e39e0650f66bfc8ffdea5f64ade194e31a")
    expanded: Final = _reconcile(previous, config, _upstream_rows("a"), 1)
    result: Final = _admit(expanded, "one", "sonnet", "first")
    assert isinstance(result, Granted) and result.resource == "a-sonnet"


@pytest.mark.parametrize("limits", [Limits(parallel=1), Limits(rpm=1), Limits(tpm=15)])
def test_model_aliases_share_capacity_and_quota(limits: Limits) -> None:
    config: Final = _config()
    rows: Final = _upstream_rows("a", metadata={"exclusive_pool_limits": limits.model_dump()})
    snapshot: Final = _reconcile(empty_state(config), config, rows, 1)
    first: Final = _admit(snapshot, "one", "opus", "first")
    assert isinstance(first, Granted)
    second: Final = _admit(first.state, "one", "sonnet", "second")
    assert not isinstance(second, Granted)
    assert second.failure.code == "endpoint_pool_exhausted"


def test_quarantine_and_removal_cannot_release_a_physical_upstream_through_an_alias() -> None:
    config: Final = _config()
    rows: Final = (*_upstream_rows("a"), *_upstream_rows("b"))
    snapshot: Final = _reconcile(empty_state(config), config, rows, 1)
    first: Final = _admit(snapshot, "one", "opus", "first")
    assert isinstance(first, Granted)
    uncertain: Final = quarantine(start(first.state, "first", 2), "first")
    removed: Final = _reconcile(uncertain, config, tuple(row for row in rows if row.model_id != "a-opus"), 3)
    switch: Final = _admit(removed, "one", "sonnet", "switch", 4)
    assert not isinstance(switch, Granted)
    other: Final = _admit(removed, "two", "sonnet", "other", 4)
    assert isinstance(other, Granted) and other.resource == "b-sonnet"
    assert removed.operations == uncertain.operations
    assert removed.charges == uncertain.charges


def test_removing_the_current_model_retains_the_lease_on_sibling_models() -> None:
    config: Final = _config()
    rows: Final = (*_upstream_rows("a"), *_upstream_rows("b"))
    snapshot: Final = _reconcile(empty_state(config), config, rows, 1)
    first: Final = _admit(snapshot, "one", "opus", "first")
    assert isinstance(first, Granted)
    removed: Final = _reconcile(first.state, config, tuple(row for row in rows if row.model_id != "a-opus"), 3)
    switch: Final = _admit(removed, "one", "sonnet", "switch", 4)
    assert isinstance(switch, Granted) and switch.resource == "a-sonnet"
    other: Final = _admit(switch.state, "two", "haiku", "other", 4)
    assert isinstance(other, Granted) and other.resource == "b-haiku"


def test_missing_model_does_not_move_a_healthy_client_to_another_upstream() -> None:
    config: Final = _config()
    rows: Final = (_upstream_rows("a")[0], *_upstream_rows("b"))
    snapshot: Final = _reconcile(empty_state(config), config, rows, 1)
    first: Final = _admit(snapshot, "one", "opus", "first")
    assert isinstance(first, Granted) and first.resource == "a-opus"
    ended: Final = complete(first.state, config.resolve(first.state), "first", 3)
    switch: Final = _admit(ended, "one", "sonnet", "switch", 4)
    assert not isinstance(switch, Granted)
    assert switch.state.sessions == ended.sessions


def test_fallback_models_share_one_physical_reserve() -> None:
    config: Final = _config()
    rows: Final = _upstream_rows("f", "fallback", metadata={"exclusive_pool_limits": Limits(parallel=2).model_dump()})
    snapshot: Final = _reconcile(empty_state(config), config, rows, 1)
    first: Final = _admit(snapshot, "one", "opus", "first")
    assert isinstance(first, Granted) and first.resource == "f-opus"
    second: Final = _admit(first.state, "two", "sonnet", "second")
    assert isinstance(second, Granted) and second.resource == "f-sonnet"
    third: Final = _admit(second.state, "three", "haiku", "third")
    assert not isinstance(third, Granted)


@pytest.mark.parametrize(
    "metadata",
    [
        {"exclusive_quota_scope": "different"},
        {"exclusive_pool_role": "fallback"},
        {"exclusive_pool_limits": {"parallel": 1}},
    ],
)
def test_aliases_cannot_disagree_on_physical_policy(metadata: Mapping[str, object]) -> None:
    config: Final = _config()
    original: Final = _upstream_rows("a")
    changed: Final = original[1].model_copy(update={"model_info": {**original[1].model_info, **metadata}})
    catalog: Final = discover((original[0], changed), config)
    if isinstance(catalog, Failure):
        assert catalog.code == "routing_state_unavailable"
        return
    assert isinstance(reconcile(empty_state(config), config, catalog, 1), Failure)


def test_round_robin_advances_to_another_physical_upstream_after_expiration() -> None:
    config: Final = _config()
    rows: Final = (*_upstream_rows("a"), *_upstream_rows("b"))
    initial: Final = _reconcile(empty_state(config), config, rows, 1)
    first: Final = _admit(initial, "one", "opus", "first")
    assert isinstance(first, Granted) and first.resource == "a-opus"
    ended: Final = complete(first.state, config.resolve(first.state), "first", 3)
    refreshed: Final = _reconcile(ended, config, rows, 904)
    second: Final = _admit(refreshed, "two", "sonnet", "second", 905)
    assert isinstance(second, Granted) and second.resource == "b-sonnet"


def test_failed_model_does_not_release_a_running_sibling_or_admit_probes() -> None:
    config: Final = _config()
    rows: Final = (*_upstream_rows("a"), *_upstream_rows("b"))
    initial: Final = _reconcile(empty_state(config), config, rows, 1)
    first: Final = _admit(initial, "one", "opus", "first")
    assert isinstance(first, Granted)
    second: Final = _admit(start(first.state, "first", 2), "one", "sonnet", "second")
    assert isinstance(second, Granted)
    failed: Final = fail(
        start(second.state, "second", 2),
        config.resolve(second.state),
        "first",
        ErrorPolicy(kind="cooldown", confirmed_end=True),
        3,
        0,
    )
    assert all(h.status == "cooldown" for h in failed.health if h.resource.startswith("a-"))
    assert not isinstance(_admit(failed, "one", "haiku", "switch", 4), Granted)
    assert not isinstance(claim_probe(failed, config.resolve(failed), "a-haiku", "probe", 20), Granted)
    other: Final = _admit(failed, "two", "haiku", "other", 4)
    assert isinstance(other, Granted) and other.resource == "b-haiku"
    ended: Final = complete(failed, config.resolve(failed), "second", 5)
    moved: Final = _admit(ended, "one", "haiku", "moved", 6)
    assert isinstance(moved, Granted) and moved.resource == "b-haiku"


def test_shared_quota_does_not_merge_physical_ownership() -> None:
    config: Final = _config()
    rows: Final = (
        *_upstream_rows("a", metadata={"exclusive_quota_scope": "account"}),
        *_upstream_rows("b", metadata={"exclusive_quota_scope": "account"}),
    )
    initial: Final = _reconcile(empty_state(config), config, rows, 1)
    first: Final = _admit(initial, "one", "opus", "first")
    assert isinstance(first, Granted) and first.resource == "a-opus"
    other: Final = _admit(first.state, "two", "sonnet", "other")
    assert isinstance(other, Granted) and other.resource == "b-sonnet"


@pytest.mark.parametrize("retire", [False, True])
def test_new_models_inherit_disabled_health_from_the_physical_upstream(retire: bool) -> None:
    config: Final = _config()
    rows: Final = _upstream_rows("a")
    initial: Final = _reconcile(empty_state(config), config, (rows[0],), 1)
    first: Final = _admit(initial, "one", "opus", "first")
    assert isinstance(first, Granted)
    disabled: Final = fail(
        first.state, config.resolve(first.state), "first", ErrorPolicy(kind="disabled", confirmed_end=True), 3, 0
    )
    expanded: Final = _reconcile(disabled, config, (rows[1],) if retire else rows, 4)
    assert all(h.status == "disabled" for h in expanded.health)
    denied: Final = _admit(expanded, "two", "sonnet", "other", 5)
    assert not isinstance(denied, Granted)


def test_recovery_generations_and_subsequent_disable_are_shared_by_all_models() -> None:
    config: Final = _config()
    initial: Final = _reconcile(empty_state(config), config, _upstream_rows("a"), 1)
    first: Final = _admit(initial, "one", "opus", "first")
    assert isinstance(first, Granted)
    cooled: Final = fail(
        first.state, config.resolve(first.state), "first", ErrorPolicy(kind="cooldown", confirmed_end=True), 3, 0
    )
    probe: Final = claim_probe(cooled, config.resolve(cooled), "a-opus", "probe", 19)
    assert isinstance(probe, Granted)
    assert all(h.status == "probing" and h.generation == "probe" for h in probe.state.health)
    recovered: Final = probe_success(probe.state, config.resolve(probe.state), "probe", 20)
    assert all(h.status == "ready" and h.generation == "probe" for h in recovered.health)
    second: Final = _admit(recovered, "two", "opus", "second", 21)
    assert isinstance(second, Granted)
    disabled: Final = fail(
        second.state, config.resolve(second.state), "second", ErrorPolicy(kind="disabled", confirmed_end=True), 22, 0
    )
    assert all(h.status == "disabled" for h in disabled.health)
    assert not isinstance(claim_probe(disabled, config.resolve(disabled), "a-sonnet", "bypass", 23), Granted)
    enabled: Final = enable(disabled, "a-sonnet", "operator")
    assert all(h.status == "cooldown" and h.generation == "operator" for h in enabled.health)


@pytest.mark.parametrize("retire", [False, True])
def test_fallback_capacity_ramp_is_shared_between_models(retire: bool) -> None:
    config: Final = _config()
    rows: Final = _upstream_rows("f", "fallback")
    initial: Final = _reconcile(empty_state(config), config, rows, 1)
    first: Final = _admit(initial, "one", "opus", "first")
    assert isinstance(first, Granted)
    cooled: Final = fail(
        first.state, config.resolve(first.state), "first", ErrorPolicy(kind="cooldown", confirmed_end=True), 3, 0
    )
    current: Final = _reconcile(cooled, config, rows[:2] if retire else rows, 4)
    probe: Final = claim_probe(current, config.resolve(current), "f-sonnet", "probe", 19)
    assert isinstance(probe, Granted)
    recovered: Final = probe_success(probe.state, config.resolve(probe.state), "probe", 20)
    assert all(h.status == "ready" and h.capacity == 1 for h in recovered.health)
    refreshed: Final = _reconcile(recovered, config, rows[:2] if retire else rows, 20)
    assert all(h.status == "ready" and h.capacity == 1 for h in refreshed.health)
    one: Final = _admit(refreshed, "one", "sonnet", "one", 21)
    assert isinstance(one, Granted)
    assert not isinstance(_admit(one.state, "two", "opus", "two", 21), Granted)
    ended: Final = complete(one.state, config.resolve(one.state), "one", 22)
    assert all(h.capacity == 2 for h in ended.health)


def _config() -> PoolConfig:
    return PoolConfig(pool_id="main", discovery=True, client_limits=Limits())


def _row(identifier: str, role: str = "primary", **metadata: object) -> CatalogRow:
    return CatalogRow(
        model_id=identifier,
        model_name="model-a",
        model_info={"exclusive_pool": "main", "exclusive_pool_role": role, **metadata},
    )


def _reconcile(snapshot: Snapshot, config: PoolConfig, rows: tuple[CatalogRow, ...], now: float) -> Snapshot:
    catalog: Final = discover(rows, config)
    assert not isinstance(catalog, Failure)
    updated: Final = reconcile(snapshot, config, catalog, now)
    assert not isinstance(updated, Failure)
    return updated


def test_membership_uses_metadata_and_deployment_ids_instead_of_model_names() -> None:
    config: Final = _config()
    rows: Final = (
        _row("a"),
        _row("f", "fallback"),
        _row("other", exclusive_pool="another-pool"),
        CatalogRow(model_id="unmarked", model_name="model-a", model_info={}),
    )
    discovered: Final = _reconcile(empty_state(config), config, rows, now=1)
    resolved: Final = config.resolve(discovered)
    assert tuple(r.id for r in resolved.primaries) == ("a",)
    assert resolved.fallback is not None and resolved.fallback.id == "f"
    assert resolved.models == ("model-a",)
    admitted: Final = acquire(
        discovered, resolved, Admission(client="c", device="d", operation="op", tokens=1, eligible=("unmarked",)), 2
    )
    assert not isinstance(admitted, Granted)


@pytest.mark.parametrize(
    "rows",
    [(_row("a", "invalid"),), (_row("a", "fallback"), _row("b", "fallback")), (_row("a"), _row("a"))],
)
def test_invalid_catalog_is_rejected_instead_of_silently_changing_membership(rows: tuple[CatalogRow, ...]) -> None:
    assert isinstance(discover(rows, _config()), Failure)


def test_addition_preserves_active_assignments_and_uses_new_capacity() -> None:
    config: Final = _config()
    original: Final = _reconcile(empty_state(config), config, (_row("a"),), 1)
    admitted: Final = acquire(
        original,
        config.resolve(original),
        Admission(client="c", device="d", operation="op", tokens=1, eligible=("a",)),
        2,
    )
    assert isinstance(admitted, Granted)
    expanded: Final = _reconcile(start(admitted.state, "op", 2), config, (_row("b"), _row("a")), 3)
    assert expanded.operations[0].started
    assert expanded.sessions[0].resource == "a"
    newcomer: Final = acquire(
        expanded,
        config.resolve(expanded),
        Admission(client="new", device="d", operation="new-op", tokens=1, eligible=("a", "b")),
        4,
    )
    assert isinstance(newcomer, Granted) and newcomer.resource == "b"


def test_removed_resource_drains_without_losing_uncertain_work_or_quota() -> None:
    config: Final = _config()
    original: Final = _reconcile(empty_state(config), config, (_row("a"), _row("b")), 1)
    admitted: Final = acquire(
        original,
        config.resolve(original),
        Admission(client="c", device="d", operation="op", tokens=7, eligible=("a",)),
        2,
    )
    assert isinstance(admitted, Granted)
    running: Final = quarantine(start(admitted.state, "op", 2), "op")
    removed: Final = _reconcile(running, config, (_row("b"),), 3)
    assert removed.operations == running.operations
    assert removed.charges == running.charges
    denied: Final = acquire(
        removed,
        config.resolve(removed),
        Admission(client="c", device="d", operation="next", tokens=1, eligible=("b",)),
        4,
    )
    assert not isinstance(denied, Granted)
    ended: Final = complete(removed, config.resolve(removed), "op", 5)
    moved: Final = acquire(
        ended, config.resolve(ended), Admission(client="c", device="d", operation="next", tokens=1, eligible=("b",)), 6
    )
    assert isinstance(moved, Granted) and moved.resource == "b"
    restored: Final = _reconcile(removed, config, (_row("a"), _row("b")), 7)
    blocked: Final = acquire(
        restored,
        config.resolve(restored),
        Admission(client="new", device="d", operation="new", tokens=1, eligible=("a",)),
        8,
    )
    assert not isinstance(blocked, Granted)


def test_resource_identity_and_role_cannot_be_rewritten_by_rediscovery() -> None:
    config: Final = _config()
    original: Final = _reconcile(empty_state(config), config, (_row("a"),), 1)
    changed: Final = discover((_row("a", "fallback"),), config)
    assert not isinstance(changed, Failure)
    assert isinstance(reconcile(original, config, changed, 2), Failure)


def test_stale_catalog_stops_new_admissions() -> None:
    config: Final = _config()
    original: Final = _reconcile(empty_state(config), config, (_row("a"),), 1)
    outcome: Final = acquire(
        original,
        config.resolve(original),
        Admission(client="c", device="d", operation="op", tokens=1, eligible=("a",)),
        100,
    )
    assert not isinstance(outcome, Granted)
    assert outcome.failure.code == "routing_state_unavailable"


def test_unmarked_database_rows_with_null_metadata_do_not_disable_discovery() -> None:
    ordinary: Final = CatalogRow.model_validate({"model_id": "ordinary", "model_name": "model-a", "model_info": None})
    assert discover((ordinary, _row("a")), _config()) == discover((_row("a"),), _config())


def test_removed_resource_cannot_be_probed() -> None:
    from litellm.router_utils.exclusive_pool.recovery import claim_probe

    config: Final = _config()
    original: Final = _reconcile(empty_state(config), config, (_row("a"),), 1)
    removed: Final = _reconcile(original, config, (), 2)
    cooled: Final = removed.model_copy(
        update={"health": tuple(h.model_copy(update={"status": "cooldown"}) for h in removed.health)}
    )
    assert not isinstance(claim_probe(cooled, config.resolve(cooled), "a", "probe", 3), Granted)


def test_blocked_deployments_are_drained_and_cannot_receive_new_clients() -> None:
    config: Final = _config()
    original: Final = _reconcile(empty_state(config), config, (_row("a"),), 1)
    blocked: Final = _reconcile(original, config, (_row("a").model_copy(update={"blocked": True}),), 2)
    result: Final = acquire(
        blocked,
        config.resolve(blocked),
        Admission(client="c", device="d", operation="op", tokens=1, eligible=("a",)),
        3,
    )
    assert not isinstance(result, Granted)
