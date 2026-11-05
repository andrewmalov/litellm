from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import Annotated, Final, NoReturn

from pydantic import Field, JsonValue, TypeAdapter, ValidationError

from litellm.proxy._types import ProxyException
from litellm.types.utils import CallTypes, ModelResponseStream

from .models import Failure, FrozenModel, Limits

TICKET_KEY: Final = "exclusive_endpoint_pool"


class Ticket(FrozenModel):
    operation: str
    client: str
    device: str
    resource: str
    needs_count: bool = False
    pin_upstream: bool = False
    requires_binding: bool = False
    anthropic: bool = False
    probe: bool = False
    tokens: int = 1
    limits: Limits | None = None

    def is_adapter_call(self, call_type: CallTypes | None) -> bool:
        return self.anthropic and call_type in (CallTypes.acompletion, CallTypes.aresponses)

    def sign(self, secret: bytes) -> SignedTicket:
        return SignedTicket(
            ticket=self, signature=hmac.new(secret, self.model_dump_json().encode(), hashlib.sha256).hexdigest()
        )


class SignedTicket(FrozenModel):
    ticket: Ticket
    signature: str = Field(repr=False)


def verified_ticket(value: object, secret: bytes) -> Ticket | None:
    try:
        signed: Final = SignedTicket.model_validate(value)
    except ValidationError:
        return None
    return signed.ticket if hmac.compare_digest(signed.signature, signed.ticket.sign(secret).signature) else None


class ProxyRequest(FrozenModel):
    headers: Mapping[str, str] = Field(default_factory=dict)


class RequestView(FrozenModel):
    model: str
    system: JsonValue = None
    container: JsonValue = None
    messages: tuple[Mapping[str, JsonValue], ...] = ()
    tools: tuple[Mapping[str, JsonValue], ...] = ()
    functions: tuple[Mapping[str, JsonValue], ...] = ()
    response_format: Mapping[str, JsonValue] | None = None
    output_config: Mapping[str, JsonValue] | None = None
    output_format: Mapping[str, JsonValue] | None = None
    thinking: JsonValue = None
    tool_choice: JsonValue = None
    function_call: JsonValue = None
    metadata: Mapping[str, object] = Field(default_factory=dict)
    litellm_metadata: Mapping[str, object] = Field(default_factory=dict)
    proxy_server_request: ProxyRequest | None = None
    max_tokens: Annotated[int, Field(gt=0, strict=True)] | None = None
    max_completion_tokens: Annotated[int, Field(gt=0, strict=True)] | None = None
    n: Annotated[int, Field(ge=1, le=1, strict=True)] = 1
    stream: bool = False
    previous_response_id: str | None = None

    @property
    def text_only(self) -> bool:
        return bool(self.messages) and all(
            isinstance(m.get("content"), str) or m.get("content") is None for m in self.messages
        )

    def supports(self, call_type: str) -> bool:
        if self.previous_response_id or not self.messages:
            return False
        if call_type in ("anthropic_messages", "aanthropic_messages"):
            return (self.container is None or isinstance(self.container, (str, dict))) and all(
                _supported_content(message.get("content")) for message in self.messages
            )
        return call_type in ("completion", "acompletion") and self.text_only

    @property
    def requires_binding(self) -> bool:
        from .context import has_reference

        return (
            isinstance(self.container, str)
            or (isinstance(self.container, dict) and bool(self.container.get("id")))
            or any(has_reference(message.get("content")) for message in self.messages)
        )

    @property
    def pin_upstream(self) -> bool:
        from .context import client_tool

        return (
            self.requires_binding
            or self.container is not None
            or any(not client_tool(tool.get("type")) for tool in self.tools)
        )

    @property
    def needs_count(self) -> bool:
        from .context import has_media

        return self.pin_upstream or any(has_media(message.get("content")) for message in self.messages)

    def output_limit(self, server_limit: int) -> int:
        return min(self.max_completion_tokens or self.max_tokens or server_limit, server_limit)

    def reserved_tokens(self, output_limit: int) -> int:
        encoded: Final = json.dumps(
            {
                "messages": self.messages,
                "system": self.system,
                "tools": self.tools,
                "functions": self.functions,
                "response_format": self.response_format,
                "output_config": self.output_config,
                "output_format": self.output_format,
                "tool_choice": self.tool_choice,
                "function_call": self.function_call,
            },
            ensure_ascii=False,
        )
        return len(encoded.encode()) + output_limit + 32 * len(self.messages)

    def counted_reservation(self, input_tokens: int, output_limit: int) -> int:
        schemas: Final = tuple(
            schema for schema in (self.output_format, self.response_format, self.functions) if schema
        )
        return (
            input_tokens
            + output_limit
            + sum(len(json.dumps(schema, ensure_ascii=False).encode()) for schema in schemas)
        )

    def ticket(self, secret: bytes) -> Ticket | None:
        return verified_ticket(self.metadata.get(TICKET_KEY, self.litellm_metadata.get(TICKET_KEY)), secret)


