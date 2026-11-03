from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from threading import Barrier
from typing import Annotated, Final, Protocol, Self

from e2e_http import AuthHeaders, StreamingResponse
from e2e_metadata import step
from models import ChatMessage
from pydantic import BaseModel, ConfigDict, Field, model_validator


class DeviceIdentity(BaseModel):
    model_config = ConfigDict(frozen=True)
    device_id: str
    account_uuid: str = ""
    session_id: str


class PoolMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)
    user_id: str


class PoolCache(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)
    no_cache: bool = Field(default=True, alias="no-cache")


class PoolRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    model: str
    messages: tuple[ChatMessage, ...]
    metadata: PoolMetadata | None = None
    max_tokens: int = 16
    stream: bool = False
    cache: PoolCache = Field(default_factory=PoolCache)

    @classmethod
    def for_device(cls, model: str, device: DeviceIdentity, prompt: str) -> Self:
        return cls.with_user_id(model, device.model_dump_json(), prompt)

    @classmethod
    def with_user_id(cls, model: str, user_id: str | None, prompt: str) -> Self:
        return cls(
            model=model,
            messages=(ChatMessage(role="user", content=prompt),),
            metadata=None if user_id is None else PoolMetadata(user_id=user_id),
        )


class PoolHeaders(AuthHeaders):
    model_config = ConfigDict(frozen=True, populate_by_name=True)
    forwarded_for: str | None = Field(default=None, serialization_alias="x-forwarded-for")


def pool_headers(key: str, ip: str | None = None) -> PoolHeaders:
    return PoolHeaders(authorization=f"Bearer {key}", forwarded_for=ip)


type ResourceId = Annotated[str, Field(min_length=1)]


class PoolSettings(BaseModel):
    model_config = ConfigDict(frozen=True)
    model: Annotated[str, Field(min_length=1)]
    primary_ids: tuple[ResourceId, ResourceId]
    fallback_id: ResourceId

    @model_validator(mode="after")
    def distinct_resources(self) -> Self:
        assert len(frozenset((*self.primary_ids, self.fallback_id))) == 3, "deployment IDs must be distinct"
        return self


class PoolError(BaseModel):
    model_config = ConfigDict(frozen=True)
    code: str


class PoolErrorBody(BaseModel):
    model_config = ConfigDict(frozen=True)
    error: PoolError


class PoolSender(Protocol):
    def send(self, path: str, *, headers: BaseModel, json: BaseModel) -> StreamingResponse: ...


@dataclass(frozen=True, slots=True)
class PoolClient:
    transport: PoolSender

    @step("Send a pool request with forwarded address {ip}")
    def send(self, key: str, request: PoolRequest, *, ip: str | None = None) -> StreamingResponse:
        return self.transport.send("/v1/chat/completions", headers=pool_headers(key, ip), json=request)


@dataclass(frozen=True, slots=True)
class PoolCall:
    key: str = field(repr=False)
    request: PoolRequest


@step("Send simultaneous requests from independent pool clients")
def simultaneous(client: PoolClient, calls: tuple[PoolCall, ...]) -> tuple[StreamingResponse, ...]:
    assert calls, "at least one client is required"
    barrier: Final = Barrier(len(calls))

    def send(call: PoolCall) -> StreamingResponse:
        barrier.wait(timeout=30)
        return client.send(call.key, call.request)

    with ThreadPoolExecutor(max_workers=len(calls)) as workers:
        return tuple(workers.map(send, calls))


def deployment_id(response: StreamingResponse) -> str:
    assert response.status_code == 200, f"expected successful generation, got status {response.status_code}"
    chosen: Final = response.headers.get("x-litellm-model-id")
    assert chosen, "successful response must expose the deployment ID"
    return chosen


def assert_pool_error(response: StreamingResponse, status: int, code: str) -> None:
    assert response.status_code == status, f"expected status {status}, got {response.status_code}"
    error: Final = PoolErrorBody.model_validate_json(response.body).error
    assert error.code == code, f"expected code {code}, got {error.code}"
