from dataclasses import dataclass
from threading import Barrier
from typing import Final

import pytest
from e2e_http import StreamingResponse
from pydantic import BaseModel, ValidationError
from router.exclusive_pool_client import (
    DeviceIdentity,
    PoolCall,
    PoolClient,
    PoolHeaders,
    PoolRequest,
    PoolSettings,
    assert_pool_error,
    deployment_id,
    pool_headers,
    simultaneous,
)


def test_identity_preserves_device_across_sessions() -> None:
    first: Final = DeviceIdentity(device_id="device-a", session_id="session-a")
    second: Final = DeviceIdentity(device_id="device-a", session_id="session-b")
    request: Final = PoolRequest.for_device("pool", first, "hello")
    assert request.model_dump(mode="json")["metadata"] == {"user_id": first.model_dump_json()}
    assert first.model_dump_json() != second.model_dump_json()
    assert request.max_tokens is not None and request.max_tokens > 0
    assert request.messages[0].content == "hello"


@pytest.mark.parametrize("raw", [None, "", "{", "{}", '{"device_id":42}'])
def test_invalid_identity_reaches_proxy_verbatim(raw: str | None) -> None:
    request: Final = PoolRequest.with_user_id("pool", raw, "hello")
    wire: Final = request.model_dump(mode="json", exclude_none=True)
    assert wire.get("metadata") == (None if raw is None else {"user_id": raw})


def test_secrets_are_hidden_from_headers_and_request_repr() -> None:
    headers: Final = pool_headers("sk-private", "192.0.2.1")
    assert "sk-private" not in repr(headers)
    assert headers.model_dump(by_alias=True)["authorization"] == "Bearer sk-private"
    assert headers.model_dump(by_alias=True)["x-forwarded-for"] == "192.0.2.1"


@pytest.mark.parametrize("status", [400, 409, 429, 503])
def test_error_assertion_rejects_wrong_status_and_code(status: int) -> None:
    response: Final = StreamingResponse(status_code=status, body='{"error":{"code":"expected"}}')
    assert_pool_error(response, status, "expected")
    with pytest.raises(AssertionError, match="status"):
        assert_pool_error(response, 200, "expected")
    with pytest.raises(AssertionError, match="code"):
        assert_pool_error(response, status, "wrong")


@pytest.mark.parametrize("body", ["{}", '{"error":{}}', "not json"])
def test_error_assertion_does_not_accept_unstructured_failures(body: str) -> None:
    with pytest.raises(ValidationError):
        assert_pool_error(StreamingResponse(status_code=409, body=body), 409, "conflict")


def test_deployment_readback_requires_success_and_nonempty_id() -> None:
    assert (
        deployment_id(StreamingResponse(status_code=200, headers={"x-litellm-model-id": "primary-a"}, body="{}"))
        == "primary-a"
    )
    with pytest.raises(AssertionError, match="deployment"):
        deployment_id(StreamingResponse(status_code=200, body="{}"))
    with pytest.raises(AssertionError, match="status"):
        deployment_id(StreamingResponse(status_code=503, headers={"x-litellm-model-id": "primary-a"}, body="{}"))


@pytest.mark.parametrize("ids,fallback", [(("a", "a"), "f"), (("a", "b"), "a"), (("", "b"), "f")])
def test_settings_reject_ambiguous_resource_ids(ids: tuple[str, str], fallback: str) -> None:
    with pytest.raises(ValidationError):
        PoolSettings(model="pool", primary_ids=ids, fallback_id=fallback)


def test_client_uses_injected_transport_and_forwards_identity_and_key() -> None:
    request: Final = PoolRequest.for_device("pool", DeviceIdentity(device_id="a", session_id="s"), "hello")
    response: Final = StreamingResponse(status_code=200, body="result")

    @dataclass(frozen=True, slots=True)
    class CheckingSender:
        def send(self, path: str, *, headers: BaseModel, json: BaseModel) -> StreamingResponse:
            assert path == "/v1/chat/completions"
            assert isinstance(headers, PoolHeaders)
            assert headers.authorization == "Bearer sk-private"
            assert headers.forwarded_for == "192.0.2.1"
            assert json is request
            assert json.model_dump(by_alias=True)["cache"] == {"no-cache": True}
            return response

    assert PoolClient(CheckingSender()).send("sk-private", request, ip="192.0.2.1") is response


def test_simultaneous_requests_are_sent_concurrently_and_keep_input_order() -> None:
    entered: Final = Barrier(2)

    @dataclass(frozen=True, slots=True)
    class ConcurrentSender:
        def send(self, path: str, *, headers: BaseModel, json: BaseModel) -> StreamingResponse:
            assert isinstance(headers, PoolHeaders)
            assert isinstance(json, PoolRequest)
            assert path == "/v1/chat/completions"
            assert headers.authorization == f"Bearer {json.model}"
            entered.wait(timeout=2)
            return StreamingResponse(status_code=200, body=json.model)

    calls: Final = tuple(PoolCall(key=key, request=PoolRequest.with_user_id(key, None, "hello")) for key in ("a", "b"))
    replies: Final = simultaneous(PoolClient(ConcurrentSender()), calls)
    assert tuple(reply.body for reply in replies) == ("a", "b")
