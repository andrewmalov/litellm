from __future__ import annotations

import asyncio
import logging
import os
import secrets
import uuid
import weakref
from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Final

import anyio
from opentelemetry.trace import Span
from pydantic import TypeAdapter, ValidationError
from redis.asyncio import Redis

import litellm
from litellm.caching.caching import DualCache
from litellm.integrations.custom_logger import CustomLogger
from litellm.litellm_core_utils.device_identity import DeviceMissing, device_identity
from litellm.proxy._types import UserAPIKeyAuth
from litellm.types.utils import AllMessageValues, CallTypes, CallTypesLiteral, LLMResponseTypes, ModelResponseStream

from . import state
from .models import Admission, Denied, Failure, Granted, Limits, Outcome, PoolConfig, Snapshot
from .recovery import ErrorPolicy, classify, fail, probe_success
from .request import (
    TICKET_KEY,
    DeploymentView,
    MetadataCarriers,
    RequestView,
    StreamProgress,
    Ticket,
    actual_usage,
    has_upstream_override,
    raise_public,
    recovery_request,
    safe_id,
)
from .store import RedisPoolStore
from .worker import RecoveryWorker

_LOGGER: Final = logging.getLogger(__name__)
_MESSAGES: Final = TypeAdapter(list[AllMessageValues])


