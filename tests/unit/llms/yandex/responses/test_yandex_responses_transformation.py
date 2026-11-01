from typing import Final

import pytest

import litellm
from litellm.llms.yandex.responses.transformation import YandexResponsesConfig
from litellm.types.router import GenericLiteLLMParams
from litellm.types.utils import LlmProviders
from litellm.utils import ProviderConfigManager


def test_yandex_responses_config_is_resolved_through_public_registry() -> None:
    config: Final = ProviderConfigManager.get_provider_responses_api_config(
        provider=LlmProviders.YANDEX, model="gpt://test-folder/aliceai-llm/latest"
    )
    assert isinstance(config, YandexResponsesConfig)
    assert litellm.YandexResponsesConfig is YandexResponsesConfig
    assert config.custom_llm_provider == LlmProviders.YANDEX


def test_yandex_headers_preserve_caller_input_and_apply_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YANDEX_OPENAI_PROJECT", "configured-project")
    headers: Final = {"X-Request-ID": "request-id", "Authorization": "old-value"}
    result: Final = YandexResponsesConfig().validate_environment(
        headers=headers,
        model="gpt://model-folder/aliceai-llm/latest",
        litellm_params=GenericLiteLLMParams(api_key="test-key"),
    )
    assert result == {
        "X-Request-ID": "request-id",
        "Authorization": "Api-Key test-key",
        "OpenAI-Project": "configured-project",
        "Content-Type": "application/json",
    }
    assert headers == {"X-Request-ID": "request-id", "Authorization": "old-value"}


@pytest.mark.parametrize("instructions", ["Answer briefly", "", None])
def test_yandex_request_preserves_optional_params(instructions: str | None) -> None:
    params: Final = {"instructions": instructions, "max_output_tokens": 37, "temperature": 0.4}
    result: Final = YandexResponsesConfig().transform_responses_api_request(
        model="gpt://test-folder/aliceai-llm/latest",
        input="Hello",
        response_api_optional_request_params=params,
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )
    assert result == {
        "model": "gpt://test-folder/aliceai-llm/latest",
        "input": "Hello",
        "max_output_tokens": 37,
        "temperature": 0.4,
        **({"instructions": instructions} if instructions else {}),
    }
    assert params == {"instructions": instructions, "max_output_tokens": 37, "temperature": 0.4}


@pytest.mark.parametrize("created_at", [123456, ""])
def test_yandex_response_preserves_text_usage_and_legacy_timestamp(created_at: int | str) -> None:
    payload: Final = {
        "response_id": "response-id",
        "created_at": created_at,
        "model": "response-model",
        "response": {
            "result": "Generated answer",
            "usage": {"input_tokens": 3, "output_tokens": 5, "total_tokens": 8},
        },
    }
    response: Final = YandexResponsesConfig()._parse_yandex_response(payload)
    normalized: Final = response.model_dump()
    assert normalized["id"] == payload["response_id"]
    assert normalized["model"] == payload["model"]
    assert normalized["created_at"] == created_at
    assert normalized["status"] == "completed"
    assert normalized["output"] == [
        {
            "type": "message",
            "id": "response-id",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Generated answer"}],
        }
    ]
    assert normalized["usage"]["input_tokens"] == 3
    assert normalized["usage"]["output_tokens"] == 5
    assert normalized["usage"]["total_tokens"] == 8
