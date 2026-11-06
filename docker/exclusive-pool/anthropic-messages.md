# Anthropic Messages in the exclusive pool

Claude Desktop can send `POST /v1/messages?beta=true` to the proxy using the same virtual key and public model names as Chat Completions. Both endpoints share key identity, physical upstream assignments, fallback, key budgets and pool admission limits

The pool accepts text, client `tool_use` / `tool_result` history, thinking blocks, images and documents. Images and documents can use inline base64 data, URLs, or provider file sources. Documents can also contain inline text or content blocks. Nested attachments inside client tool results are supported. Send the complete conversation on each request

Before generating a request with attachments or server state, the pool calls `POST /v1/messages/count_tokens` on the selected physical upstream using its own credentials. It atomically reserves the counted input plus the allowed output against both key and upstream TPM limits. System prompts, tools, thinking settings and output configuration are included in counting. A missing, failing or malformed count endpoint returns HTTP 503 with `pool_token_count_unavailable` before generation. A key TPM overflow returns HTTP 429; upstream capacity exhaustion returns HTTP 503. Unsent reservations are released

Every upstream used for these requests must expose the Anthropic Count Tokens endpoint at the same API base as generation. The pool delegates media fetching to the upstream and does not download arbitrary client URLs itself

Provider `file_id`, `container_upload`, server tool results and existing `container` references require a live assignment on the same key. The caller must create or upload these objects on that assigned physical upstream. The pool neither discovers their owner nor copies provider files or execution containers between accounts. The standard Files API does not automatically select an exclusive-pool assignment

Server tools and new container configurations can start a new assignment. Once generation starts with server state, the session stays on that physical upstream, including when switching models registered for the same account. If that account is unavailable, requests fail rather than exposing its references to another account. Failed counting does not pin an existing text session. Portable image and document requests keep the normal failover behavior

An existing server reference without a live assignment returns HTTP 409 with `pool_reference_binding_required`, including after the usual 15-minute idle expiry. Resume with a fresh portable conversation and recreate the server objects on the new account. Do not reuse old provider IDs after a reassignment

The verified virtual key identifies the client. Device metadata is optional and used only for diagnostics. Changed, missing or malformed `device_id` values do not reject requests. All sessions using one key share its upstream assignment and limits, including copies of that key on different devices

Use the native `anthropic/` provider prefix and the API root without a trailing `/v1`, or the full URL ending in `/v1/messages`. The default registration example already does this. Public model names such as `claude-opus-5-5` stay unchanged, and no separate pool or key is needed

For existing registrations, set `litellm_params.model` to `anthropic/<existing public model name>` and normalize `litellm_params.api_base` to that same upstream's API root through the standard model management API or UI. Keep the deployment ID, credentials and all `exclusive_*` labels. This changes the provider transport while preserving the physical upstream and pool assignments

The deployment template uses native Messages directly. Remove `litellm_settings.use_chat_completions_url_for_anthropic_messages` from older mounted proxy configurations. That switch selected a translation bridge for upstreams that only expose Chat Completions; these upstreams expose Messages themselves

Streaming responses retain Anthropic SSE events. The pool waits for `message_stop` before releasing an operation. Interrupted streams retain the existing quarantine behavior. Input reservation includes the system prompt, conversation and tool definitions; usage settlement includes cached input tokens. For portable context, a confirmed upstream failure moves the next request under the existing pool rules and never silently replays a partially generated request

Use image `cr.selcloud.ru/ttkdc/litellm:exclusive-pool-key-identity-20261106` or its published digest. Update the model registrations and mounted proxy YAML as described above when deploying over an existing installation; changing the image alone does not replace mounted configuration. Replace all workers sharing pool Redis state together, since older workers do not preserve the new reservation and affinity fields

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

The response should have `type: message` and an assistant text block. Repeat with `stream: true` in the JSON and `curl -N` to check SSE through `message_stop`. Use a dedicated key because this check assigns it an upstream and charges the usual model cost

To check a real image, set `IMAGE_PATH` to a local image and `IMAGE_MEDIA_TYPE` to its MIME type, for example `image/png`. Run against an upstream that supports both Messages and Count Tokens:

```bash
base64 < "$IMAGE_PATH" | jq -Rs --arg model "$MODEL_NAME" --arg media_type "$IMAGE_MEDIA_TYPE" '{
  model: $model,
  max_tokens: 64,
  messages: [{role: "user", content: [
    {type: "text", text: "Briefly describe this image"},
    {type: "image", source: {type: "base64", media_type: $media_type, data: gsub("[[:space:]]"; "")}}
  ]}],
  metadata: {user_id: "{\"device_id\":\"messages-check\"}"}
}' | curl --fail-with-body "$PROXY_URL/v1/messages?beta=true" \
  -H "x-api-key: $LITELLM_API_KEY" \
  -H 'anthropic-version: 2023-06-01' \
  -H 'content-type: application/json' \
  --data-binary @-
```

The assistant should describe the supplied image. To check a PDF, use `type: "document"` for the attachment and `application/pdf` as its MIME type, and change the prompt to ask about the document
