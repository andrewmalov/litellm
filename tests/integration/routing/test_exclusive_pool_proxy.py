import json
import os
import queue
import threading
import uuid
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import anyio
import httpx
import pytest
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import JsonValue, TypeAdapter
from redis import Redis

from litellm.router_utils.exclusive_pool.models import Limits, PoolConfig, Resource, Snapshot
from litellm.router_utils.exclusive_pool.state import empty_state
from tests.integration._support.asgi import asgi_server
from tests.integration._support.client import Gateway, eventually
from tests.integration._support.process import owned_proxy


@dataclass(frozen=True, slots=True)
class Rig:
    proxy: Gateway
    config: PoolConfig
    calls: queue.SimpleQueue[str]
    upstream_models: queue.SimpleQueue[str]
    stream_release: threading.Event
    fail_primary: threading.Event
    upstream: str
    payloads: queue.SimpleQueue[Mapping[str, JsonValue]]
    counts: queue.SimpleQueue[tuple[str, Mapping[str, JsonValue]]]
    count_betas: queue.SimpleQueue[frozenset[str]]
    generation_betas: queue.SimpleQueue[frozenset[str]]

    def chat(self, key: str, device: str | None, **extra: object) -> httpx.Response:
        return self.proxy.request(
            "POST",
            "/v1/chat/completions",
            {
                "model": self.config.models[0],
                "messages": [{"role": "user", "content": "say hello"}],
                "max_tokens": 16,
                **(
                    {"metadata": {"user_id": f'{{"device_id":"{device}","session_id":"session"}}'}}
                    if device is not None
                    else {}
                ),
                **extra,
            },
            key=key,
        )

    def messages(self, key: str, device: str, **extra: object) -> httpx.Response:
        return self.proxy.request(
            "POST",
            "/v1/messages?beta=true",
            {
                "model": self.config.models[0],
                "max_tokens": 16,
                "system": [{"type": "text", "text": "Answer briefly"}],
                "messages": [{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
                "metadata": {"user_id": json.dumps({"device_id": device})},
                **extra,
            },
            key=key,
        )


@pytest.fixture
def rig(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[Rig]:
    mode: Final = request.param if hasattr(request, "param") else False
    dynamic: Final = mode is True
    provider: Final = "anthropic" if mode == "anthropic" else "openai"
    marker: Final = uuid.uuid4().hex
    config: Final = PoolConfig(
        pool_id=f"pool-{marker}",
        discovery=dynamic,
        models=(f"exclusive-{marker}",),
        primaries=(Resource(id=f"a-{marker}", upstream_id="a"), Resource(id=f"b-{marker}", upstream_id="b")),
        fallback=Resource(id=f"f-{marker}", upstream_id="f", limits=Limits(parallel=2)),
        client_limits=Limits(parallel=1),
        heartbeat_seconds=0.1,
        heartbeat_timeout=1,
    )
    pool_config: Final = tmp_path / "pool.json"
    pool_config.write_text(config.model_dump_json())
    calls: Final = queue.SimpleQueue[str]()
    payloads: Final = queue.SimpleQueue[Mapping[str, JsonValue]]()
    upstream_models: Final = queue.SimpleQueue[str]()
    counts: Final = queue.SimpleQueue[tuple[str, Mapping[str, JsonValue]]]()
    count_betas: Final = queue.SimpleQueue[frozenset[str]]()
    generation_betas: Final = queue.SimpleQueue[frozenset[str]]()
    app: Final = FastAPI()
    stream_release: Final = threading.Event()
    fail_primary: Final = threading.Event()

    @app.post("/{resource}/v1/messages/count_tokens", response_model=None)
    async def count_tokens(resource: str, request: Request) -> JSONResponse:
        body: Final = TypeAdapter(dict[str, JsonValue]).validate_python(await request.json())
        assert "exclusive_endpoint_pool" not in str(body)
        assert request.headers.get("x-api-key") == "synthetic"
        counts.put((resource, body))
        count_betas.put(frozenset(request.headers.get("anthropic-beta", "").split(",")))
        if resource == config.fallback.id and request.headers.getlist("content-type") != ["application/json"]:
            return JSONResponse(status_code=415, content={"error": {"message": "expected one JSON content type"}})
        if body.get("system") == "count-unavailable":
            return JSONResponse(status_code=501, content={"error": {"message": "not implemented"}})
        return JSONResponse(content={"input_tokens": 61000 if body.get("system") == "count-too-large" else 100})

    @app.post("/{resource}/v1/messages", response_model=None)
    @app.post("/{resource}/v1/chat/completions", response_model=None)
    async def generate(resource: str, request: Request) -> dict[str, object] | StreamingResponse | JSONResponse:
        body: Final = await request.json()
        generation_betas.put(frozenset(request.headers.get("anthropic-beta", "").split(",")) - {""})
        assert "exclusive_endpoint_pool" not in str(body)
        payloads.put(TypeAdapter(dict[str, JsonValue]).validate_python(body))
        calls.put(resource)
        upstream_models.put(body["model"])
        if resource == config.primaries[0].id and fail_primary.is_set():
            fail_primary.clear()
            return JSONResponse(status_code=503, content={"error": {"message": "overloaded", "type": "server_error"}})
        if request.url.path.endswith("/messages"):
            native: Final = {
                "id": f"msg-{uuid.uuid4().hex}",
                "type": "message",
                "role": "assistant",
                "model": "exclusive-test-model",
                "content": [{"type": "text", "text": resource}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 11, "output_tokens": 7},
            }
            if body.get("stream"):
                return StreamingResponse(stream_messages(native, resource), media_type="text/event-stream")
            return native
        if body.get("stream"):
            return StreamingResponse(stream(resource), media_type="text/event-stream")
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": 0,
            "model": "exclusive-test-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": resource}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    async def stream(resource: str):
        yield (
            "data: "
            + json.dumps(
                {
                    "id": "chatcmpl-stream",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "exclusive-test-model",
                    "choices": [
                        {"index": 0, "delta": {"role": "assistant", "content": resource}, "finish_reason": None}
                    ],
                }
            )
            + "\n\n"
        )
        await anyio.to_thread.run_sync(stream_release.wait)
        yield (
            "data: "
            + json.dumps(
                {
                    "id": "chatcmpl-stream",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "exclusive-test-model",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
            )
            + "\n\n"
        )
        yield "data: [DONE]\n\n"

    async def stream_messages(native: Mapping[str, object], resource: str):
        events: Final = (
            {
                "type": "message_start",
                "message": {
                    **native,
                    "content": [],
                    "stop_reason": None,
                    "usage": {"input_tokens": 11, "output_tokens": 0},
                },
            },
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": resource}},
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 7},
            },
        )
        for event in events:
            yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
        await anyio.to_thread.run_sync(stream_release.wait)
        yield b'event: message_stop\ndata: {"type":"message_stop"}\n\n'

    redis: Final = Redis(host=os.environ["REDIS_HOST"], port=int(os.environ["REDIS_PORT"]))
    state_key: Final = f"litellm:exclusive-pool:{{{config.pool_id}}}:state"
    redis.set(state_key, empty_state(config).model_dump_json(), nx=True)
    with asgi_server(app) as upstream:
        proxy_config: Final = tmp_path / "proxy.yaml"
        proxy_config.write_text(
            yaml.safe_dump(
                {
                    "model_list": []
                    if dynamic
                    else [
                        {
                            "model_name": config.models[0],
                            "litellm_params": {
                                "model": f"{provider}/exclusive-test-model",
                                "api_base": f"{upstream}/{resource.id}" + ("/v1" if provider == "openai" else ""),
                                "api_key": "synthetic",
                                "max_retries": 0,
                                "timeout": 5,
                                "input_cost_per_token": 0,
                                "output_cost_per_token": 0,
                            },
                            "model_info": {"id": resource.id},
                        }
                        for resource in config.resources
                    ],
                    "litellm_settings": {
                        "callbacks": ["litellm.router_utils.exclusive_pool.configured.callback"],
                        "use_chat_completions_url_for_anthropic_messages": True,
                    },
                    "router_settings": {"num_retries": 0, "disable_cooldowns": True},
                    "general_settings": {"master_key": "os.environ/LITELLM_MASTER_KEY", "store_model_in_db": dynamic},
                }
            )
        )
        with httpx.Client(timeout=15, trust_env=False) as client:
            seed: Final = Gateway(client, "sk-exclusive-test-master", upstream)
            with owned_proxy(
                seed,
                tmp_path,
                {
                    "EXCLUSIVE_POOL_CONFIG": str(pool_config),
                    "EXCLUSIVE_POOL_REDIS_URL": f"redis://{os.environ['REDIS_HOST']}:{os.environ['REDIS_PORT']}/0",
                },
                config=proxy_config,
                workers=2,
            ) as proxy:
                try:
                    if dynamic:
                        for resource in config.resources:
                            proxy.post(
                                "/model/new",
                                {
                                    "model_name": config.models[0],
                                    "litellm_params": {
                                        "model": f"{provider}/exclusive-test-model",
                                        "api_base": f"{upstream}/{resource.id}"
                                        + ("/v1" if provider == "openai" else ""),
                                        "api_key": "synthetic",
                                        "max_retries": 0,
                                        "input_cost_per_token": 0,
                                        "output_cost_per_token": 0,
                                    },
                                    "model_info": {
                                        "id": resource.id,
                                        "exclusive_pool": config.pool_id,
                                        "exclusive_pool_role": "fallback"
                                        if config.is_fallback(resource.id)
                                        else "primary",
                                        "exclusive_pool_limits": resource.limits.model_dump(),
                                    },
                                },
                            )
                    yield Rig(
                        proxy,
                        config,
                        calls,
                        upstream_models,
                        stream_release,
                        fail_primary,
                        upstream,
                        payloads,
                        counts,
                        count_betas,
                        generation_betas,
                    )
                finally:
                    stream_release.set()
                    if dynamic:
                        for resource in config.resources:
                            proxy.post("/model/delete", {"id": resource.id})
    redis.delete(state_key)
    redis.close()


@pytest.mark.parametrize("rig", [False, True], indirect=True)
def test_real_proxy_keeps_key_assignments_across_device_changes_and_missing_identity(rig: Rig) -> None:
    keys: Final = tuple(
        str(rig.proxy.post("/key/generate", {"models": list(rig.config.models)})["key"]) for _ in range(4)
    )
    try:
        initial: Final = tuple(rig.chat(key, None) for key in keys)
        assert tuple(reply.status_code for reply in initial) == (200, 200, 200, 200), tuple(
            reply.text for reply in initial
        )
        expected: Final = (*tuple(r.id for r in rig.config.primaries), rig.config.fallback.id, rig.config.fallback.id)
        assert tuple(reply.headers["x-litellm-model-id"] for reply in initial) == expected
        for index, key in enumerate(keys):
            changed: Final = rig.chat(key, "other-device")
            assert changed.status_code == 200, changed.text
            assert changed.headers["x-litellm-model-id"] == expected[index]
            accepted: Final = rig.chat(key, None, metadata={"user_id": "not-json"})
            assert accepted.status_code == 200, accepted.text
            assert accepted.headers["x-litellm-model-id"] == expected[index]
        limited_key: Final = str(
            rig.proxy.post("/key/generate", {"models": list(rig.config.models), "rpm_limit": 1})["key"]
        )
        try:
            assert rig.chat(limited_key, "limited-device").status_code == 200
            limited: Final = rig.chat(limited_key, "changed-device")
            assert limited.status_code == 429, limited.text
            assert limited.json()["error"]["code"] == "client_rate_limit_exceeded", limited.text
            assert float(limited.headers.get("retry-after", "0")) > 0
        finally:
            rig.proxy.post("/key/delete", {"keys": [limited_key]})
    finally:
        rig.proxy.post("/key/delete", {"keys": list(keys)})


@pytest.mark.parametrize("rig", [False, "anthropic"], indirect=True)
@pytest.mark.parametrize("route", ["chat/completions", "messages?beta=true"])
def test_cancelled_client_stream_does_not_release_an_upstream_that_is_still_running(rig: Rig, route: str) -> None:
    keys: Final = tuple(
        str(rig.proxy.post("/key/generate", {"models": list(rig.config.models)})["key"]) for _ in range(2)
    )
    try:
        with rig.proxy.client.stream(
            "POST",
            f"/v1/{route}",
            headers={"Authorization": f"Bearer {keys[0]}"},
            json={
                "model": rig.config.models[0],
                "messages": [{"role": "user", "content": "stream"}],
                "max_tokens": 16,
                "stream": True,
                "metadata": {"user_id": '{"device_id":"stream-device"}'},
            },
        ) as streamed:
            assert streamed.status_code == 200
            assert next(line for line in streamed.iter_lines() if line.startswith("data: "))
        next_client: Final = rig.chat(keys[1], "other-device")
        assert next_client.status_code == 200, next_client.text
        assert next_client.headers["x-litellm-model-id"] == rig.config.primaries[1].id
        changed: Final = rig.chat(keys[0], "other-device")
        assert changed.status_code == 429, changed.text
        assert changed.json()["error"]["code"] == "client_rate_limit_exceeded"
    finally:
        rig.stream_release.set()
        rig.proxy.post("/key/delete", {"keys": list(keys)})


def test_completed_stream_releases_its_inflight_slot(rig: Rig) -> None:
    key: Final = str(rig.proxy.post("/key/generate", {"models": list(rig.config.models)})["key"])
    rig.stream_release.set()
    try:
        streamed: Final = rig.chat(key, "stream-device", stream=True)
        assert streamed.status_code == 200, streamed.text
        assert "[DONE]" in streamed.text
        next_call: Final = rig.chat(key, "stream-device")
        assert next_call.status_code == 200, next_call.text
        assert next_call.headers["x-litellm-model-id"] == streamed.headers["x-litellm-model-id"]
    finally:
        rig.proxy.post("/key/delete", {"keys": [key]})


def test_confirmed_failure_moves_next_request_and_actual_generation_probe_recovers_resource(rig: Rig) -> None:
    key: Final = str(rig.proxy.post("/key/generate", {"models": list(rig.config.models)})["key"])
    rig.fail_primary.set()
    try:
        failed: Final = rig.chat(key, "device")
        assert failed.status_code == 503, failed.text
        assert rig.calls.get(timeout=5) == rig.config.primaries[0].id
        moved: Final = rig.chat(key, "device")
        assert moved.status_code == 200, moved.text
        assert moved.headers["x-litellm-model-id"] == rig.config.primaries[1].id
        assert rig.calls.get(timeout=5) == rig.config.primaries[1].id
        assert rig.calls.get(timeout=25) == rig.config.primaries[0].id
        repeated: Final = rig.chat(key, "device")
        assert repeated.status_code == 200, repeated.text
        assert repeated.headers["x-litellm-model-id"] == rig.config.primaries[1].id
    finally:
        rig.proxy.post("/key/delete", {"keys": [key]})


def test_two_workers_share_key_assignment_across_devices_and_fail_closed_after_state_loss(rig: Rig) -> None:
    key: Final = str(rig.proxy.post("/key/generate", {"models": list(rig.config.models)})["key"])
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            replies: Final = tuple(executor.map(lambda device: rig.chat(key, device), ("left", "right")))
        assert all(reply.status_code in (200, 429) for reply in replies), tuple(reply.text for reply in replies)
        for reply in replies:
            if reply.status_code == 200:
                assert reply.headers["x-litellm-model-id"] == rig.config.primaries[0].id
                assert rig.calls.get(timeout=5) == rig.config.primaries[0].id
                continue
            assert reply.json()["error"]["code"] == "client_rate_limit_exceeded"
        for device in ("left", "right"):
            repeated: Final = rig.chat(key, device)
            assert repeated.status_code == 200, repeated.text
            assert repeated.headers["x-litellm-model-id"] == rig.config.primaries[0].id
            assert rig.calls.get(timeout=5) == rig.config.primaries[0].id
        redis: Final = Redis(host=os.environ["REDIS_HOST"], port=int(os.environ["REDIS_PORT"]))
        try:
            redis.delete(f"litellm:exclusive-pool:{{{rig.config.pool_id}}}:state")
            unavailable: Final = rig.chat(key, "left")
            assert unavailable.status_code == 503, unavailable.text
            assert unavailable.json()["error"]["code"] == "routing_state_unavailable"
            assert rig.calls.empty()
        finally:
            redis.close()
    finally:
        rig.proxy.post("/key/delete", {"keys": [key]})


@pytest.mark.parametrize("rig", [True], indirect=True)
def test_standard_model_api_addition_and_removal_update_pool_without_restart(rig: Rig) -> None:
    redis: Final = Redis(host=os.environ["REDIS_HOST"], port=int(os.environ["REDIS_PORT"]))
    state_key: Final = f"litellm:exclusive-pool:{{{rig.config.pool_id}}}:state"
    identifier: Final = f"new-{rig.config.pool_id}"
    keys: Final = tuple(
        str(rig.proxy.post("/key/generate", {"models": list(rig.config.models)})["key"]) for _ in range(4)
    )
    try:
        initial: Final = tuple(rig.chat(key, "device") for key in keys[:2])
        assert tuple(reply.status_code for reply in initial) == (200, 200), tuple(reply.text for reply in initial)
        rig.proxy.post(
            "/model/new",
            {
                "model_name": rig.config.models[0],
                "litellm_params": {
                    "model": "openai/exclusive-test-model",
                    "api_base": f"{rig.upstream}/{identifier}/v1",
                    "api_key": "synthetic",
                    "max_retries": 0,
                    "input_cost_per_token": 0,
                    "output_cost_per_token": 0,
                },
                "model_info": {
                    "id": identifier,
                    "exclusive_pool": rig.config.pool_id,
                    "exclusive_pool_role": "primary",
                },
            },
        )
        eventually(
            lambda: Snapshot.model_validate_json(redis.get(state_key)),
            lambda snapshot: any(entry.resource.id == identifier and entry.active for entry in snapshot.catalog),
        )
        added: Final = eventually(lambda: rig.chat(keys[2], "device"), lambda reply: reply.status_code == 200)
        assert added.headers["x-litellm-model-id"] == identifier, added.text
        sticky: Final = rig.chat(keys[0], "device")
        assert (
            sticky.status_code == 200
            and sticky.headers["x-litellm-model-id"] == initial[0].headers["x-litellm-model-id"]
        )
        rig.proxy.post("/model/delete", {"id": identifier})
        eventually(
            lambda: Snapshot.model_validate_json(redis.get(state_key)),
            lambda snapshot: any(entry.resource.id == identifier and not entry.active for entry in snapshot.catalog),
        )
        moved: Final = rig.chat(keys[2], "device")
        assert moved.status_code == 200, moved.text
        assert rig.config.fallback is not None
        assert moved.headers["x-litellm-model-id"] == rig.config.fallback.id
        changed: Final = rig.chat(keys[2], "another-device")
        assert changed.status_code == 200, changed.text
        assert changed.headers["x-litellm-model-id"] == moved.headers["x-litellm-model-id"]
    finally:
        rig.proxy.post("/key/delete", {"keys": list(keys)})
        redis.close()


@pytest.mark.parametrize("rig", [True], indirect=True)
def test_direct_deployment_responses_cannot_mask_pool_identity_with_caller_metadata(rig: Rig) -> None:
    response: Final = rig.proxy.request(
        "POST",
        "/v1/responses",
        {"model": rig.config.primaries[0].id, "input": "hello", "metadata": {"model_info": {"id": "fake"}}},
    )
    assert response.status_code in (400, 503), response.text
    assert rig.calls.empty()


@pytest.mark.parametrize("rig", [True], indirect=True)
def test_unmarked_model_keeps_serving_when_pool_state_is_lost(rig: Rig) -> None:
    identifier: Final = f"ordinary-{rig.config.pool_id}"
    rig.proxy.post(
        "/model/new",
        {
            "model_name": identifier,
            "litellm_params": {
                "model": "openai/exclusive-test-model",
                "api_base": f"{rig.upstream}/ordinary/v1",
                "api_key": "synthetic",
                "input_cost_per_token": 0,
                "output_cost_per_token": 0,
            },
            "model_info": {"id": identifier},
        },
    )
    redis: Final = Redis(host=os.environ["REDIS_HOST"], port=int(os.environ["REDIS_PORT"]))
    state_key: Final = f"litellm:exclusive-pool:{{{rig.config.pool_id}}}:state"
    redis.delete(state_key)
    try:
        ordinary: Final = rig.proxy.request(
            "POST", "/v1/chat/completions", {"model": identifier, "messages": [{"role": "user", "content": "hello"}]}
        )
        assert ordinary.status_code == 200, ordinary.text
        assert ordinary.json()["choices"][0]["message"]["content"] == "ordinary"
        protected: Final = rig.chat(rig.proxy.key, "device")
        assert protected.status_code == 503, protected.text
        assert redis.exists(state_key) == 0
    finally:
        rig.proxy.post("/model/delete", {"id": identifier})
        redis.close()


@pytest.mark.parametrize("rig", [True], indirect=True)
def test_multiple_models_share_physical_ownership_through_standard_model_api(rig: Rig) -> None:
    aliases: Final = ("sonnet", "haiku")
    public_models: Final = (rig.config.models[0], *(f"{rig.config.models[0]}-{model}" for model in aliases))
    keys: Final = tuple(str(rig.proxy.post("/key/generate", {"models": list(public_models)})["key"]) for _ in range(4))
    redis: Final = Redis(host=os.environ["REDIS_HOST"], port=int(os.environ["REDIS_PORT"]))
    state_key: Final = f"litellm:exclusive-pool:{{{rig.config.pool_id}}}:state"
    try:
        for resource in rig.config.resources:
            for model in aliases:
                rig.proxy.post(
                    "/model/new",
                    {
                        "model_name": f"{rig.config.models[0]}-{model}",
                        "litellm_params": {
                            "model": f"openai/exclusive-test-{model}",
                            "api_base": f"{rig.upstream}/{resource.id}/v1",
                            "api_key": "synthetic",
                            "max_retries": 0,
                            "input_cost_per_token": 0,
                            "output_cost_per_token": 0,
                        },
                        "model_info": {
                            "id": f"{resource.id}-{model}",
                            "exclusive_pool": rig.config.pool_id,
                            "exclusive_pool_role": "fallback" if rig.config.is_fallback(resource.id) else "primary",
                            "exclusive_upstream_id": resource.id,
                            "exclusive_pool_limits": resource.limits.model_dump(),
                        },
                    },
                )
        eventually(
            lambda: Snapshot.model_validate_json(redis.get(state_key)),
            lambda snapshot: len(snapshot.catalog) == len(rig.config.resources) * len(public_models),
        )
        first: Final = rig.chat(keys[0], "device")
        assert first.status_code == 200, first.text
        assert first.headers["x-litellm-model-id"] == rig.config.primaries[0].id
        assert rig.upstream_models.get(timeout=5) == "exclusive-test-model"
        for model in aliases:
            switched: Final = eventually(
                lambda selected=model: rig.chat(keys[0], "device", model=f"{rig.config.models[0]}-{selected}"),
                lambda reply: reply.status_code == 200,
            )
            assert switched.headers["x-litellm-model-id"] == f"{rig.config.primaries[0].id}-{model}", switched.text
            assert switched.json()["choices"][0]["message"]["content"] == rig.config.primaries[0].id
            assert rig.upstream_models.get(timeout=5) == f"exclusive-test-{model}"
        second: Final = rig.chat(keys[1], "device", model=public_models[1])
        assert second.status_code == 200, second.text
        assert second.headers["x-litellm-model-id"] == f"{rig.config.primaries[1].id}-sonnet"
        reserve: Final = rig.chat(keys[2], "device", model=public_models[2])
        assert reserve.status_code == 200, reserve.text
        assert rig.config.fallback is not None
        assert reserve.headers["x-litellm-model-id"] == f"{rig.config.fallback.id}-haiku"
        shared: Final = rig.chat(keys[3], "device", model=public_models[1])
        assert shared.status_code == 200, shared.text
        assert shared.headers["x-litellm-model-id"] == f"{rig.config.fallback.id}-sonnet"
        changed: Final = rig.chat(keys[0], "other-device", model=public_models[2])
        assert changed.status_code == 200, changed.text
        assert changed.headers["x-litellm-model-id"] == f"{rig.config.primaries[0].id}-haiku"
    finally:
        rig.proxy.post("/key/delete", {"keys": list(keys)})
        for resource in rig.config.resources:
            for model in aliases:
                rig.proxy.post("/model/delete", {"id": f"{resource.id}-{model}"})
        redis.close()


@pytest.mark.parametrize("rig", [False, "anthropic"], indirect=True)
def test_messages_share_key_assignments_and_fallback_with_chat_across_device_changes(rig: Rig) -> None:
    keys: Final = tuple(
        str(rig.proxy.post("/key/generate", {"models": list(rig.config.models)})["key"]) for _ in range(3)
    )

    try:
        first: Final = rig.messages(keys[0], "desktop")
        assert first.status_code == 200, first.text
        assert first.json()["type"] == "message"
        assert first.json()["content"][0]["text"] == rig.config.primaries[0].id
        chat: Final = rig.chat(keys[0], None)
        assert chat.status_code == 200, chat.text
        assert chat.headers["x-litellm-model-id"] == first.headers["x-litellm-model-id"]
        changed: Final = rig.messages(keys[0], "another-desktop")
        assert changed.status_code == 200, changed.text
        assert changed.headers["x-litellm-model-id"] == first.headers["x-litellm-model-id"]
        rest: Final = tuple(rig.messages(key, "desktop") for key in keys[1:])
        assert tuple(reply.status_code for reply in rest) == (200, 200), tuple(reply.text for reply in rest)
        assert tuple(reply.headers["x-litellm-model-id"] for reply in rest) == (
            rig.config.primaries[1].id,
            rig.config.fallback.id,
        )
    finally:
        rig.proxy.post("/key/delete", {"keys": list(keys)})


@pytest.mark.parametrize("rig", [False, "anthropic"], indirect=True)
def test_messages_stream_releases_inflight_slots_and_keeps_assignment(rig: Rig) -> None:
    key: Final = str(rig.proxy.post("/key/generate", {"models": list(rig.config.models)})["key"])
    rig.stream_release.set()
    try:
        responses: Final = tuple(rig.messages(key, "desktop", stream=True) for _ in range(3))
        for response in responses:
            assert response.status_code == 200, response.text
            assert "event: message_stop" in response.text, response.text
            assert '"type": "error"' not in response.text, response.text
            assert response.headers["x-litellm-model-id"] == rig.config.primaries[0].id
    finally:
        rig.proxy.post("/key/delete", {"keys": [key]})


@pytest.mark.parametrize("rig", [False, "anthropic"], indirect=True)
def test_messages_client_rpm_limit_is_enforced_before_upstream(rig: Rig) -> None:
    key: Final = str(rig.proxy.post("/key/generate", {"models": list(rig.config.models), "rpm_limit": 1})["key"])
    try:
        accepted: Final = rig.messages(key, "desktop")
        assert accepted.status_code == 200, accepted.text
        assert rig.calls.get(timeout=5) == rig.config.primaries[0].id
        limited: Final = rig.messages(key, "desktop")
        assert limited.status_code == 429, limited.text
        assert rig.calls.empty()
        assert float(limited.headers.get("retry-after", "0")) > 0
    finally:
        rig.proxy.post("/key/delete", {"keys": [key]})


@pytest.mark.parametrize("rig", [False, "anthropic"], indirect=True)
def test_messages_confirmed_failure_moves_next_request_without_replaying(rig: Rig) -> None:
    key: Final = str(rig.proxy.post("/key/generate", {"models": list(rig.config.models)})["key"])
    tools: Final = [{"name": "lookup", "input_schema": {"type": "object", "properties": {}}}]
    rig.fail_primary.set()
    try:
        failed: Final = rig.messages(key, "desktop", tools=tools)
        assert failed.status_code == 503, failed.text
        assert rig.calls.get(timeout=5) == rig.config.primaries[0].id
        assert rig.calls.empty()
        moved: Final = rig.messages(key, "desktop", tools=tools)
        assert moved.status_code == 200, moved.text
        assert moved.headers["x-litellm-model-id"] == rig.config.primaries[1].id
        assert rig.counts.empty()
    finally:
        rig.proxy.post("/key/delete", {"keys": [key]})


@pytest.mark.parametrize("rig", [False, "anthropic"], indirect=True)
@pytest.mark.parametrize("with_tools", [False, True])
def test_messages_large_text_is_estimated_locally_on_the_assigned_upstream(rig: Rig, with_tools: bool) -> None:
    key: Final = str(rig.proxy.post("/key/generate", {"models": list(rig.config.models)})["key"])
    description: Final = "x" * rig.config.client_limits.tpm
    system_text: Final = "y" * rig.config.client_limits.tpm
    tools: Final = (
        [
            {
                "name": "lookup",
                "description": description,
                "input_schema": {"type": "object", "properties": {}},
            }
        ]
        if with_tools
        else []
    )
    system: Final = [{"type": "text", "text": system_text}]
    messages: Final = (
        [
            {"role": "user", "content": "look up a value"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "tool-1", "name": "lookup", "input": {}}]},
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "result-marker"}],
            },
        ]
        if with_tools
        else [{"role": "user", "content": "hello"}]
    )
    try:
        response: Final = rig.messages(
            key,
            "desktop",
            tools=tools,
            system=system,
            messages=messages,
        )
        assert response.status_code == 200, response.text
        payload: Final = rig.payloads.get(timeout=5)
        assert ("result-marker" if with_tools else "hello") in json.dumps(payload["messages"])
        if with_tools:
            assert "lookup" in json.dumps(payload["tools"])
            assert description in json.dumps(payload["tools"])
        assert system_text in json.dumps(payload)
        assert rig.counts.empty()
        assert response.headers["x-litellm-model-id"] == rig.config.primaries[0].id
    finally:
        rig.proxy.post("/key/delete", {"keys": [key]})


