from typing import Final

import httpx
import pytest

import litellm
from litellm.router_utils.exclusive_pool.models import (
    Admission,
    DarioHealth,
    Denied,
    Granted,
    Limits,
    PoolConfig,
    Resource,
)
from litellm.router_utils.exclusive_pool.recovery import (
    ErrorPolicy,
    classify,
    enable,
    fail,
    record_dario_health,
    retry_after,
)
from litellm.router_utils.exclusive_pool.state import acquire, complete, empty_state, start, sweep


def config() -> PoolConfig:
    return PoolConfig(
        pool_id="p",
        models=("model",),
        primaries=(Resource(id="a", upstream_id="a"),),
        fallback=Resource(id="f", upstream_id="f"),
        client_limits=Limits(),
    )


def test_retry_after_is_a_lower_bound_including_http_dates() -> None:
    assert retry_after("600", now=0) == 600
    assert retry_after("Thu, 01 Jan 1970 00:10:00 GMT", now=0) == 600
    assert retry_after("bad", now=1000) == 0
    assert retry_after("Thu, 01 Jan 1970 00:10:00 GMT", now=1000) == 0


def test_uncertain_network_failure_allows_failover_and_recovery_without_reassignment() -> None:
    cfg: Final = config()
    pending: Final = acquire(
        empty_state(cfg), cfg, Admission(client="c", device="d", operation="one", tokens=10, eligible=("a", "f")), now=0
    )
    assert isinstance(pending, Granted)
    policy: Final = classify(
        litellm.APIConnectionError(message="connection lost", model="model", llm_provider="openai")
    )
    uncertain: Final = fail(
        start(pending.state, "one", now=0),
        cfg,
        "one",
        policy,
        now=5,
        jitter=0.1,
    )
    assert uncertain.operations[0].quarantined
    owner: Final = Admission(client="c", device="d", operation="next", tokens=10, eligible=("a", "f"))
    failover: Final = acquire(uncertain, cfg, owner, now=6)
    assert isinstance(failover, Granted) and failover.resource == "f"
    assert isinstance(acquire(uncertain, cfg, owner.model_copy(update={"eligible": ("a",)}), now=6), Denied)
    next_request: Final = owner.model_copy(update={"eligible": ("a",)})
    recovered: Final = acquire(uncertain, cfg, next_request, now=30)
    assert isinstance(recovered, Granted)
    ready: Final = complete(start(recovered.state, "next", 30), cfg, "next", 31)
    assert ready.health[0].status == "ready"
    assert isinstance(acquire(ready, cfg, next_request.model_copy(update={"client": "other"}), now=32), Denied)


def test_confirmed_error_cools_down_once_and_only_one_coordinator_can_probe() -> None:
    cfg: Final = config()
    request: Final = Admission(client="c", device="d", operation="one", tokens=10, eligible=("a", "f"))
    one: Final = acquire(empty_state(cfg), cfg, request, now=0)
    assert isinstance(one, Granted)
    two: Final = acquire(one.state, cfg, request.model_copy(update={"operation": "two"}), now=0)
    assert isinstance(two, Granted)
    error: Final = litellm.ServiceUnavailableError(
        message="overloaded", model="model", llm_provider="openai", response=httpx.Response(503)
    )
    cooled: Final = fail(
        start(start(two.state, "one", now=0), "two", now=0), cfg, "one", classify(error), now=1, jitter=0.1
    )
    duplicate: Final = fail(cooled, cfg, "two", classify(error), now=2, jitter=0.1)
    assert duplicate.health == cooled.health
    assert duplicate.operations == ()
    recovery: Final = request.model_copy(update={"operation": "recovery", "eligible": ("a",)})
    first: Final = acquire(duplicate, cfg, recovery, now=100)
    assert isinstance(first, Granted)
    parallel: Final = recovery.model_copy(update={"operation": "parallel"})
    assert isinstance(acquire(first.state, cfg, parallel, now=101), Denied)
    recovered: Final = complete(start(first.state, "recovery", 100), cfg, "recovery", 102)
    assert recovered.health[0].status == "ready"


def test_existing_backup_cooldown_allows_admission_without_a_probe() -> None:
    cfg: Final = config()
    initialized: Final = empty_state(cfg)
    cold: Final = initialized.model_copy(
        update={
            "health": tuple(
                h.model_copy(update={"status": "cooldown", "cycles": 1}) if h.resource == "f" else h
                for h in initialized.health
            )
        }
    )
    first: Final = acquire(
        cold, cfg, Admission(client="c", device="d", operation="a", tokens=1, eligible=("f",)), now=3
    )
    assert isinstance(first, Granted)
    assert sweep(first.state, cfg, now=4).health[1].status == "ready"


