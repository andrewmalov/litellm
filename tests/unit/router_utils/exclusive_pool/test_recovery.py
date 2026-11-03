from typing import Final

import httpx
import litellm

from litellm.router_utils.exclusive_pool.models import Admission, Denied, Granted, Limits, PoolConfig, Resource
from litellm.router_utils.exclusive_pool.recovery import classify, fail, claim_probe, probe_success, retry_after
from litellm.router_utils.exclusive_pool.state import acquire, empty_state, start


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


def test_uncertain_network_failure_blocks_reassignment_and_probes() -> None:
    cfg: Final = config()
    pending: Final = acquire(
        empty_state(cfg), cfg, Admission(client="c", device="d", operation="one", tokens=10, eligible=("a", "f")), now=0
    )
    assert isinstance(pending, Granted)
    uncertain: Final = fail(
        start(pending.state, "one", now=0),
        cfg,
        "one",
        classify(litellm.APIConnectionError(message="connection lost", model="model", llm_provider="openai")),
        now=5,
        jitter=0.1,
    )
    assert uncertain.operations[0].quarantined
    assert isinstance(claim_probe(uncertain, cfg, "a", "probe", now=10000), Denied)


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
    probe: Final = claim_probe(duplicate, cfg, "a", "probe", now=100)
    assert isinstance(probe, Granted)
    assert isinstance(claim_probe(probe.state, cfg, "a", "other-probe", now=200), Denied)
    recovered: Final = probe_success(probe.state, cfg, "probe", now=201)
    assert recovered.health[0].status == "ready"
    assert recovered.sessions[0].resource is None
    assert probe_success(recovered, cfg, "probe", now=202) == recovered


def test_fallback_capacity_increases_only_after_successful_request_completion() -> None:
    from litellm.router_utils.exclusive_pool.state import complete

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
    claimed: Final = claim_probe(cold, cfg, "f", "probe", now=1)
    assert isinstance(claimed, Granted)
    warm: Final = probe_success(claimed.state, cfg, "probe", now=2)
    first: Final = acquire(
        warm, cfg, Admission(client="c", device="d", operation="a", tokens=1, eligible=("f",)), now=3
    )
    assert isinstance(first, Granted)
    rejected: Final = acquire(
        first.state, cfg, Admission(client="other", device="d", operation="b", tokens=1, eligible=("f",)), now=3
    )
    assert isinstance(rejected, Denied)
    completed: Final = complete(start(first.state, "a", now=3), cfg, "a", now=4)
    assert completed.health[1].capacity == 2


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


def test_abandoned_unsent_probe_can_be_claimed_again() -> None:
    from litellm.router_utils.exclusive_pool.state import sweep

    cfg: Final = config()
    original: Final = empty_state(cfg)
    cooled: Final = original.model_copy(
        update={"health": tuple(h.model_copy(update={"status": "cooldown"}) for h in original.health)}
    )
    probe: Final = claim_probe(cooled, cfg, "a", "probe", now=1)
    assert isinstance(probe, Granted)
    reclaimed: Final = sweep(probe.state, cfg, now=100)
    assert isinstance(claim_probe(reclaimed, cfg, "a", "new-probe", now=101), Granted)


def test_client_error_on_probe_retries_after_cooldown() -> None:
    cfg: Final = config()
    original: Final = empty_state(cfg)
    cooled: Final = original.model_copy(
        update={"health": tuple(h.model_copy(update={"status": "cooldown"}) for h in original.health)}
    )
    probe: Final = claim_probe(cooled, cfg, "a", "probe", now=1)
    assert isinstance(probe, Granted)
    error: Final = litellm.BadRequestError(message="invalid probe", model="model", llm_provider="openai")
    rejected: Final = fail(start(probe.state, "probe", now=1), cfg, "probe", classify(error), now=2, jitter=0)
    assert isinstance(claim_probe(rejected, cfg, "a", "new-probe", now=100), Granted)


def test_permanent_provider_quota_is_disabled_using_structured_body() -> None:
    body: Final = {"error": {"code": "insufficient_quota", "message": "no credit"}}
    error: Final = litellm.RateLimitError(
        message="no credit", model="model", llm_provider="openai", body=body, response=httpx.Response(429, json=body)
    )
    assert classify(error).kind == "disabled"
    assert classify(error).shared_quota


def test_probe_reserves_its_input_even_with_tiny_client_output_cap() -> None:
    cfg: Final = config().model_copy(
        update={"output_tokens": 1, "primaries": (Resource(id="a", upstream_id="a", limits=Limits(tpm=2)),)}
    )
    initial: Final = empty_state(cfg)
    cooled: Final = initial.model_copy(
        update={"health": tuple(h.model_copy(update={"status": "cooldown"}) for h in initial.health)}
    )
    assert isinstance(claim_probe(cooled, cfg, "a", "probe", now=1), Denied)
