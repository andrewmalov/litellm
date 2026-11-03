from typing import Final

from litellm.router_utils.exclusive_pool.admin import confirm_idle, reconfigure
from litellm.router_utils.exclusive_pool.models import Admission, Denied, Granted, Limits, PoolConfig, Resource
from litellm.router_utils.exclusive_pool.state import acquire, empty_state, quarantine, start


def test_confirmation_never_releases_a_live_operation_or_a_different_operation() -> None:
    config: Final = PoolConfig(
        pool_id="p",
        models=("m",),
        primaries=(Resource(id="a", upstream_id="a"),),
        fallback=Resource(id="f", upstream_id="f"),
        client_limits=Limits(),
    )
    pending: Final = acquire(
        empty_state(config),
        config,
        Admission(client="c", device="d", operation="one", tokens=1, eligible=("a", "f")),
        now=0,
    )
    assert isinstance(pending, Granted)
    running: Final = start(pending.state, "one", now=0)
    assert isinstance(confirm_idle(running, config, "one", now=1), Denied)
    assert confirm_idle(running, config, "old-operation", now=1000) == running
    unknown: Final = quarantine(running, "one")
    confirmed: Final = confirm_idle(unknown, config, "one", now=1)
    assert not isinstance(confirmed, Denied)
    assert confirmed.operations == ()


def test_configuration_change_requires_all_old_operations_to_end() -> None:
    config: Final = PoolConfig(
        pool_id="p",
        models=("m",),
        primaries=(Resource(id="a", upstream_id="a"),),
        fallback=Resource(id="f", upstream_id="f"),
        client_limits=Limits(),
    )
    original: Final = empty_state(config)
    updated: Final = config.model_copy(update={"models": ("new-model",)})
    accepted: Final = reconfigure(original, updated)
    assert not isinstance(accepted, Denied)
    assert accepted.fingerprint == updated.fingerprint
    pending: Final = acquire(
        original, config, Admission(client="c", device="d", operation="one", tokens=1, eligible=("a", "f")), now=0
    )
    assert isinstance(pending, Granted)
    assert isinstance(reconfigure(pending.state, updated), Denied)


def test_confirming_unknown_probe_end_requires_another_probe_before_ready() -> None:
    from litellm.router_utils.exclusive_pool.recovery import claim_probe

    config: Final = PoolConfig(
        pool_id="p",
        models=("m",),
        primaries=(Resource(id="a", upstream_id="a"),),
        fallback=Resource(id="f", upstream_id="f"),
        client_limits=Limits(),
    )
    original: Final = empty_state(config)
    cooled: Final = original.model_copy(
        update={"health": tuple(h.model_copy(update={"status": "cooldown"}) for h in original.health)}
    )
    probe: Final = claim_probe(cooled, config, "a", "probe", now=1)
    assert isinstance(probe, Granted)
    confirmed: Final = confirm_idle(quarantine(start(probe.state, "probe", now=1), "probe"), config, "probe", now=100)
    assert not isinstance(confirmed, Denied)
    assert confirmed.health[0].status == "cooldown"
    assert isinstance(claim_probe(confirmed, config, "a", "new-probe", now=101), Granted)
