from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Annotated, Final

from pydantic import Field, JsonValue, TypeAdapter, ValidationError

from litellm.exceptions import AuthenticationError
from litellm.llms.anthropic.common_utils import AnthropicError
from litellm.llms.anthropic.count_tokens.handler import AnthropicCountTokensHandler
from litellm.llms.anthropic.pass_through.messages.transformation import AnthropicMessagesConfig

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


def has_media(value: JsonValue) -> bool:
    if isinstance(value, list):
        return any(has_media(item) for item in value)
    if not isinstance(value, dict):
        return False
    return value.get("type") in ("image", "document", "container_upload") or (
        value.get("type") == "tool_result" and has_media(value.get("content"))
    )


class _Count(FrozenModel):
    input_tokens: Annotated[int, Field(ge=0, strict=True)]


class _Parameters(FrozenModel):
    model: str
    api_key: str | None = Field(default=None, repr=False)
    api_base: str | None = None
    extra_headers: Mapping[str, str] = Field(default_factory=dict)


class _Deployment(FrozenModel):
    litellm_params: Mapping[str, object] = Field(repr=False)


_ENVIRONMENT: Final = TypeAdapter(tuple[Mapping[str, str], str | None])
_OPTIONS: Final = TypeAdapter(Mapping[str, JsonValue])


def count_unavailable() -> Failure:
    return Failure(
        code="pool_token_count_unavailable",
        status=503,
        message="This upstream must support Anthropic Count Tokens for pool attachments and server references",
    )


async def count_input_tokens(request: RequestView, deployment: Mapping[str, object]) -> int | Failure:
    try:
        configured: Final = _Deployment.model_validate(deployment).litellm_params
        parameters: Final = _Parameters.model_validate(configured)
        options: Final = _OPTIONS.validate_python(
            {
                key: value
                for key, value in (
                    ("tools", [dict(tool) for tool in request.tools] or None),
                    ("container", request.container),
                    ("tool_choice", request.tool_choice),
                    ("thinking", request.thinking),
                    ("output_config", dict(request.output_config) if request.output_config is not None else None),
                )
                if value is not None
            }
        )
        api: Final = AnthropicMessagesConfig()
        headers, _ = _ENVIRONMENT.validate_python(
            await api.avalidate_anthropic_messages_environment(  # pyright: ignore[reportUnknownMemberType]  # untyped
                headers={
                    **parameters.extra_headers,
                    **(
                        {"anthropic-beta": request.proxy_server_request.headers["anthropic-beta"]}
                        if request.proxy_server_request is not None
                        and "anthropic-beta" in request.proxy_server_request.headers
                        else {}
                    ),
                },
                model=parameters.model.split("/", 1)[-1],
                messages=[dict(message) for message in request.messages],
                optional_params=dict(options),
                litellm_params=dict(configured),
                api_key=parameters.api_key,
                api_base=parameters.api_base,
            )
        )
        response: Final = await AnthropicCountTokensHandler().handle_count_tokens_request(
            model=parameters.model.split("/", 1)[-1],
            messages=[dict(message) for message in request.messages],
            auth_header=headers,
            api_base=parameters.api_base,
            timeout=10,
            tools=[dict(tool) for tool in request.tools] or None,
            system=request.system,
            optional_params=options,
        )
        return _Count.model_validate(response).input_tokens
    except (AnthropicError, AuthenticationError, ValidationError):
        return count_unavailable()
