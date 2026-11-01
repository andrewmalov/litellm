"""
Yandex Cloud Responses API configuration.

API Docs: https://cloud.yandex.com/en/docs/yandexgpt/api-ref/grpc
"""

import re
from typing import Any

import httpx

from litellm.litellm_core_utils.litellm_logging import Logging as LiteLLMLoggingObj
from litellm.llms.openai.responses.transformation import OpenAIResponsesAPIConfig
from litellm.secret_managers.main import get_secret_str
from litellm.types.llms.openai import (
    ResponseInputParam,
    ResponsesAPIOptionalRequestParams,
    ResponsesAPIResponse,
)
from litellm.types.router import GenericLiteLLMParams
from litellm.types.utils import LlmProviders


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

    def get_supported_openai_params(self, model: str) -> list:
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
    ) -> dict:
        return dict(response_api_optional_params)

    @property
    def custom_llm_provider(self) -> LlmProviders:
        return LlmProviders.YANDEX

    def validate_environment(self, headers: dict, model: str, litellm_params: GenericLiteLLMParams | None) -> dict:
        litellm_params = litellm_params or GenericLiteLLMParams()
        api_key = litellm_params.api_key or get_secret_str("YANDEX_API_KEY")
        if api_key:
            headers["Authorization"] = f"Api-Key {api_key}"

        openai_project = get_secret_str("YANDEX_OPENAI_PROJECT")
        if openai_project:
            headers["OpenAI-Project"] = openai_project
        else:
            folder_id = self._extract_folder_id(model)
            if folder_id:
                headers["OpenAI-Project"] = folder_id

        headers["Content-Type"] = "application/json"
        return headers

    def _extract_folder_id(self, model: str) -> str | None:
        match = self._FOLDER_ID_PATTERN.match(model)
        if match:
            return match.group(1)
        return None

    def get_complete_url(self, api_base: str | None, litellm_params: dict) -> str:
        api_base = api_base or get_secret_str("YANDEX_API_BASE") or self.YANDEX_API_BASE
        return f"{api_base.rstrip('/')}/responses"

    def transform_responses_api_request(
        self,
        model: str,
        input: str | ResponseInputParam,
        response_api_optional_request_params: dict,
        litellm_params: GenericLiteLLMParams,
        headers: dict,
    ) -> dict:
        """Build Yandex-specific request body."""
        instructions = response_api_optional_request_params.pop("instructions", None)
        data: dict[str, Any] = {
            "model": model,
            "input": input,
        }
        if instructions:
            data["instructions"] = instructions

        data.update(response_api_optional_request_params)
        return data

    def transform_response_api_response(
        self,
        model: str,
        raw_response: httpx.Response,
        logging_obj: LiteLLMLoggingObj,
    ) -> ResponsesAPIResponse:
        """Parse Yandex response into standard ResponsesAPIResponse format."""
        try:
            logging_obj.post_call(
                original_response=raw_response.text,
                additional_args={"complete_input_dict": {}},
            )
            raw_response_json = raw_response.json()
        except Exception:
            raise Exception(
                f"Error parsing Yandex response: {raw_response.text}, status_code: {raw_response.status_code}"
            )

        return self._parse_yandex_response(raw_response_json)

    def _parse_yandex_response(self, raw_response_json: dict) -> ResponsesAPIResponse:
        """Transform Yandex-specific response format to OpenAI Responses API format."""
        yandex_response = raw_response_json.get("response", {})
        raw_response_json.get("usage", {})

        output_items: list[dict[str, Any]] = []
        if "result" in yandex_response:
            result_text = yandex_response.get("result", {})
            if isinstance(result_text, str):
                output_items.append(
                    {
                        "type": "message",
                        "id": raw_response_json.get("response_id", ""),
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": result_text,
                            }
                        ],
                    }
                )

        usage = yandex_response.get("usage", {})
        normalized_usage = {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        }

        normalized_response = {
            "id": raw_response_json.get("response_id", ""),
            "status": "completed",
            "created_at": raw_response_json.get("created_at", ""),
            "model": raw_response_json.get("model", ""),
            "output": output_items,
            "usage": normalized_usage,
        }

        try:
            return ResponsesAPIResponse(**normalized_response)
        except Exception:
            return ResponsesAPIResponse.model_construct(**normalized_response)

    def supports_native_websocket(self) -> bool:
        """Yandex does not support native WebSocket for Responses API"""
        return False
