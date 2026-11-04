import json
import os
import queue
import threading
import uuid
from collections.abc import Iterator
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


@pytest.fixture
def rig(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[Rig]:
    dynamic: Final = request.param if hasattr(request, "param") else False
    marker: Final = uuid.uuid4().hex
    config: Final = PoolConfig(
        pool_id=f"pool-{marker}",
        discovery=dynamic,
        models=(f"exclusive-{marker}",),
        primaries=(Resource(id=f"a-{marker}", upstream_id="a"), Resource(id=f"b-{marker}", upstream_id="b")),
        fallback=Resource(id=f"f-{marker}", upstream_id="f", limits=Limits(parallel=2)),
        client_limits=Limits(parallel=2),
        heartbeat_seconds=0.1,
        heartbeat_timeout=1,
    )
    pool_config: Final = tmp_path / "pool.json"
    pool_config.write_text(config.model_dump_json())
    calls: Final = queue.SimpleQueue[str]()
    upstream_models: Final = queue.SimpleQueue[str]()
    app: Final = FastAPI()
    stream_release: Final = threading.Event()
    fail_primary: Final = threading.Event()

    @app.post("/{resource}/v1/chat/completions", response_model=None)
    async def generate(resource: str, request: Request) -> dict[str, object] | StreamingResponse | JSONResponse:
        body: Final = await request.json()
        assert "exclusive_endpoint_pool" not in str(body)
        calls.put(resource)
        upstream_models.put(body["model"])
        if resource == config.primaries[0].id and fail_primary.is_set():
            fail_primary.clear()
            return JSONResponse(status_code=503, content={"error": {"message": "overloaded", "type": "server_error"}})
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
                                "model": "openai/exclusive-test-model",
                                "api_base": f"{upstream}/{resource.id}/v1",
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
                    "litellm_settings": {"callbacks": ["litellm.router_utils.exclusive_pool.configured.callback"]},
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
                                        "model": "openai/exclusive-test-model",
                                        "api_base": f"{upstream}/{resource.id}/v1",
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
                    yield Rig(proxy, config, calls, upstream_models, stream_release, fail_primary, upstream)
                finally:
                    stream_release.set()
                    if dynamic:
                        for resource in config.resources:
                            proxy.post("/model/delete", {"id": resource.id})
    redis.delete(state_key)
    redis.close()


@pytest.mark.parametrize("rig", [False, True], indirect=True)
def test_real_proxy_enforces_device_binding_and_sticky_primary_and_fallback(rig: Rig) -> None:
    keys: Final = tuple(
        str(rig.proxy.post("/key/generate", {"models": list(rig.config.models)})["key"]) for _ in range(4)
    )
    try:
        invalid: Final = rig.chat(keys[0], None)
        assert invalid.status_code == 400, invalid.text
        assert invalid.json()["error"]["code"] == "device_id_required"
        assert rig.calls.empty()
        initial: Final = tuple(rig.chat(key, f"device-{index}") for index, key in enumerate(keys))
        assert tuple(reply.status_code for reply in initial) == (200, 200, 200, 200), tuple(
            reply.text for reply in initial
        )
        expected: Final = (*tuple(r.id for r in rig.config.primaries), rig.config.fallback.id, rig.config.fallback.id)
        assert tuple(reply.headers["x-litellm-model-id"] for reply in initial) == expected
        for index, key in enumerate(keys):
            conflict: Final = rig.chat(key, "other-device")
            assert conflict.status_code == 409, conflict.text
            assert conflict.json()["error"]["code"] == "key_bound_to_another_device"
            accepted: Final = rig.chat(key, f"device-{index}")
            assert accepted.status_code == 200, accepted.text
            assert accepted.headers["x-litellm-model-id"] == expected[index]
        limited_key: Final = str(
            rig.proxy.post("/key/generate", {"models": list(rig.config.models), "rpm_limit": 1})["key"]
        )
        try:
            assert rig.chat(limited_key, "limited-device").status_code == 200
            limited: Final = rig.chat(limited_key, "limited-device")
            assert limited.status_code == 429, limited.text
            assert limited.json()["error"]["code"] == "client_rate_limit_exceeded", limited.text
            assert float(limited.headers.get("retry-after", "0")) > 0
        finally:
            rig.proxy.post("/key/delete", {"keys": [limited_key]})
    finally:
        rig.proxy.post("/key/delete", {"keys": list(keys)})


def test_cancelled_client_stream_does_not_release_an_upstream_that_is_still_running(rig: Rig) -> None:
    keys: Final = tuple(
        str(rig.proxy.post("/key/generate", {"models": list(rig.config.models)})["key"]) for _ in range(2)
    )
    try:
        with rig.proxy.client.stream(
            "POST",
            "/v1/chat/completions",
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
        conflict: Final = rig.chat(keys[0], "other-device")
        assert conflict.status_code == 409, conflict.text
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


def test_two_workers_resolve_first_device_race_and_fail_closed_after_state_loss(rig: Rig) -> None:
    key: Final = str(rig.proxy.post("/key/generate", {"models": list(rig.config.models)})["key"])
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            replies: Final = tuple(executor.map(lambda device: rig.chat(key, device), ("left", "right")))
        assert sorted(reply.status_code for reply in replies) == [200, 409], tuple(reply.text for reply in replies)
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
        conflict: Final = rig.chat(keys[2], "another-device")
        assert conflict.status_code == 409, conflict.text
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
                lambda: rig.chat(keys[0], "device", model=f"{rig.config.models[0]}-{model}"),
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
        conflict: Final = rig.chat(keys[0], "other-device", model=public_models[2])
        assert conflict.status_code == 409, conflict.text
    finally:
        rig.proxy.post("/key/delete", {"keys": list(keys)})
        for resource in rig.config.resources:
            for model in aliases:
                rig.proxy.post("/model/delete", {"id": f"{resource.id}-{model}"})
        redis.close()