@pytest.mark.parametrize("rig", ["anthropic"], indirect=True)
@pytest.mark.parametrize(
    "block",
    [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "aGVsbG8="}},
        {"type": "image", "source": {"type": "url", "url": "https://example.invalid/image.png"}},
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "cGRm"}},
        {"type": "document", "source": {"type": "url", "url": "https://example.invalid/file.pdf"}},
        {"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": "document text"}},
    ],
)
def test_messages_attachments_are_estimated_locally_and_forwarded_unchanged(
    rig: Rig,
    block: dict[str, object],
) -> None:
    key: Final = str(rig.proxy.post("/key/generate", {"models": list(rig.config.models)})["key"])
    messages: Final = [{"role": "user", "content": [block]}]
    try:
        response: Final = rig.messages(key, "desktop", messages=messages)
        assert response.status_code == 200, response.text
        assert rig.payloads.get(timeout=5)["messages"] == messages
        assert rig.counts.empty()
    finally:
        rig.proxy.post("/key/delete", {"keys": [key]})


@pytest.mark.parametrize("rig", ["anthropic"], indirect=True)
@pytest.mark.parametrize("system,tpm_limit,status", [("count-unavailable", 60000, 200), ("ordinary text", 32, 429)])
@pytest.mark.parametrize("with_tools", [False, True])
def test_messages_use_local_estimates_and_reject_real_budget_overflow(
    rig: Rig, system: str, tpm_limit: int, status: int, with_tools: bool
) -> None:
    key: Final = str(
        rig.proxy.post("/key/generate", {"models": list(rig.config.models), "tpm_limit": tpm_limit})["key"]
    )
    try:
        response: Final = rig.messages(
            key,
            "desktop",
            system=system,
            messages=[{"role": "user", "content": "local token budget " * 100}],
            tools=[{"name": "lookup", "input_schema": {"type": "object", "properties": {}}}] if with_tools else [],
        )
        assert response.status_code == status, response.text
        assert rig.counts.empty()
        if status == 200:
            assert rig.calls.get(timeout=5) == response.headers["x-litellm-model-id"]
            return
        assert rig.calls.empty()
        recovered: Final = rig.messages(key, "desktop")
        assert recovered.status_code == 200, recovered.text
        assert rig.calls.get(timeout=5) == recovered.headers["x-litellm-model-id"]
        assert rig.calls.empty()
    finally:
        rig.proxy.post("/key/delete", {"keys": [key]})