def _supported_content(content: JsonValue) -> bool:
    if isinstance(content, str):
        return True
    if not isinstance(content, list) or not content:
        return False
    return all(_supported_block(block) for block in content)


def _supported_block(block: JsonValue) -> bool:
    if not isinstance(block, dict):
        return False
    match block.get("type"):
        case "text" | "tool_use" | "thinking" | "redacted_thinking":
            return True
        case "image" | "document":
            source: Final = block.get("source")
            return isinstance(source, dict) and source.get("type") in ("base64", "url", "text", "content", "file")
        case "container_upload" | "server_tool_use":
            return True
        case "tool_result":
            return "content" not in block or block.get("content") == [] or _supported_content(block.get("content"))
        case str() as kind if kind.endswith("_tool_result"):
            return True
        case _:
            return False


def recovery_request(model: str) -> RequestView:
    return RequestView(model=model, messages=({"role": "user", "content": "Reply with OK"},), max_tokens=1)


def safe_id(pool: str, value: str) -> str:
    return hashlib.sha256(f"{pool}\0{value}".encode()).hexdigest()


class PoolProxyException(ProxyException):
    def __init__(self, failure: Failure) -> None:
        self.failure = failure
        super().__init__(  # pyright: ignore[reportUnknownMemberType]  # ProxyException retains an untyped provider-fields parameter
            message=failure.message,
            type=failure.code,
            param=None,
            code=failure.status,
            headers={"Retry-After": str(failure.retry_after)} if failure.retry_after else {},
        )

    def to_dict(self) -> dict[str, JsonValue]:  # mutable-ok: ProxyException's wire serializer requires a dictionary
        return {"message": self.failure.message, "type": self.failure.code, "param": None, "code": self.failure.code}


def raise_public(failure: Failure) -> NoReturn:
    raise PoolProxyException(failure)


class MetadataCarriers(FrozenModel):
    model_info: DeploymentInfo | None = None
    metadata: Mapping[str, object] = Field(default_factory=dict)
    litellm_metadata: Mapping[str, object] = Field(default_factory=dict)

    def ticket(self, secret: bytes) -> Ticket | None:
        return verified_ticket(self.metadata.get(TICKET_KEY, self.litellm_metadata.get(TICKET_KEY)), secret)

    @property
    def pool(self) -> str | None:
        return self.model_info.exclusive_pool if self.model_info is not None else None

    @property
    def deployment(self) -> str | None:
        return self.model_info.id if self.model_info is not None else None


class DeploymentInfo(FrozenModel):
    id: str
    exclusive_pool: str | None = None


class DeploymentView(FrozenModel):
    model_name: str | None = None
    model_info: DeploymentInfo


class _RouterDeployment(FrozenModel):
    model_name: str
    model_info: Mapping[str, object]


