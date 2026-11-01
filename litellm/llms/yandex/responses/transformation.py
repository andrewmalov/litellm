"""
Yandex Cloud Responses API configuration.

API Docs: https://cloud.yandex.com/en/docs/yandexgpt/api-ref/grpc
"""

import re
from collections.abc import Mapping
from json import JSONDecodeError
from typing import Final

import httpx
from pydantic import TypeAdapter, ValidationError

from litellm.litellm_core_utils.litellm_logging import Logging as LiteLLMLoggingObj
from litellm.llms.openai.common_utils import OpenAIError
from litellm.llms.openai.responses.transformation import OpenAIResponsesAPIConfig
from litellm.secret_managers.main import get_secret_str
from litellm.types.llms.openai import (
    ResponseInputParam,
    ResponsesAPIOptionalRequestParams,
    ResponsesAPIResponse,
)
from litellm.types.router import GenericLiteLLMParams
from litellm.types.utils import LlmProviders

_JSON_OBJECT: Final = TypeAdapter(Mapping[str, object])


class YandexResponsesConfig(OpenAIResponsesAPIConfig):
    """
    Yandex Cloud Responses API config.

    Key differences from OpenAI:
    - Auth via Api-Key header (not Bearer)
    - Requires OpenAI-Project header with folder ID
    - model format: gpt://<folder_id>/<model_name>/latest
    - supports max_output_tokens (not max_tokens/max_completion_tokens)
    - instructions maps to system prompt
    """

    YANDEX_API_BASE = "https://ai.api.cloud.yandex.net/v1"
    _FOLDER_ID_PATTERN = re.compile(r"^gpt://([^/]+)/")

    def get_supported_openai_params(
        self, model: str
    ) -> list[str]:  # mutable-ok: inherited interface requires a list return
        return [
            "input",
            "model",
            "instructions",
            "max_output_tokens",
            "temperature",
            "top_p",
            "stream",
            "timeout",
        ]

    def map_openai_params(
        self,
        response_api_optional_params: ResponsesAPIOptionalRequestParams,
        model: str,
        drop_params: bool,
    ) -> dict[str, object]:  # mutable-ok: inherited interface requires a dict return
        return dict(response_api_optional_params)

    @property
    def custom_llm_provider(self) -> LlmProviders:
        return LlmProviders.YANDEX

    def validate_environment(
        self,
        headers: Mapping[str, object],
        model: str,
        litellm_params: GenericLiteLLMParams | None,
    ) -> dict[str, object]:  # mutable-ok: inherited interface requires a dict return
        params: Final = litellm_params or GenericLiteLLMParams()
        api_key: Final = params.api_key or get_secret_str("YANDEX_API_KEY")
        openai_project: Final = get_secret_str("YANDEX_OPENAI_PROJECT") or self._extract_folder_id(model)
        return {
            **headers,
            **({"Authorization": f"Api-Key {api_key}"} if api_key else {}),
            **({"OpenAI-Project": openai_project} if openai_project else {}),
            "Content-Type": "application/json",
        }

    def _extract_folder_id(self, model: str) -> str | None:
        match: Final = self._FOLDER_ID_PATTERN.match(model)
        if match:
            return match.group(1)
        return None

    def get_complete_url(self, api_base: str | None, litellm_params: Mapping[str, object]) -> str:
        resolved_base: Final = api_base or get_secret_str("YANDEX_API_BASE") or self.YANDEX_API_BASE
        return f"{resolved_base.rstrip('/')}/responses"

    def transform_responses_api_request(
        self,
        model: str,
        input: str | ResponseInputParam,
        response_api_optional_request_params: Mapping[str, object],
        litellm_params: GenericLiteLLMParams,
        headers: Mapping[str, object],
    ) -> dict[str, object]:  # mutable-ok: inherited interface requires a dict return
        """Build Yandex-specific request body."""
        return {
            "model": model,
            "input": input,
            **{
                key: value
                for key, value in response_api_optional_request_params.items()
                if key != "instructions" or value
            },
        }

    def transform_response_api_response(
        self,
        model: str,
        raw_response: httpx.Response,
        logging_obj: LiteLLMLoggingObj,
    ) -> ResponsesAPIResponse:
        """Parse Yandex response into standard ResponsesAPIResponse format."""
        logging_obj.post_call(  # pyright: ignore[reportUnknownMemberType]  # shared logger parameters are untyped
            original_response=raw_response.text,
            additional_args={"complete_input_dict": {}},
        )
        try:
            raw_response_json: Final = _JSON_OBJECT.validate_python(raw_response.json())
        except (JSONDecodeError, ValidationError) as error:
            raise OpenAIError(
                message=f"Error parsing Yandex response: {raw_response.text}, status_code: {raw_response.status_code}",
                status_code=raw_response.status_code,
            ) from error

        return self._parse_yandex_response(raw_response_json)

    def _parse_yandex_response(self, raw_response_json: Mapping[str, object]) -> ResponsesAPIResponse:
        """Transform Yandex-specific response format to OpenAI Responses API format."""
        yandex_response: Final = _JSON_OBJECT.validate_python(raw_response_json.get("response", {}))
        result_text: Final = yandex_response.get("result")
        usage: Final = _JSON_OBJECT.validate_python(yandex_response.get("usage", {}))
        normalized_response: Final = {
            "id": raw_response_json.get("response_id", ""),
            "status": "completed",
            "created_at": raw_response_json.get("created_at", ""),
            "model": raw_response_json.get("model", ""),
            "output": (
                [
                    {
                        "type": "message",
                        "id": raw_response_json.get("response_id", ""),
                        "status": "completed",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": result_text}],
                    }
                ]
                if isinstance(result_text, str)
                else []
            ),
            "usage": {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
        }
        try:
            return ResponsesAPIResponse.model_validate(normalized_response)
        except ValidationError:
            return ResponsesAPIResponse.model_construct(_fields_set=None, **normalized_response)

    def supports_native_websocket(self) -> bool:
        """Yandex does not support native WebSocket for Responses API"""
        return False
