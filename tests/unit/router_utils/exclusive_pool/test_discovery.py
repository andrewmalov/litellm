from typing import Final

import pytest

from litellm.router_utils.exclusive_pool.discovery import CatalogRow, discover, reconcile
from litellm.router_utils.exclusive_pool.models import Admission, Failure, Granted, Limits, PoolConfig, Snapshot
from litellm.router_utils.exclusive_pool.state import acquire, complete, empty_state, quarantine, start


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