_ROUTER_DEPLOYMENTS: Final = TypeAdapter(tuple[_RouterDeployment, ...])


def marked_model(model: object, deployments: object) -> bool:
    validated: Final = _ROUTER_DEPLOYMENTS.validate_python(deployments)
    return any(
        deployment.model_info.get("exclusive_pool") is not None
        and model in (deployment.model_name, deployment.model_info.get("id"))
        for deployment in validated
    )


class UsageView(FrozenModel):
    total_tokens: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @property
    def total(self) -> int | None:
        if self.total_tokens is not None:
            return self.total_tokens
        if self.input_tokens is None or self.output_tokens is None:
            return None
        return self.input_tokens + self.output_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens


class _UsageCarrier(FrozenModel):
    usage: object = None


def actual_usage(response: object) -> int | None:
    try:
        carrier: Final = _UsageCarrier.model_validate(response, from_attributes=True)
        return UsageView.model_validate(carrier.usage, from_attributes=True).total
    except ValidationError:
        return None


_UPSTREAM_OVERRIDES: Final = frozenset(
    (
        "api_key",
        "api_base",
        "base_url",
        "custom_llm_provider",
        "deployment_id",
        "specific_deployment",
        "model_list",
        "headers",
        "extra_headers",
        "extra_body",
        "mock_response",
        "mock_tool_calls",
        "mock_timeout",
        "mock_testing_fallbacks",
        "router_settings",
        "router_settings_override",
    )
)


def has_upstream_override(data: Mapping[str, object]) -> bool:
    return any(data.get(name) is not None for name in _UPSTREAM_OVERRIDES)


class StreamProgress(FrozenModel):
    terminal: bool = False
    failed: bool = False
    tokens: int | None = None

    usage: UsageView | None = None
    pending: bytes = Field(default=b"", repr=False)

    def feed(self, chunk: object) -> StreamProgress:
        if isinstance(chunk, ModelResponseStream):
            usage: Final = actual_usage(chunk)
            return self.model_copy(
                update={
                    "terminal": self.terminal or any(choice.finish_reason is not None for choice in chunk.choices),
                    "tokens": usage if usage is not None else self.tokens,
                }
            )
        if isinstance(chunk, (str, bytes)):
            raw: Final = self.pending + (chunk.encode() if isinstance(chunk, str) else chunk)
            complete, separator, pending = raw.rpartition(b"\n")
            return self.model_copy(update={"pending": pending})._events(complete + separator)
        return self._events(chunk)

    def _events(self, chunk: object) -> StreamProgress:
        from litellm.llms.anthropic.chat.guardrail_translation.handler import AnthropicMessagesHandler

        progress: StreamProgress = self  # rebind-ok: fold parsed events without retaining generated content
        events: Final = AnthropicMessagesHandler._iter_sse_events(  # pyright: ignore[reportPrivateUsage]  # shared SSE
            chunk
        )
        for payload in events:
            progress = progress._event(payload)
        return progress

    def _event(self, payload: Mapping[str, object]) -> StreamProgress:
        event: Final = _AnthropicEvent.model_validate(payload)
        current: Final = event.message.usage if event.message is not None else event.usage
        merged: Final = UsageView.model_validate(
            {
                **(self.usage.model_dump() if self.usage is not None else {}),
                **(current.model_dump(exclude_unset=True) if current is not None else {}),
            }
        )
        failed: Final = self.failed or event.type == "error"
        return self.model_copy(
            update={
                "failed": failed,
                "terminal": (self.terminal or event.type == "message_stop") and not failed,
                "usage": merged,
                "tokens": merged.total,
            }
        )


class _AnthropicMessage(FrozenModel):
    usage: UsageView | None = None


class _AnthropicEvent(FrozenModel):
    type: str
    message: _AnthropicMessage | None = None
    usage: UsageView | None = None