@pytest.mark.parametrize("rig", ["anthropic"], indirect=True)
def test_messages_server_references_pin_the_account_and_block_cross_account_fallback(rig: Rig) -> None:
    key: Final = str(rig.proxy.post("/key/generate", {"models": list(rig.config.models)})["key"])
    try:
        unbound: Final = rig.messages(key, "desktop", container="container-one")
        assert unbound.status_code == 409, unbound.text
        assert rig.calls.empty()
        assert rig.counts.empty()
        created: Final = rig.messages(
            key,
            "desktop",
            tools=[
                {
                    "type": "code_execution_20250825",
                    "name": "code_execution",
                }
            ],
        )
        assert created.status_code == 200, created.text
        resource: Final = created.headers["x-litellm-model-id"]
        rig.fail_primary.set()
        failed: Final = rig.messages(
            key,
            "new-desktop-session",
            container="container-one",
            messages=[
                {
                    "role": "user",
                    "content": [{"type": "document", "source": {"type": "file", "file_id": "file-one"}}],
                }
            ],
        )
        assert failed.status_code == 503, failed.text
        assert rig.calls.get(timeout=5) == resource
        assert rig.calls.get(timeout=5) == resource
        assert rig.payloads.get(timeout=5)["tools"] == [
            {
                "type": "code_execution_20250825",
                "name": "code_execution",
            }
        ]
        assert rig.payloads.get(timeout=5)["container"] == "container-one"
        retry: Final = rig.messages(key, "third-desktop-session", container="container-one")
        assert retry.status_code == 503, retry.text
        assert rig.calls.empty()
        assert rig.counts.empty()
    finally:
        rig.proxy.post("/key/delete", {"keys": [key]})