@pytest.mark.parametrize(
    "policy",
    (
        ErrorPolicy(kind="cooldown", confirmed_end=True),
        ErrorPolicy(kind="disabled", confirmed_end=True),
    ),
)
def test_backup_keeps_accepting_requests_after_an_upstream_error(policy: ErrorPolicy) -> None:
    cfg: Final = config()
    request: Final = Admission(client="first", device="d", operation="first", tokens=1, eligible=("f",))
    first: Final = acquire(empty_state(cfg), cfg, request, now=0)
    assert isinstance(first, Granted)
    failed: Final = fail(start(first.state, "first", now=0), cfg, "first", policy, now=1, jitter=0)
    second: Final = acquire(failed, cfg, request.model_copy(update={"client": "other", "operation": "next"}), now=2)
    assert isinstance(second, Granted) and second.resource == "f"
    assert not any(h.status != "ready" for h in failed.health)


def test_provider_quota_blocks_all_deployments_in_the_same_scope() -> None:
    cfg: Final = PoolConfig(
        pool_id="shared",
        models=("model",),
        primaries=(
            Resource(id="a", upstream_id="a", quota_scope="account"),
            Resource(id="b", upstream_id="b", quota_scope="account"),
        ),
        fallback=Resource(id="f", upstream_id="f"),
        client_limits=Limits(),
    )
    pending: Final = acquire(
        empty_state(cfg),
        cfg,
        Admission(client="c", device="d", operation="one", tokens=1, eligible=("a", "b", "f")),
        now=0,
    )
    assert isinstance(pending, Granted)
    error: Final = litellm.RateLimitError(
        message="temporary quota",
        model="model",
        llm_provider="openai",
        response=httpx.Response(429, headers={"Retry-After": "600"}),
    )
    cooled: Final = fail(start(pending.state, "one", now=0), cfg, "one", classify(error), now=1, jitter=0)
    assert tuple(h.status for h in cooled.health) == ("cooldown", "cooldown", "ready")
    assert tuple(h.retry_at for h in cooled.health[:2]) == (601, 601)


def test_invalid_provider_parameters_do_not_leave_or_refresh_a_binding() -> None:
    from litellm.router_utils.exclusive_pool.state import complete

    cfg: Final = config()
    request: Final = Admission(client="c", device="d", operation="one", tokens=10, eligible=("a", "f"))
    pending: Final = acquire(empty_state(cfg), cfg, request, now=0)
    assert isinstance(pending, Granted)
    error: Final = litellm.BadRequestError(message="invalid parameter", model="model", llm_provider="openai")
    rejected: Final = fail(start(pending.state, "one", now=0), cfg, "one", classify(error), now=10, jitter=0)
    assert rejected.sessions == ()
    assert rejected.health == pending.state.health
    accepted: Final = complete(start(pending.state, "one", now=0), cfg, "one", now=1)
    another: Final = acquire(accepted, cfg, request.model_copy(update={"operation": "two"}), now=100)
    assert isinstance(another, Granted)
    invalid: Final = fail(start(another.state, "two", now=100), cfg, "two", classify(error), now=110, jitter=0)
    assert invalid.sessions == accepted.sessions


def test_proxy_rate_limits_are_not_upstream_cooldowns() -> None:
    from litellm.exceptions import RateLimitErrorCategory

    error: Final = litellm.RateLimitError(
        message="key limit", model="alias", llm_provider="", category=RateLimitErrorCategory.LITELLM_RATE_LIMIT
    )
    assert classify(error).kind == "client"


@pytest.mark.parametrize("client_error", (True, False))
def test_unsuccessful_recovery_releases_slot_without_marking_ready(client_error: bool) -> None:
    cfg: Final = config()
    initial: Final = empty_state(cfg)
    cooled: Final = initial.model_copy(
        update={"health": (initial.health[0].model_copy(update={"status": "cooldown"}), initial.health[1])}
    )
    request: Final = Admission(client="c", device="d", operation="one", tokens=1, eligible=("a",))
    admitted: Final = acquire(cooled, cfg, request, now=1)
    assert isinstance(admitted, Granted)
    rejected: Final = (
        fail(start(admitted.state, "one", 1), cfg, "one", ErrorPolicy(kind="client", confirmed_end=True), 2, 0)
        if client_error
        else sweep(admitted.state, cfg, now=100)
    )
    assert rejected.health[0].status == "cooldown"
    assert isinstance(acquire(rejected, cfg, request.model_copy(update={"operation": "two"}), now=101), Granted)


