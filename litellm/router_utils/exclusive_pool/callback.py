from __future__ import annotations

import asyncio
import logging
import os
import secrets
import uuid
import weakref
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping, Sequence
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
from litellm.types.utils import AllMessageValues, CallTypes, CallTypesLiteral, LLMResponseTypes

from . import state
from .context import count_input_tokens
from .database import load_catalog
from .discovery import CatalogRow
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
    marked_model,
    raise_public,
    recovery_request,
    safe_id,
)
from .store import RedisPoolStore
from .worker import RecoveryWorker

_LOGGER: Final = logging.getLogger(__name__)
_MESSAGES: Final = TypeAdapter(list[AllMessageValues])


class ExclusiveEndpointPool(CustomLogger):
    def __init__(
        self,
        store: RedisPoolStore | None = None,
        catalog_source: Callable[[], Awaitable[tuple[CatalogRow, ...] | Failure]] = load_catalog,
        input_counter: Callable[[RequestView, Mapping[str, object]], Awaitable[int | Failure]] = count_input_tokens,
    ) -> None:
        super().__init__()  # pyright: ignore[reportUnknownMemberType]  # CustomLogger exposes an untyped kwargs initializer
        self.store = store or RedisPoolStore(
            Redis.from_url(os.environ["EXCLUSIVE_POOL_REDIS_URL"], socket_timeout=3, socket_connect_timeout=3),
            PoolConfig.model_validate_json(Path(os.environ["EXCLUSIVE_POOL_CONFIG"]).read_bytes()),
        )
        self.catalog_source = catalog_source
        self.input_counter = input_counter
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
        config: Final = await self._request_configuration(data.get("model"))
        if config is None or data.get("model") not in config.models:
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
        if not request.supports(call_type):
            raise_public(
                Failure(
                    code="unsupported_pool_request",
                    status=400,
                    message="This pool supports text chat completions and Anthropic Messages",
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
        anthropic: Final = call_type in ("anthropic_messages", "aanthropic_messages")
        ticket: Final = Ticket(
            operation=uuid.uuid4().hex,
            anthropic=anthropic,
            client=safe_id(self.store.config.pool_id, user_api_key_dict.api_key),
            device=safe_id(self.store.config.pool_id, device.device_id),
            resource="",
            tokens=output if anthropic and request.needs_count else request.reserved_tokens(output),
            needs_count=anthropic and request.needs_count,
            pin_upstream=anthropic and request.pin_upstream,
            requires_binding=anthropic and request.requires_binding,
            limits=limits,
        )
        metadata_key: Final = (
            "litellm_metadata" if call_type in ("anthropic_messages", "aanthropic_messages") else "metadata"
        )
        metadata: Final = request.litellm_metadata if metadata_key == "litellm_metadata" else request.metadata
        return {
            **data,
            metadata_key: {**metadata, TICKET_KEY: ticket.sign(self.secret).model_dump()},
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
        ticket: Final = self._ticket(request_kwargs or {})
        config: Final = await self._request_configuration(model)
        if config is None or model not in config.models:
            return [dict(deployment) for deployment in healthy_deployments]
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
                needs_count=ticket.needs_count,
                pin_upstream=ticket.pin_upstream,
                requires_binding=ticket.requires_binding,
            )
        )
        match outcome:
            case Failure():
                raise_public(outcome)
            case Denied(failure=failure):
                raise_public(failure)
            case Granted(resource=resource):
                self._begin_heartbeat(ticket.operation)
                selected: Final = next(
                    deployment
                    for deployment in healthy_deployments
                    if DeploymentView.model_validate(deployment).model_info.id == resource
                )
                active: Final = next(op for op in outcome.state.operations if op.id == ticket.operation)
                if not active.counted:
                    await self._count_before_generation(ticket, model, messages, request_kwargs or {}, selected)
                return [dict(selected)]

    async def _count_before_generation(
        self,
        ticket: Ticket,
        model: str,
        messages: Sequence[AllMessageValues] | None,
        kwargs: Mapping[str, object],
        deployment: Mapping[str, object],
    ) -> None:
        try:
            request: Final = RequestView.model_validate(
                {**kwargs, "model": model, "messages": messages or kwargs.get("messages", ())}
            )
            count: Final = await self.input_counter(request, deployment)
            if isinstance(count, Failure):
                raise_public(count)
            counted: Final = await self.store.transact(
                lambda snapshot, now: state.reserve_counted_tokens(
                    snapshot,
                    self.store.config.resolve(snapshot),
                    ticket.operation,
                    request.counted_reservation(count, ticket.tokens),
                    ticket.limits,
                    now,
                )
            )
            if isinstance(counted, Failure):
                raise_public(counted)
            if isinstance(counted, Denied):
                raise_public(counted.failure)
        except BaseException:
            with anyio.CancelScope(shield=True):
                await self.store.transact(
                    lambda snapshot, now: state.abort(
                        snapshot,
                        self.store.config.resolve(snapshot),
                        ticket.operation,
                        now,
                    )
                )
            raise

    async def async_pre_call_deployment_hook(
        self,
        kwargs: Mapping[str, object],
        call_type: CallTypes | None,
    ) -> None:
        carriers: Final = MetadataCarriers.model_validate(kwargs)
        ticket: Final = carriers.ticket(self.secret)
        snapshot: Final = await self.store.read() if self.store.config.discovery else None
        if ticket is None and carriers.pool is None and isinstance(snapshot, Failure):
            return
        config: Final = self.store.config.resolve(snapshot) if isinstance(snapshot, Snapshot) else self.store.config
        configured: Final = frozenset(r.id for r in config.resources)
        if ticket is None:
            if carriers.deployment in configured or carriers.pool is not None:
                raise_public(state.unavailable())
            return
        updated: Final = await self.store.transact(
            lambda snapshot, now: self._start(snapshot, ticket, carriers.deployment, now, call_type)
        )
        if isinstance(updated, Failure):
            raise_public(updated)
        if isinstance(updated, Denied):
            raise_public(updated.failure)

    def _start(
        self, snapshot: Snapshot, ticket: Ticket, deployment: str | None, now: float, call_type: CallTypes | None
    ) -> Outcome:
        operation: Final = next((op for op in snapshot.operations if op.id == ticket.operation), None)
        if operation is None or operation.quarantined or not operation.counted or operation.resource != deployment:
            return Denied(state=snapshot, failure=state.unavailable())
        if now - operation.heartbeat >= self.store.config.heartbeat_timeout:
            return Denied(state=snapshot, failure=state.unavailable())
        if operation.started:
            if ticket.is_adapter_call(call_type):
                return Granted(state=snapshot, resource=operation.resource, operation=operation.id)
            return Denied(state=snapshot, failure=state.unavailable())
        started: Final = state.start(snapshot, ticket.operation, now)
        if not any(op.id == operation.id and op.started for op in started.operations):
            return Denied(state=snapshot, failure=state.unavailable())
        return Granted(state=started, resource=operation.resource, operation=operation.id)

    async def async_post_call_success_deployment_hook(
        self,
        request_data: Mapping[str, object],
        response: LLMResponseTypes,
        call_type: CallTypes | None,
    ) -> None:
        if request_data.get("stream") is True:
            return
        ticket: Final = self._ticket(request_data)
        if ticket is not None and not ticket.is_adapter_call(call_type):
            await self._complete(ticket, actual_usage(response))

    async def async_post_call_failure_deployment_hook(
        self,
        request_data: Mapping[str, object],
        exception: Exception,
        call_type: CallTypes | None,
        fallback_depth: int | None = None,
    ) -> None:
        ticket: Final = self._ticket(request_data)
        if ticket is None or ticket.is_adapter_call(call_type):
            return
        policy: Final = classify(exception)
        await self.store.transact(
            lambda snapshot, now: fail(
                snapshot,
                self.store.config.resolve(snapshot),
                ticket.operation,
                policy,
                now,
                (secrets.randbelow(999) + 1) / 10000,
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
                lambda snapshot, now: state.abort(snapshot, self.store.config.resolve(snapshot), ticket.operation, now)
            )
        if (
            (ticket is not None or self._protected_model(request_data.get("model")))
            and isinstance(original_exception, litellm.RateLimitError)
            and original_exception.category in ("litellm_rate_limit", "litellm_batch_rate_limit")
        ):
            raise_public(
                Failure(
                    code="client_rate_limit_exceeded", status=429, message="Client rate limit exceeded", retry_after=60
                )
            )

    async def async_post_call_streaming_iterator_hook(  # pyright: ignore[reportIncompatibleMethodOverride]  # raw SSE
        self,
        user_api_key_dict: UserAPIKeyAuth,
        response: AsyncIterator[object],
        request_data: Mapping[str, object],
    ) -> AsyncGenerator[object, None]:
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
                        self.store.config.resolve(snapshot),
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
                probe_success(snapshot, self.store.config.resolve(snapshot), ticket.operation, now)
                if ticket.probe
                else state.complete(snapshot, self.store.config.resolve(snapshot), ticket.operation, now, usage)
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
        task: Final = asyncio.create_task(RecoveryWorker(self.store, self._run_probe, self._refresh_catalog).run())
        self._recovery = task
        self._tasks = self._tasks | frozenset((task,))
        task.add_done_callback(self._forget_task)

    def _protected_model(self, model: object) -> bool:
        if model in self.store.config.models:
            return True
        if not self.store.config.discovery:
            return False
        from litellm.proxy.proxy_server import llm_router

        if llm_router is None:
            return False
        return marked_model(model, getattr(llm_router, "model_list", ()))

    async def _request_configuration(self, model: object) -> PoolConfig | None:
        if not self.store.config.discovery:
            return self.store.config if model in self.store.config.models else None
        snapshot: Final = await self.store.read()
        known: Final = self.store.config.resolve(snapshot) if isinstance(snapshot, Snapshot) else self.store.config
        if (
            not self._protected_model(model)
            and model not in known.models
            and model not in tuple(r.id for r in known.resources)
        ):
            return None
        return await self._configuration()

    async def _refresh_catalog(self) -> None:
        if not self.store.config.discovery:
            return
        result: Final = await self.store.synchronize(self.catalog_source)
        if isinstance(result, Failure):
            _LOGGER.error("Endpoint pool catalog synchronization unavailable")

    async def _configuration(self) -> PoolConfig:
        if not self.store.config.discovery:
            return self.store.config
        snapshot: Final = await self.store.synchronize(self.catalog_source)
        if isinstance(snapshot, Failure):
            raise_public(snapshot)
        return self.store.config.resolve(snapshot)

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