@pytest.mark.parametrize("rig", ["anthropic"], indirect=True)
def test_messages_portable_attachment_failure_can_move_to_another_account(rig: Rig) -> None:
    key: Final = str(rig.proxy.post("/key/generate", {"models": list(rig.config.models)})["key"])
    messages: Final = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "url",
                        "url": "https://example.invalid/image.png",
                    },
                }
            ],
        }
    ]
    rig.fail_primary.set()
    try:
        failed: Final = rig.messages(key, "desktop", messages=messages)
        assert failed.status_code == 503, failed.text
        moved: Final = rig.messages(key, "desktop", messages=messages)
        assert moved.status_code == 200, moved.text
        assert rig.calls.get(timeout=5) == rig.config.primaries[0].id
        assert rig.calls.get(timeout=5) == rig.config.primaries[1].id
        assert rig.counts.empty()
        assert rig.calls.empty()
    finally:
        rig.proxy.post("/key/delete", {"keys": [key]})


@pytest.mark.parametrize("rig", ["anthropic"], indirect=True)
def test_messages_attachment_reaches_strict_fallback_without_duplicate_content_type(rig: Rig) -> None:
    keys: Final = tuple(
        str(rig.proxy.post("/key/generate", {"models": list(rig.config.models)})["key"]) for _ in range(3)
    )
    try:
        for key in keys[:2]:
            text: Final = rig.messages(key, "desktop")
            assert text.status_code == 200, text.text
        response: Final = rig.messages(
            keys[2],
            "desktop",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "url",
                                "url": "https://example.invalid/image.png",
                            },
                        }
                    ],
                }
            ],
        )
        assert response.status_code == 200, response.text
        assert response.headers["x-litellm-model-id"] == rig.config.fallback.id
        assert rig.counts.empty()
        assert tuple(rig.calls.get(timeout=5) for _ in range(3)) == tuple(r.id for r in rig.config.resources)
        assert rig.calls.empty()
    finally:
        rig.proxy.post("/key/delete", {"keys": list(keys)})
