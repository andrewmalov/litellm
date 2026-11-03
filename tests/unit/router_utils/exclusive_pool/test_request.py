from typing import Final

import pytest

from litellm.router_utils.exclusive_pool.request import PoolProxyException, RequestView, Ticket, verified_ticket
from litellm.router_utils.exclusive_pool.models import Failure


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
    assert body.reserved_tokens(12) >= len("hello".encode()) + 12
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
