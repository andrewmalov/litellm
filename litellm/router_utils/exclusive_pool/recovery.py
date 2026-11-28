from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Final, Literal

import httpx
import openai
from pydantic import ConfigDict, TypeAdapter, ValidationError

import litellm
from litellm.exceptions import RateLimitErrorCategory

from .models import (
    DarioHealth,
    Event,
    FrozenModel,
    Health,
    Operation,
    PoolConfig,
    Snapshot,
)
from .state import DARIO_HEALTH_MAX_AGE_SECONDS, complete, quarantine, record_event, reject

_RESPONSE: Final = TypeAdapter[httpx.Response](httpx.Response, config=ConfigDict(arbitrary_types_allowed=True))
_CODE: Final = TypeAdapter[str | None](str | None)


@dataclass(frozen=True, slots=True)
class ErrorPolicy:
    kind: Literal["client", "cooldown", "disabled"]
    confirmed_end: bool
    base_seconds: float = 15
    retry_after: str | None = None
    shared_quota: bool = False
    account_on_hold: bool = False
    unsent: bool = False


def retry_after(value: str | None, now: float) -> float:
    if value is None:
        return 0
    try:
        number: Final = float(value)
        return max(0, number) if math.isfinite(number) else 0
    except ValueError:
        pass
    try:
        parsed: Final = parsedate_to_datetime(value)
        return max(0, parsed.timestamp() - now) if parsed.tzinfo is not None else 0
    except (ValueError, TypeError, OverflowError):
        return 0


def _rate_limit_header(exception: litellm.RateLimitError, name: str) -> str | None:
    headers: Final[object] = getattr(exception, "litellm_response_headers", None)
    if isinstance(headers, httpx.Headers):
        return headers.get(name) or _RESPONSE.validate_python(exception.response).headers.get(name)
    if isinstance(headers, Mapping):
        value: Final[object] = headers.get(name)
        if isinstance(value, str):
            return value
    return _RESPONSE.validate_python(exception.response).headers.get(name)


def dario_parked(exception: litellm.RateLimitError) -> bool:
    return (
        exception.category == RateLimitErrorCategory.VENDOR_RATE_LIMIT
        and _rate_limit_header(exception, "x-dario-upstream-rejection") == "pool_parked"
    )


def classify(exception: Exception) -> ErrorPolicy:
    if isinstance(exception, openai.APIError) and _provider_code(exception) == "account_on_hold":
        return ErrorPolicy(kind="disabled", confirmed_end=True, account_on_hold=True)
    if isinstance(exception, (litellm.Timeout, litellm.APIConnectionError, openai.APIConnectionError)):
        return ErrorPolicy(kind="cooldown", confirmed_end=False)
    if isinstance(exception, (litellm.AuthenticationError, litellm.PermissionDeniedError, litellm.NotFoundError)):
        return ErrorPolicy(kind="disabled", confirmed_end=True)
    if isinstance(exception, litellm.BudgetExceededError):
        return ErrorPolicy(kind="client", confirmed_end=True)
    if isinstance(exception, litellm.RateLimitError):
        if exception.category in ("litellm_rate_limit", "litellm_batch_rate_limit"):
            return ErrorPolicy(kind="client", confirmed_end=True)
        if _provider_code(exception) in ("insufficient_quota", "billing_hard_limit_reached", "credit_balance_too_low"):
            return ErrorPolicy(kind="disabled", confirmed_end=True, shared_quota=True)
        return ErrorPolicy(
            kind="cooldown",
            confirmed_end=True,
            base_seconds=30,
            retry_after=_rate_limit_header(exception, "retry-after"),
            shared_quota=True,
            unsent=dario_parked(exception),
        )
    if isinstance(
        exception, (litellm.BadRequestError, litellm.ContentPolicyViolationError, litellm.ContextWindowExceededError)
    ):
        return ErrorPolicy(kind="client", confirmed_end=True)
    if isinstance(exception, openai.APIStatusError):
        return ErrorPolicy(
            kind="cooldown",
            confirmed_end=True,
            retry_after=_CODE.validate_python(_RESPONSE.validate_python(exception.response).headers.get("retry-after")),
        )
    return ErrorPolicy(kind="cooldown", confirmed_end=False)


class _ProviderError(FrozenModel):
    code: str | None = None
    error: _ProviderError | None = None


def _body_code(value: object) -> str | None:
    try:
        body: Final = _ProviderError.model_validate(value)
    except ValidationError:
        return None
    return body.code or (body.error.code if body.error else None)


def _provider_code(exception: openai.APIError) -> str | None:
    try:
        supplied: Final = _body_code(exception.body)
        if supplied is not None:
            return supplied
        if isinstance(exception, openai.APIStatusError):
            response: Final = _RESPONSE.validate_python(exception.response)
            try:
                parsed_body: Final = _ProviderError.model_validate_json(response.content)
                parsed: Final = parsed_body.code or (parsed_body.error.code if parsed_body.error else None)
                if parsed is not None:
                    return parsed
            except ValueError:
                pass
        return _CODE.validate_python(exception.code)
    except ValidationError:
        return None


