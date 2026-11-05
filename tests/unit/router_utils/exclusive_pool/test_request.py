from typing import Final

import pytest

from litellm.router_utils.exclusive_pool.models import Failure
from litellm.router_utils.exclusive_pool.request import PoolProxyException, RequestView, Ticket, verified_ticket


def test_pool_error_preserves_http_status_and_machine_readable_body_code() -> None:
    error: Final = PoolProxyException(Failure(code="endpoint_pool_exhausted", status=503, message="unavailable"))
    assert error.code == "503"
    assert error.to_dict()["code"] == "endpoint_pool_exhausted"


def test_ticket_cannot_be_forged_or_moved_to_another_operation() -> None:
    ticket: Final = Ticket(operation="one", client="c", device="d", resource="a")
    signed: Final = ticket.sign(b"worker-secret")
    assert verified_ticket(signed, b"worker-secret") == ticket
    assert verified_ticket(signed, b"other-worker") is None
    assert (
        verified_ticket(
            {"ticket": ticket.model_copy(update={"operation": "two"}).model_dump(), "signature": signed.signature},
            b"worker-secret",
        )
        is None
    )


def test_reservation_covers_text_and_output_without_storing_content() -> None:
    body: Final = RequestView(model="alias", messages=({"role": "user", "content": "hello"},), max_tokens=12)
    assert body.reserved_tokens(12) >= len(b"hello") + 12
    assert body.output_limit(256) == 12
    assert body.model_copy(update={"max_tokens": None}).output_limit(256) == 256


@pytest.mark.parametrize(
    "message", [{"role": "user", "content": [{"type": "image_url"}]}, {"role": "user", "content": 42}]
)
def test_unsupported_content_is_rejected_before_admission(message: dict[str, object]) -> None:
    assert not RequestView(model="alias", messages=(message,)).text_only


@pytest.mark.parametrize(
    "overrides",
    [
        {"api_key": "sk-client-override"},
        {"api_base": "https://other.invalid"},
        {"extra_headers": {"Authorization": "Bearer other"}},
        {"specific_deployment": True},
        {"mock_response": "fake"},
    ],
)
def test_pool_requests_cannot_override_the_configured_upstream(overrides: dict[str, object]) -> None:
    from litellm.router_utils.exclusive_pool.request import has_upstream_override

    assert has_upstream_override(overrides)
    assert not has_upstream_override({"model": "public-alias", "max_tokens": 16})


def test_stream_eof_alone_does_not_prove_upstream_completion() -> None:
    from litellm import ModelResponseStream
    from litellm.router_utils.exclusive_pool.request import StreamProgress

    partial: Final = ModelResponseStream(choices=[{"index": 0, "delta": {"content": "partial"}, "finish_reason": None}])
    terminal: Final = ModelResponseStream(
        choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}],
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    )
    unfinished: Final = StreamProgress().feed(partial)
    assert not unfinished.terminal
    finished: Final = unfinished.feed(terminal)
    assert finished.terminal
    assert finished.tokens == 2


def test_multiple_choices_cannot_bypass_output_reservation() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RequestView(model="alias", messages=({"role": "user", "content": "hello"},), n=100)


def test_legacy_functions_and_response_schema_are_included_in_input_reservation() -> None:
    base: Final = RequestView(model="alias", messages=({"role": "user", "content": "hello"},))
    legacy: Final = RequestView.model_validate(
        {
            **base.model_dump(),
            "functions": [{"name": "f", "description": "x" * 5000}],
            "response_format": {"type": "json_schema", "schema": {"description": "y" * 5000}},
        }
    )
    assert legacy.reserved_tokens(16) >= base.reserved_tokens(16) + 10000


def test_router_stamped_deployment_identity_takes_precedence_over_caller_metadata() -> None:
    from litellm.router_utils.exclusive_pool.request import MetadataCarriers

    carriers: Final = MetadataCarriers.model_validate(
        {
            "model_info": {"id": "actual", "exclusive_pool": "main"},
            "metadata": {"model_info": {"id": "fake"}},
            "litellm_metadata": {"model_info": {"id": "another-fake"}},
        }
    )
    assert carriers.deployment == "actual"
    assert carriers.pool == "main"


def test_malformed_pool_label_on_another_model_does_not_break_model_classification() -> None:
    from litellm.router_utils.exclusive_pool.request import marked_model

    deployments: Final = (
        {"model_name": "bad", "model_info": {"id": "bad-id", "exclusive_pool": 123}},
        {"model_name": "ordinary", "model_info": {"id": "ordinary-id"}},
    )
    assert not marked_model("ordinary", deployments)
    assert marked_model("bad", deployments)
    assert marked_model("bad-id", deployments)


@pytest.mark.parametrize("call_type", ["anthropic_messages", "aanthropic_messages"])
def test_anthropic_context_blocks_are_admitted_without_changing_chat_restrictions(call_type: str) -> None:
    request: Final = RequestView.model_validate(
        {
            "model": "alias",
            "system": [{"type": "text", "text": "system prompt"}],
            "messages": [
                {"role": "assistant", "content": [{"type": "tool_use", "id": "tool-1", "name": "lookup", "input": {}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "result"}]},
            ],
        }
    )
    assert request.supports(call_type)
    assert not request.supports("acompletion")
    assert not request.supports("aresponses")
    assert not request.model_copy(update={"previous_response_id": "server-state"}).supports(call_type)


