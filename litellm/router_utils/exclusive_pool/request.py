from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import Annotated, Final, NoReturn

from pydantic import Field, JsonValue, TypeAdapter, ValidationError

from litellm.proxy._types import ProxyException
from litellm.types.utils import ModelResponseStream

from .models import Failure, FrozenModel, Limits

TICKET_KEY: Final = "exclusive_endpoint_pool"


class Ticket(FrozenModel):
    operation: str
    client: str
    device: str
    resource: str
    probe: bool = False
    tokens: int = 1
    limits: Limits | None = None

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
    messages: tuple[Mapping[str, JsonValue], ...] = ()
    tools: tuple[Mapping[str, JsonValue], ...] = ()
    functions: tuple[Mapping[str, JsonValue], ...] = ()
    response_format: Mapping[str, JsonValue] | None = None
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

    def output_limit(self, server_limit: int) -> int:
        return min(self.max_completion_tokens or self.max_tokens or server_limit, server_limit)

    def reserved_tokens(self, output_limit: int) -> int:
        encoded: Final = json.dumps(
            {
                "messages": self.messages,
                "tools": self.tools,
                "functions": self.functions,
                "response_format": self.response_format,
                "tool_choice": self.tool_choice,
                "function_call": self.function_call,
            },
            ensure_ascii=False,
        )
        return len(encoded.encode()) + output_limit + 32 * len(self.messages)

    def ticket(self, secret: bytes) -> Ticket | None:
        return verified_ticket(self.metadata.get(TICKET_KEY, self.litellm_metadata.get(TICKET_KEY)), secret)


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
    total_tokens: int


def actual_usage(response: object) -> int | None:
    usage: Final = getattr(response, "usage", None)
    if usage is None:
        return None
    try:
        return UsageView.model_validate(usage, from_attributes=True).total_tokens
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
    tokens: int | None = None

    def feed(self, chunk: ModelResponseStream) -> StreamProgress:
        usage: Final = actual_usage(chunk)
        return StreamProgress(
            terminal=self.terminal or any(choice.finish_reason is not None for choice in chunk.choices),
            tokens=usage if usage is not None else self.tokens,
        )