class ExclusiveEndpointPool(CustomLogger):
    def __init__(self, store: RedisPoolStore | None = None) -> None:
        super().__init__()  # pyright: ignore[reportUnknownMemberType]  # CustomLogger exposes an untyped kwargs initializer
        self.store = store or RedisPoolStore(
            Redis.from_url(os.environ["EXCLUSIVE_POOL_REDIS_URL"], socket_timeout=3, socket_connect_timeout=3),
            PoolConfig.model_validate_json(Path(os.environ["EXCLUSIVE_POOL_CONFIG"]).read_bytes()),
        )
        self.secret = secrets.token_bytes(32)
        self._tasks: frozenset[asyncio.Task[None]] = frozenset()
        self._recovery: asyncio.Task[None] | None = None
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._ensure_recovery()

    async def async_pre_call_hook(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        cache: DualCache,
        data: Mapping[str, object],
        call_type: CallTypesLiteral,
    ) -> dict[str, object] | None:  # mutable-ok: CustomLogger's request replacement contract requires a dictionary
        if data.get("model") not in self.store.config.models:
            return None
        if has_upstream_override(data):
            raise_public(
                Failure(
                    code="unsupported_pool_request", status=400, message="Pool upstream settings cannot be overridden"
                )
            )
        self._ensure_recovery()
        try:
            request: Final = RequestView.model_validate(data)
        except ValidationError:
            raise_public(Failure(code="unsupported_pool_request", status=400, message="Invalid endpoint pool request"))
        if call_type not in ("completion", "acompletion") or not request.text_only or request.previous_response_id:
            raise_public(
                Failure(
                    code="unsupported_pool_request",
                    status=400,
                    message="This pool supports text chat completions with portable context",
                )
            )
        headers: Final[Mapping[str, str]] = request.proxy_server_request.headers if request.proxy_server_request else {}
        device: Final = device_identity(
            request.metadata.get("user_id"),
            explicit_header=headers.get(self.store.config.device_header.lower())
            if self.store.config.device_header
            else None,
        )
        if isinstance(device, DeviceMissing):
            raise_public(Failure(code="device_id_required", status=400, message="A stable device_id is required"))
        if not user_api_key_dict.api_key:
            raise_public(state.unavailable())
        output: Final = request.output_limit(self.store.config.output_tokens)
        defaults: Final = self.store.config.client_limits
        limits: Final = Limits(
            parallel=min(defaults.parallel, user_api_key_dict.max_parallel_requests or defaults.parallel),
            rpm=min(defaults.rpm, user_api_key_dict.rpm_limit or defaults.rpm),
            tpm=min(defaults.tpm, user_api_key_dict.tpm_limit or defaults.tpm),
        )
        ticket: Final = Ticket(
            operation=uuid.uuid4().hex,
            client=safe_id(self.store.config.pool_id, user_api_key_dict.api_key),
            device=safe_id(self.store.config.pool_id, device.device_id),
            resource="",
            tokens=request.reserved_tokens(output),
            limits=limits,
        )
        return {
            **data,
            "metadata": {**request.metadata, TICKET_KEY: ticket.sign(self.secret).model_dump()},
            "max_tokens": output if request.max_completion_tokens is None else None,
            "max_completion_tokens": output if request.max_completion_tokens is not None else None,
            "num_retries": 0,
            "max_retries": 0,
            "disable_fallbacks": True,
            "fallbacks": [],
            "context_window_fallbacks": [],
            "content_policy_fallbacks": [],
            "cache": {"no-cache": True},
        }

    async def async_filter_deployments(
        self,
        model: str,
        healthy_deployments: Sequence[Mapping[str, object]],
        messages: Sequence[AllMessageValues] | None,
        request_kwargs: Mapping[str, object] | None = None,
        parent_otel_span: Span | None = None,
    ) -> list[dict[str, object]]:  # mutable-ok: the legacy Router filter requires mutable deployment dictionaries
        if model not in self.store.config.models:
            return [dict(deployment) for deployment in healthy_deployments]
        ticket: Final = self._ticket(request_kwargs or {})
        if ticket is None:
            raise_public(state.unavailable())
        eligible: Final = tuple(
            DeploymentView.model_validate(deployment).model_info.id for deployment in healthy_deployments
        )
        outcome: Final = await self.store.acquire(
            Admission(
                client=ticket.client,
                device=ticket.device,
                operation=ticket.operation,
                tokens=ticket.tokens,
                limits=ticket.limits,
                eligible=eligible,
            )
        )
        match outcome:
            case Failure():
                raise_public(outcome)
            case Denied(failure=failure):
                raise_public(failure)
            case Granted(resource=resource):
                self._begin_heartbeat(ticket.operation)
                return [
                    dict(deployment)
                    for deployment in healthy_deployments
                    if DeploymentView.model_validate(deployment).model_info.id == resource
                ]

    async def async_pre_call_deployment_hook(
        self,
        kwargs: Mapping[str, object],
        call_type: CallTypes | None,
    ) -> None:
        carriers: Final = MetadataCarriers.model_validate(kwargs)
        ticket: Final = carriers.ticket(self.secret)
        configured: Final = frozenset(r.id for r in self.store.config.resources)
        if ticket is None:
            if carriers.deployment in configured:
                raise_public(state.unavailable())
            return
        updated: Final = await self.store.transact(
            lambda snapshot, now: self._start(snapshot, ticket, carriers.deployment, now)
        )
        if isinstance(updated, Failure):
            raise_public(updated)
        if isinstance(updated, Denied):
            raise_public(updated.failure)

    def _start(self, snapshot: Snapshot, ticket: Ticket, deployment: str | None, now: float) -> Outcome:
        operation: Final = next((op for op in snapshot.operations if op.id == ticket.operation), None)
        if operation is None or operation.quarantined or operation.started or operation.resource != deployment:
            return Denied(state=snapshot, failure=state.unavailable())
        if now - operation.heartbeat >= self.store.config.heartbeat_timeout:
            return Denied(state=snapshot, failure=state.unavailable())
        return Granted(
            state=state.start(snapshot, ticket.operation, now), resource=operation.resource, operation=operation.id
        )

    async def async_post_call_success_deployment_hook(
        self,
        request_data: Mapping[str, object],
        response: LLMResponseTypes,
        call_type: CallTypes | None,
    ) -> None:
        if request_data.get("stream") is True:
            return
        ticket: Final = self._ticket(request_data)
        if ticket is not None:
            await self._complete(ticket, actual_usage(response))

    async def async_post_call_failure_deployment_hook(
        self,
        request_data: Mapping[str, object],
        exception: Exception,
        call_type: CallTypes | None,
        fallback_depth: int | None = None,
    ) -> None:
        ticket: Final = self._ticket(request_data)
        if ticket is None:
            return
        policy: Final = classify(exception)
        await self.store.transact(
            lambda snapshot, now: fail(
                snapshot, self.store.config, ticket.operation, policy, now, (secrets.randbelow(999) + 1) / 10000
            )
        )

    async def async_post_call_failure_hook(
        self,
        request_data: Mapping[str, object],
        original_exception: Exception,
        user_api_key_dict: UserAPIKeyAuth,
        traceback_str: str | None = None,
    ) -> None:
        ticket: Final = self._ticket(request_data)
        if ticket is not None:
            await self.store.transact(
                lambda snapshot, now: state.abort(snapshot, self.store.config, ticket.operation, now)
            )
        if (
            request_data.get("model") in self.store.config.models
            and isinstance(original_exception, litellm.RateLimitError)
            and original_exception.category in ("litellm_rate_limit", "litellm_batch_rate_limit")
        ):
            raise_public(
                Failure(
                    code="client_rate_limit_exceeded", status=429, message="Client rate limit exceeded", retry_after=60
                )
            )

    async def async_post_call_streaming_iterator_hook(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        response: AsyncIterator[ModelResponseStream],
        request_data: Mapping[str, object],
    ) -> AsyncGenerator[ModelResponseStream, None]:
        ticket: Final = self._ticket(request_data)
        if ticket is None:
            async for chunk in response:
                yield chunk
            return
        self._begin_heartbeat(ticket.operation)
        progress: StreamProgress = StreamProgress()  # rebind-ok: fold stream accounting without buffering chunks
        try:
            async for chunk in response:
                progress = progress.feed(chunk)
                yield chunk
        except BaseException:
            with anyio.CancelScope(shield=True):
                await self.store.transact(lambda snapshot, now: state.quarantine(snapshot, ticket.operation))
            raise
        else:
            if not progress.terminal:
                await self.store.transact(
                    lambda snapshot, now: fail(
                        snapshot,
                        self.store.config,
                        ticket.operation,
                        ErrorPolicy(kind="cooldown", confirmed_end=False),
                        now,
                        0,
                    )
                )
                raise_public(
                    Failure(
                        code="upstream_stream_incomplete",
                        status=502,
                        message="Upstream stream ended without a terminal event",
                    )
                )
            await self._complete(ticket, progress.tokens)

    def _ticket(self, data: Mapping[str, object]) -> Ticket | None:
        return MetadataCarriers.model_validate(data).ticket(self.secret)

    async def _complete(self, ticket: Ticket, usage: int | None) -> None:
        await self.store.transact(
            lambda snapshot, now: (
                probe_success(snapshot, self.store.config, ticket.operation, now)
                if ticket.probe
                else state.complete(snapshot, self.store.config, ticket.operation, now, usage)
            )
        )

    def _begin_heartbeat(self, operation: str) -> None:
        owner: Final = asyncio.current_task()
        if owner is None:
            return
        task: Final = asyncio.create_task(self._heartbeat(operation, weakref.ref(owner)))
        self._tasks = self._tasks | frozenset((task,))
        task.add_done_callback(self._forget_task)

    def _forget_task(self, task: asyncio.Task[None]) -> None:
        self._tasks = self._tasks - frozenset((task,))
        if not task.cancelled():
            exception: Final = task.exception()
            if exception is not None:
                _LOGGER.error("Endpoint pool background task failed: %s", type(exception).__name__)

    async def _heartbeat(self, operation: str, owner: weakref.ReferenceType[asyncio.Task[object]]) -> None:
        while _owner_running(owner):
            await asyncio.sleep(self.store.config.heartbeat_seconds)
            if not await self._pulse(operation):
                return

    async def _pulse(self, operation: str) -> bool:
        snapshot: Final = await self.store.read()
        if isinstance(snapshot, Failure) or not any(
            op.id == operation and not op.quarantined for op in snapshot.operations
        ):
            return False
        updated: Final = await self.store.transact(lambda current, now: state.heartbeat(current, operation, now))
        return not isinstance(updated, Failure)

    def _ensure_recovery(self) -> None:
        if self._recovery is not None and not self._recovery.done():
            return
        task: Final = asyncio.create_task(RecoveryWorker(self.store, self._run_probe).run())
        self._recovery = task
        self._tasks = self._tasks | frozenset((task,))
        task.add_done_callback(self._forget_task)

    async def _run_probe(self, ticket: Ticket) -> bool:
        from litellm.proxy.proxy_server import llm_router

        if llm_router is None:
            return False
        self._begin_heartbeat(ticket.operation)
        probe: Final = recovery_request(ticket.resource)
        await llm_router.acompletion(  # pyright: ignore[reportUnknownMemberType]  # Router exposes untyped provider kwargs
            model=ticket.resource,
            messages=_MESSAGES.validate_python(probe.messages),
            max_tokens=1,
            metadata={TICKET_KEY: ticket.sign(self.secret).model_dump()},
            num_retries=0,
            max_retries=0,
            disable_fallbacks=True,
            cache={"no-cache": True},
        )
        return True

    async def aclose(self) -> None:
        tasks: Final = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.store.redis.close()


def _owner_running(owner: weakref.ReferenceType[asyncio.Task[object]]) -> bool:
    current: Final = owner()
    return current is not None and not current.done()