def test_anthropic_system_prompt_is_included_in_token_reservation() -> None:
    base: Final = {"model": "alias", "messages": [{"role": "user", "content": "hi"}]}
    short: Final = RequestView.model_validate(base)
    long: Final = RequestView.model_validate({**base, "system": [{"type": "text", "text": "s" * 5000}]})
    assert long.reserved_tokens(16) >= short.reserved_tokens(16) + 5000


def test_anthropic_response_usage_counts_cached_input_as_well_as_output() -> None:
    from litellm.router_utils.exclusive_pool.request import actual_usage

    assert (
        actual_usage(
            {
                "usage": {
                    "input_tokens": 11,
                    "output_tokens": 7,
                    "cache_creation_input_tokens": 13,
                    "cache_read_input_tokens": 17,
                }
            }
        )
        == 48
    )


@pytest.mark.parametrize("wire", [False, True])
def test_anthropic_stream_requires_message_stop_and_merges_usage(wire: bool) -> None:
    import json

    from litellm.router_utils.exclusive_pool.request import StreamProgress

    start: Final = {
        "type": "message_start",
        "message": {
            "usage": {
                "input_tokens": 11,
                "output_tokens": 0,
                "cache_creation_input_tokens": 13,
                "cache_read_input_tokens": 17,
            }
        },
    }
    delta: Final = {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 7}}
    stop: Final = {"type": "message_stop"}
    progress: Final = StreamProgress().feed(json.dumps(start).join(("data: ", "\n\n")).encode() if wire else start)
    ended: Final = progress.feed(json.dumps(delta).join(("data: ", "\n\n")).encode() if wire else delta)
    assert not ended.terminal
    assert ended.tokens == 48
    finished: Final = ended.feed(b'data: {"type":"message_stop"}\n\n' if wire else stop)
    assert finished.terminal
    assert finished.tokens == 48


def test_anthropic_stream_handles_fragmented_utf8_and_coalesced_events() -> None:
    from litellm.router_utils.exclusive_pool.request import StreamProgress

    frame: Final = 'data: {"type":"content_block_delta","delta":{"text":"привет"}}\r\n\r\n'.encode()
    cut: Final = frame.index("п".encode()) + 1
    partial: Final = StreamProgress().feed(frame[:cut])
    assert not partial.terminal
    done: Final = partial.feed(frame[cut:] + b'data: {"type":"message_stop"}\r\n\r\n')
    assert done.terminal
    assert not StreamProgress().feed(b'data: {"type":"message_stop"}').terminal


def test_chat_ticket_is_not_valid_for_nested_anthropic_adapter_calls() -> None:
    from litellm.types.utils import CallTypes

    chat: Final = Ticket(operation="one", client="c", device="d", resource="a")
    native: Final = chat.model_copy(update={"anthropic": True})
    assert not chat.is_adapter_call(CallTypes.acompletion)
    assert native.is_adapter_call(CallTypes.acompletion)
    assert native.is_adapter_call(CallTypes.aresponses)
    assert not native.is_adapter_call(CallTypes.anthropic_messages)


@pytest.mark.parametrize(
    "content",
    [
        [{"type": "container_upload", "file_id": "file-1"}],
        [{"type": "document", "source": {"type": "file", "file_id": "file-1"}}],
        [{"type": "image", "source": {"type": "url", "url": "https://example.invalid/image"}}],
        [
            {
                "type": "tool_result",
                "tool_use_id": "tool-1",
                "content": [
                    {"type": "document", "source": {"type": "url", "url": "https://example.invalid/document"}},
                ],
            }
        ],
    ],
)
def test_anthropic_nonportable_and_unreserved_media_are_rejected(content: list[dict[str, object]]) -> None:
    request: Final = RequestView.model_validate({"model": "alias", "messages": [{"role": "user", "content": content}]})
    assert not request.supports("anthropic_messages")


def test_anthropic_container_reference_is_rejected() -> None:
    request: Final = RequestView.model_validate(
        {
            "model": "alias",
            "messages": [{"role": "user", "content": "hello"}],
            "container": "container-1",
        }
    )
    assert not request.supports("anthropic_messages")


def test_anthropic_empty_tool_result_content_is_portable() -> None:
    request: Final = RequestView.model_validate(
        {
            "model": "alias",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "tool-1", "content": []},
                    ],
                }
            ],
        }
    )
    assert request.supports("anthropic_messages")


@pytest.mark.parametrize("field", ["output_config", "output_format"])
def test_anthropic_output_schema_is_included_in_input_reservation(field: str) -> None:
    base: Final = {"model": "alias", "messages": [{"role": "user", "content": "hello"}]}
    plain: Final = RequestView.model_validate(base)
    schema: Final = RequestView.model_validate({**base, field: {"schema": {"description": "x" * 5000}}})
    assert schema.reserved_tokens(16) >= plain.reserved_tokens(16) + 5000


def test_anthropic_error_event_cannot_be_followed_by_successful_completion() -> None:
    from litellm.router_utils.exclusive_pool.request import StreamProgress

    failed: Final = StreamProgress().feed({"type": "error", "error": {"type": "overloaded_error"}})
    assert not failed.feed({"type": "message_stop"}).terminal