def test_permanent_provider_quota_is_disabled_using_structured_body() -> None:
    body: Final = {"error": {"code": "insufficient_quota", "message": "no credit"}}
    error: Final = litellm.RateLimitError(
        message="no credit", model="model", llm_provider="openai", body=body, response=httpx.Response(429, json=body)
    )
    assert classify(error).kind == "disabled"
    assert classify(error).shared_quota


def test_disabled_requires_fresh_oauth_refresh_and_hold_requires_operator() -> None:
    cfg: Final = config()
    initial: Final = empty_state(cfg)
    request: Final = Admission(client="c", device="d", operation="one", tokens=10, eligible=("a",))
    admitted: Final = acquire(initial, cfg, request, now=0)
    assert isinstance(admitted, Granted)
    disabled: Final = fail(
        start(admitted.state, "one", 0), cfg, "one", ErrorPolicy(kind="disabled", confirmed_end=True), 10, 0
    )
    next_request: Final = request.model_copy(update={"operation": "two"})
    healthy: Final = DarioHealth(upstream="a", observed_at=909, status="ok")
    observed: Final = record_dario_health(disabled, cfg, healthy, 909, "health")
    assert isinstance(acquire(observed, cfg, next_request, now=910), Denied)
    refreshed: Final = healthy.model_copy(update={"status": "oauth_refreshed", "observed_at": 910})
    refreshed_state: Final = record_dario_health(observed, cfg, refreshed, 910, "refresh")
    assert isinstance(acquire(refreshed_state, cfg, next_request, now=1091), Denied)
    attempt: Final = acquire(refreshed_state, cfg, next_request, now=911)
    assert isinstance(attempt, Granted)
    running: Final = start(attempt.state, "two", 911)
    hold: Final = healthy.model_copy(update={"status": "account_on_hold", "observed_at": 912})
    stopped: Final = record_dario_health(running, cfg, hold, 912, "hold")
    late: Final = complete(stopped, cfg, "two", 913)
    after_hold: Final = record_dario_health(
        late, cfg, refreshed.model_copy(update={"observed_at": 914}), 914, "late-refresh"
    )
    assert isinstance(acquire(after_hold, cfg, next_request, now=915), Denied)
    assert isinstance(acquire(enable(after_hold, "a", "manual"), cfg, next_request, now=916), Granted)


def test_farm_quota_reset_blocks_client_routing_until_reset() -> None:
    cfg: Final = config()
    quota: Final = DarioHealth(upstream="a", observed_at=100, status="rate_limited", retry_at=500)
    request: Final = Admission(client="c", device="d", operation="one", tokens=1, eligible=("a", "f"))
    assigned: Final = acquire(empty_state(cfg), cfg, request, now=99)
    assert isinstance(assigned, Granted)
    finished: Final = complete(start(assigned.state, "one", 99), cfg, "one", 99)
    limited: Final = record_dario_health(finished, cfg, quota, 100, "quota")
    next_request: Final = request.model_copy(update={"operation": "two"})
    fallback: Final = acquire(limited, cfg, next_request, now=499)
    assert isinstance(fallback, Granted) and fallback.resource == "f"
    primary: Final = acquire(limited, cfg, next_request, now=500)
    assert isinstance(primary, Granted) and primary.resource == "a"
    assert primary.state.health[0].status == "probing"


def test_due_cooldown_uses_one_client_request_before_backup() -> None:
    cfg: Final = config()
    initial: Final = empty_state(cfg)
    cooled: Final = initial.model_copy(
        update={
            "health": (initial.health[0].model_copy(update={"status": "cooldown", "retry_at": 30}), initial.health[1])
        }
    )
    request: Final = Admission(client="c", device="d", operation="one", tokens=10, eligible=("a", "f"))
    idle: Final = sweep(cooled, cfg, now=30)
    assert idle.health[0].status == "cooldown"
    assert not idle.operations
    first: Final = acquire(idle, cfg, request, now=30)
    assert isinstance(first, Granted) and first.resource == "a"
    second: Final = acquire(
        first.state, cfg, request.model_copy(update={"client": "other", "operation": "two"}), now=30
    )
    assert isinstance(second, Granted) and second.resource == "f"
    assert complete(start(first.state, "one", 30), cfg, "one", 31).health[0].status == "ready"
