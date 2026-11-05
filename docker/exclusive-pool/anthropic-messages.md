# Anthropic Messages in the exclusive pool

Claude Desktop can send `POST /v1/messages?beta=true` to the proxy using the same virtual key and public model names as Chat Completions. Both endpoints share device binding, physical upstream assignments, fallback, key budgets and pool admission limits

The pool accepts text, text content blocks, client `tool_use` / `tool_result` history, and thinking blocks. Send the complete conversation on each request. Images, documents, provider file IDs, server tool results and container references are rejected with HTTP 400 before admission. Their media token reservations or account-specific state cannot safely use the current portable text pool

Keep a stable `metadata.user_id` containing JSON with a `device_id`, or configure and send the pool's `device_header`. Reusing a key from a different device still returns HTTP 409

For an OpenAI-compatible upstream serving Chat Completions, keep its `openai/` provider prefix and API base ending in `/v1`. Add this setting to the deployed proxy YAML, as included in the Compose template:

```yaml
litellm_settings:
  use_chat_completions_url_for_anthropic_messages: true
  callbacks:
    - litellm.router_utils.exclusive_pool.configured.callback
```

Without this setting, LiteLLM uses the Responses API when adapting Anthropic requests for OpenAI providers. Upstreams that serve only Chat Completions will reject that route

For an Anthropic-native upstream, use the `anthropic/` provider prefix and the API root without a trailing `/v1`, or the full URL ending in `/v1/messages`. No separate pool or key is needed

Streaming responses retain Anthropic SSE events. The pool waits for `message_stop` before releasing an operation. Interrupted streams retain the existing quarantine behavior. Input reservation includes the system prompt, conversation and tool definitions; usage settlement includes cached input tokens. A confirmed upstream failure moves the next request under the existing pool rules and never silently replays a partially generated request

Use image `cr.selcloud.ru/ttkdc/litellm:exclusive-pool-messages-20261105` or its published digest. Update the proxy YAML setting above when deploying over an existing installation; changing the image alone does not replace mounted configuration

To check generation, set `PROXY_URL`, `LITELLM_API_KEY` and `MODEL_NAME` to your deployment values and run:

```bash
jq -n --arg model "$MODEL_NAME" '{
  model: $model,
  max_tokens: 64,
  messages: [{role: "user", content: [{type: "text", text: "Reply with OK"}]}],
  metadata: {user_id: "{\"device_id\":\"messages-check\"}"}
}' | curl --fail-with-body "$PROXY_URL/v1/messages?beta=true" \
  -H "x-api-key: $LITELLM_API_KEY" \
  -H 'anthropic-version: 2023-06-01' \
  -H 'content-type: application/json' \
  --data-binary @-
```

The response should have `type: message` and an assistant text block. Repeat with `stream: true` in the JSON and `curl -N` to check SSE through `message_stop`. Use a dedicated key because this check binds it to `messages-check` and charges the usual model cost
