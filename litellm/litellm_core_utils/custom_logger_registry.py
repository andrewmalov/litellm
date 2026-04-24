"""
Registry mapping the callback class string to the class type.

This is used to get the class type from the callback class string.

Example:
    "datadog" -> DataDogLogger
    "prometheus" -> PrometheusLogger
"""

from typing import Union

from litellm import _custom_logger_compatible_callbacks_literal
from litellm.integrations.agentops import AgentOps
from litellm.integrations.anthropic_cache_control_hook import AnthropicCacheControlHook
from litellm.integrations.argilla import ArgillaLogger
from litellm.integrations.azure_sentinel.azure_sentinel import AzureSentinelLogger
from litellm.integrations.azure_storage.azure_storage import AzureBlobStorageLogger
from litellm.integrations.bitbucket import BitBucketPromptManager
from litellm.integrations.braintrust_logging import BraintrustLogger
from litellm.integrations.cloudzero.cloudzero import CloudZeroLogger
from litellm.integrations.datadog.datadog import DataDogLogger
from litellm.integrations.datadog.datadog_llm_obs import DataDogLLMObsLogger
from litellm.integrations.datadog.datadog_metrics import DatadogMetricsLogger
from litellm.integrations.deepeval import DeepEvalLogger
from litellm.integrations.dotprompt import DotpromptManager
from litellm.integrations.focus.focus_logger import FocusLogger
from litellm.integrations.mavvrik_focus.mavvrik_focus_logger import MavvrikFocusLogger
from litellm.integrations.vantage.vantage_logger import VantageLogger
from litellm.integrations.galileo import GalileoObserve
from litellm.integrations.gcs_bucket.gcs_bucket import GCSBucketLogger
from litellm.integrations.gcs_pubsub.pub_sub import GcsPubSubLogger
from litellm.integrations.gitlab import GitLabPromptManager
from litellm.integrations.humanloop import HumanloopLogger
from litellm.integrations.lago import LagoLogger
from litellm.integrations.langfuse.langfuse_prompt_management import (
    LangfusePromptManagement,
)
from litellm.integrations.langsmith import LangsmithLogger
from litellm.integrations.litellm_agent import LiteLLMAgentModelResolver
from litellm.integrations.literal_ai import LiteralAILogger
from litellm.integrations.mlflow import MlflowLogger
from litellm.integrations.newrelic import NewRelicLogger
from litellm.integrations.openmeter import OpenMeterLogger
from litellm.integrations.opentelemetry import OpenTelemetry
from litellm.integrations.opik.opik import OpikLogger
from litellm.integrations.posthog import PostHogLogger
from litellm.integrations.prometheus import PrometheusLogger
from litellm.integrations.s3_v2 import S3Logger
from litellm.integrations.sqs import SQSLogger
from litellm.integrations.vector_store_integrations.vector_store_pre_call_hook import (
    VectorStorePreCallHook,
)
from litellm.proxy.hooks.dynamic_rate_limiter import _PROXY_DynamicRateLimitHandler
from litellm.proxy.hooks.dynamic_rate_limiter_v3 import _PROXY_DynamicRateLimitHandlerV3


def _should_disable_external_integrations() -> bool:
    """Check if external integrations should be disabled."""
    import os
    return os.environ.get("LITELLM_DISABLE_EXTERNAL_INTEGRATIONS", "false").lower() == "true"


class CustomLoggerRegistry:
    """
    Registry mapping the callback class string to the class type.
    """

    # Check if external integrations are disabled at module load time
    _integrations_disabled = _should_disable_external_integrations()

    if _integrations_disabled:
        # When disabled, only allow internal/critical callbacks
        CALLBACK_CLASS_STR_TO_CLASS_TYPE: dict = {}
    else:
        CALLBACK_CLASS_STR_TO_CLASS_TYPE = {
            "lago": LagoLogger,
            "openmeter": OpenMeterLogger,
            "braintrust": BraintrustLogger,
            "galileo": GalileoObserve,
            "langsmith": LangsmithLogger,
            "literalai": LiteralAILogger,
            "litellm_agent": LiteLLMAgentModelResolver,
            "prometheus": PrometheusLogger,
            "datadog": DataDogLogger,
            "datadog_llm_observability": DataDogLLMObsLogger,
            "datadog_metrics": DatadogMetricsLogger,
            "gcs_bucket": GCSBucketLogger,
            "opik": OpikLogger,
            "argilla": ArgillaLogger,
            "opentelemetry": OpenTelemetry,
            "azure_sentinel": AzureSentinelLogger,
            "azure_storage": AzureBlobStorageLogger,
            "humanloop": HumanloopLogger,
            # OTEL compatible loggers
            "logfire": OpenTelemetry,
            "arize": OpenTelemetry,
            "langfuse_otel": OpenTelemetry,
            "arize_phoenix": OpenTelemetry,
            "langtrace": OpenTelemetry,
            "weave_otel": OpenTelemetry,
            "levo": OpenTelemetry,
            "mlflow": MlflowLogger,
            "langfuse": LangfusePromptManagement,
            "otel": OpenTelemetry,
            "gcs_pubsub": GcsPubSubLogger,
            "anthropic_cache_control_hook": AnthropicCacheControlHook,
            "agentops": AgentOps,
            "deepeval": DeepEvalLogger,
            "s3_v2": S3Logger,
            "aws_sqs": SQSLogger,
            "dynamic_rate_limiter": _PROXY_DynamicRateLimitHandler,
            "dynamic_rate_limiter_v3": _PROXY_DynamicRateLimitHandlerV3,
            "vector_store_pre_call_hook": VectorStorePreCallHook,
            "dotprompt": DotpromptManager,
            "bitbucket": BitBucketPromptManager,
            "gitlab": GitLabPromptManager,
            "cloudzero": CloudZeroLogger,
            "focus": FocusLogger,
            "mavvrik": MavvrikFocusLogger,
            "vantage": VantageLogger,
            "posthog": PostHogLogger,
            "newrelic": NewRelicLogger,
        }
