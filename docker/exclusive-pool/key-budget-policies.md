# Individual key budget policies

Available in `cr.selcloud.ru/ttkdc/litellm:exclusive-pool-key-policies-20261105` for `linux/amd64`

Set `metadata.key_budget_policy` on a team to apply the same individual allowances to every key on that team. Set it on an organization to supply allowances for keys without a team. Aggregate team and organization budgets remain separate

Only a proxy administrator can configure or remove a policy. Policies require exactly two windows, `5h` and `7d`, with positive finite dollar amounts

```bash
curl --fail-with-body "$PROXY_URL/team/update" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "team_id": "team-a",
    "metadata": {
      "key_budget_policy": {
        "budget_limits": [
          {"budget_duration": "5h", "max_budget": 10},
          {"budget_duration": "7d", "max_budget": 100}
        ]
      }
    }
  }'
```

Use the same metadata on `/team/new`, `/organization/new`, `/organization/update` or `PATCH /v2/organization/{organization_id}`. Preserve other metadata when using an endpoint that replaces metadata

An organization administrator creates a key with `team_id` or, for an unteamed key, `organization_id`, without supplying budget fields

```bash
curl --fail-with-body "$PROXY_URL/key/generate" \
  -H "Authorization: Bearer $ORGANIZATION_ADMIN_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"team_id": "team-a", "key_alias": "member-key"}'
```

Each key has independent spend counters. The first exhausted window blocks that key. LiteLLM's existing wall-clock reset mechanics apply: `7d` resets on Monday at the configured budget reset time, and `5h` uses the existing hour alignment

Existing keys receive policy changes before their next authenticated request, after the existing team or organization cache refreshes. Windows are persisted on the key, preserving reset timestamps and duration-based spend counters. A concurrent key update may return HTTP 409 and should be retried

A team key uses only its team's policy. If a team has no policy, it does not take the organization's unteamed-key default. Configure every team used for delegated issuance. A key with no applicable policy keeps its existing settings

Non-proxy callers cannot override inherited budget fields, including explicit null or empty values. Keys with stored Budget Windows also prohibit non-proxy spend resets and regeneration, including after moving to an unconfigured team. Automatic rotation is blocked for policy keys and keys with stored windows, including schedules configured before attaching the policy. Create a separate key when a new allowance is intended. Each additional key has its own allowance, so this feature does not limit total spending across multiple newly created keys

Only proxy administrators can create teams in an organization with an active default key policy. Organization administrators cannot remove a team's organization. Organization administrators identified through memberships must specify a team or organization when issuing keys

Removing an entity policy does not delete previously materialized key windows. A proxy administrator can change those windows explicitly. Organizations retain LiteLLM's existing Enterprise license requirement