def fail(
    state: Snapshot, config: PoolConfig, operation: str, policy: ErrorPolicy, now: float, jitter: float
) -> Snapshot:
    active: Final = next((op for op in state.operations if op.id == operation), None)
    if active is None:
        return state
    if policy.kind == "client":
        return reject(state, config, operation, now)
    ended: Final = (
        complete(state, config, operation, now, 0 if policy.unsent else None, successful=False)
        if policy.confirmed_end
        else quarantine(state, operation)
    )
    resource: Final = next(r for r in config.resources if r.id == active.resource)
    affected: Final = (
        frozenset(r.id for r in config.resources if r.scope == resource.scope)
        if policy.shared_quota
        else config.upstream_deployments(resource.upstream_id)
    )
    audited: Final = record_event(
        ended,
        config,
        Event(
            time=now,
            kind="upstream_backup_error" if config.is_fallback(resource.id) else f"upstream_{policy.kind}",
            client=active.client,
            resource=resource.id,
        ),
    )
    blocked_resources: Final = (
        affected if policy.account_on_hold else affected.difference(r.id for r in config.fallbacks)
    )
    return audited.model_copy(
        update={
            "health": tuple(
                _failed_health(h, active, policy, now, jitter) if h.resource in blocked_resources else h
                for h in ended.health
            ),
            "sessions": tuple(
                s.model_copy(update={"resource": None}) if s.resource in blocked_resources else s
                for s in ended.sessions
            ),
        }
    )


def _failed_health(health: Health, operation: Operation, policy: ErrorPolicy, now: float, jitter: float) -> Health:
    if health.account_on_hold or (
        not policy.account_on_hold and health.status != "ready" and health.generation != operation.health_generation
    ):
        return health
    backoff: Final = 1 << min(max(health.cycles, 0), 6)
    spread: Final = 1 + max(0, min(jitter, 0.1))
    interval: Final = (
        min(3600, 900 * backoff) * spread
        if policy.kind == "disabled"
        else min(300, policy.base_seconds * backoff * spread)
    )
    delay: Final = max(
        retry_after(policy.retry_after, now),
        interval,
    )
    return health.model_copy(
        update={
            "status": "cooldown" if policy.kind == "client" else policy.kind,
            "retry_at": now + delay,
            "cycles": health.cycles + 1,
            "failed_at": now,
            "account_on_hold": policy.account_on_hold,
            "generation": operation.id,
            "success_since": None,
            "capacity": None,
        }
    )


def record_dario_health(
    state: Snapshot, config: PoolConfig, report: DarioHealth, now: float, generation: str
) -> Snapshot:
    resources: Final = config.upstream_deployments(report.upstream)
    if not resources or not now - DARIO_HEALTH_MAX_AGE_SECONDS <= report.observed_at <= now:
        return state
    previous: Final = next((item for item in state.dario_health if item.upstream == report.upstream), None)
    if previous is not None and previous.observed_at >= report.observed_at:
        return state
    if (
        report.status == "ok"
        and previous is not None
        and (
            (previous.status == "rate_limited" and previous.retry_at > now)
            or (previous.status == "oauth_refreshed" and now - previous.observed_at <= DARIO_HEALTH_MAX_AGE_SECONDS)
        )
    ):
        return state
    updated: Final = state.model_copy(
        update={
            "dario_health": (
                *tuple(item for item in state.dario_health if item.upstream != report.upstream),
                report,
            )
        }
    )
    if report.status in ("unavailable", "rate_limited"):
        return updated.model_copy(
            update={
                "health": tuple(
                    h.model_copy(
                        update={
                            "status": "cooldown",
                            "generation": generation,
                            "retry_at": max(h.retry_at, report.retry_at)
                            if report.status == "rate_limited"
                            else h.retry_at,
                        }
                    )
                    if h.resource in resources
                    and h.status in ("ready", "cooldown")
                    and not config.is_fallback(h.resource)
                    else h
                    for h in state.health
                ),
            }
        )
    if report.status != "account_on_hold" or all(h.account_on_hold for h in state.health if h.resource in resources):
        return updated
    blocked: Final = updated.model_copy(
        update={
            "health": tuple(
                h.model_copy(
                    update={
                        "status": "disabled",
                        "account_on_hold": True,
                        "generation": generation,
                        "failed_at": report.observed_at,
                        "success_since": None,
                        "capacity": None,
                    }
                )
                if h.resource in resources
                else h
                for h in state.health
            ),
            "sessions": tuple(
                session.model_copy(update={"resource": None}) if session.resource in resources else session
                for session in state.sessions
            ),
        }
    )
    return record_event(blocked, config, Event(time=now, kind="upstream_disabled", resource=min(resources)))


def enable(state: Snapshot, resource: str, generation: str) -> Snapshot:
    upstream: Final = next((e.resource.upstream_id for e in state.catalog if e.resource.id == resource), None)
    affected: Final = (
        frozenset(e.resource.id for e in state.catalog if e.resource.upstream_id == upstream)
        if upstream is not None
        else frozenset((resource,))
    )
    return state.model_copy(
        update={
            "health": tuple(
                h.model_copy(
                    update={"status": "cooldown", "retry_at": 0, "generation": generation, "account_on_hold": False}
                )
                if h.resource in affected and h.status == "disabled"
                else h
                for h in state.health
            ),
        }
    )
