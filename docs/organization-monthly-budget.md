# Monthly organization budget resets

Set `budget_reset_day` to an integer from 1 through 31 to reset an organization's budget each month at 00:00 in `Europe/Moscow`. This selects `budget_duration: "1mo"` automatically and takes precedence over a supplied duration

```bash
curl -X PATCH "http://localhost:4000/v2/organization/$ORGANIZATION_ID" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"budget_reset_day": 15}'
```

The same field is supported by `POST /organization/new` when creating a new linked budget, and by `PATCH /organization/update` with `organization_id` in the request body

The next reset is the nearest selected midnight strictly after the update. Changing the schedule preserves accumulated spend. The existing reset job applies the reset on its first run after that midnight and calculates the next monthly date

If the day does not exist in a month, the reset uses the last day of that month. For example, day 31 uses February 28 in a non-leap year, then March 31. The stored preferred day stays 31

Sending `{"budget_reset_day": null}` disables scheduled resets and clears `budget_duration` and `budget_reset_at`. Omitting the schedule fields preserves the existing schedule. Sending `budget_duration` without `budget_reset_day` switches back to the existing interval behavior and clears the custom day

The day is stored on the linked budget and returned in `litellm_budget_table` by organization info and list APIs. Organizations that share a budget ID also share its reset schedule. Use a separate budget for each organization that needs an independent reset day

Apply the schema migration before running the updated proxy. The migration adds one nullable integer column and does not rewrite existing rows
