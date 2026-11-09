from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Final

from pydantic import Field, JsonValue, ValidationError

from litellm._lazy_imports import get_default_encoding
from litellm.constants import DEFAULT_IMAGE_TOKEN_COUNT
from litellm.litellm_core_utils.token_counter import offload_token_count, token_counter
from litellm.llms.anthropic.pass_through.messages.utils import anthropic_system_to_openai_message
from litellm.rust_bridge.tokenizer import anthropic
from litellm.types.utils import SelectTokenizerResponse
from litellm.utils import huggingface_tokenizer_kind

from .models import Failure, FrozenModel

if TYPE_CHECKING:
    from .request import RequestView


def client_tool(kind: JsonValue) -> bool:
    return kind in (None, "custom", "function") or (
        isinstance(kind, str) and kind.startswith(("computer_", "bash_", "text_editor_", "memory_"))
    )


def has_reference(value: JsonValue) -> bool:
    if isinstance(value, list):
        return any(has_reference(item) for item in value)
    if not isinstance(value, dict):
        return False
    kind: Final = value.get("type")
    if kind in ("container_upload", "server_tool_use") or (isinstance(kind, str) and kind.endswith("_tool_result")):
        return True
    if kind == "tool_result":
        return has_reference(value.get("content"))
    source: Final = value.get("source")
    return (
        kind in ("image", "document")
        and isinstance(source, dict)
        and (source.get("type") == "file" or has_reference(source.get("content")))
    )


class _Parameters(FrozenModel):
    model: str


class _Deployment(FrozenModel):
    litellm_params: _Parameters = Field(repr=False)


def count_unavailable() -> Failure:
    return Failure(
        code="pool_token_count_unavailable",
        status=503,
        message="Local input token estimation is unavailable",
    )


def estimate_input_tokens(request: RequestView, deployment: Mapping[str, object]) -> int | Failure:
    try:
        model: Final = _Deployment.model_validate(deployment).litellm_params.model.split("/", 1)[-1]
        tokenizer: Final[SelectTokenizerResponse] = (
            {"type": "huggingface_tokenizer", "tokenizer": anthropic()}
            if huggingface_tokenizer_kind(model) == "anthropic"
            else {"type": "openai_tokenizer", "tokenizer": get_default_encoding()}
        )
        system: Final = anthropic_system_to_openai_message(request.system)
        messages: Final = (*((system,) if system is not None else ()), *request.messages)
        context_tokens: Final = token_counter(
            model=model,
            messages=messages,
            custom_tokenizer=tokenizer,
            use_default_image_token_count=True,
            default_token_count=DEFAULT_IMAGE_TOKEN_COUNT,
        )
        options: Final = {
            key: value
            for key, value in (
                ("tools", request.tools or None),
                ("functions", request.functions or None),
                ("response_format", request.response_format),
                ("output_config", request.output_config),
                ("output_format", request.output_format),
                ("tool_choice", request.tool_choice),
                ("function_call", request.function_call),
            )
            if value is not None
        }
        extra_tokens: Final = (
            token_counter(model=model, custom_tokenizer=tokenizer, text=json.dumps(options, ensure_ascii=False))
            if options
            else 0
        )
        return context_tokens + extra_tokens if context_tokens > 0 else count_unavailable()
    except (ValidationError, ValueError, TypeError):
        return count_unavailable()


async def count_input_tokens(request: RequestView, deployment: Mapping[str, object]) -> int | Failure:
    return await offload_token_count(estimate_input_tokens)(request, deployment)
