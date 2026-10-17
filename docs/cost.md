## Cost per agent per day

From the recorded token counts and costs below. Each agent runs once a day (`schedule.cadence: daily` in every yaml), one daily brief and one document agent per tenant, four tenants.

- Daily brief: $0.00006144 a run × 1 run a day = $0.00006144 per agent per day. Four tenants: 4 × $0.00006144 = $0.00024576 a day.
- Document agent: $0.00001668 a run × 1 run a day = $0.00001668 per agent per day. Four tenants: 4 × $0.00001668 = $0.00006672 a day.
- All eight agents: $0.00024576 + $0.00006672 = $0.00031248 a day.

These are one recorded call each (the brief for t-a, the document for t-b). A day with a fallback to `local` costs less (local is $0); a resumed run does not pay again for completed steps.

The daily brief, tenant t-a, eight synthetic emails: 204 input tokens, 246 output tokens, `usage.cost` 0.00006144 dollars.

The document agent, tenant t-b, spreadsheet doc-b-budget: 158 input tokens, 36 output tokens, `usage.cost` 0.00001668 dollars.

The journal stores `usage.cost` from the Gateway response. Replay charges that stored figure.

Project spend is `GET /v1/projects/{project_id}/usage`. It needs a management key with the read_usage scope. The key that calls the model cannot call this route. `start` and `end` are inclusive dates. They default to the first of the month through today.

The response carries:

- `merge_hosted_spend`, managed-credential spend in dollars
- `byok_spend`, bring-your-own-key usage at list price
- `web_search_spend`, server-side web search
- `total_spend`
- `request_count`
- `model_spend`, one amount per model
- `model_spend_granularity`, which is `month`
- `project_id`, `start`, and `end`

`model_spend` does not split a brief from a document run when both use one model. A single call's charge is `usage.cost` on that response, and that is the figure the journal stores.

Four projects now exist, one per tenant. Each has a monthly hard limit of 1 dollar. A recorded model call sends the tenant's project id as `X-Project-Id`. Spend after that lands on `GET /v1/projects/{project_id}/usage`. The two charges above are one brief call and one document call from the latest recording.

A read of organization usage for 1 Oct 2026 through 16 Oct 2026 showed 0.025478 dollars and 9 requests, on one key with no project. That read is not recomputed here. The documentation sample amounts are not this measurement.

Merge Gateway sets no default request or token rate limits for paid organisations; the model vendors' capacity bounds throughput ([rate limits](https://docs.merge.dev/merge-gateway/rate-limits)). A key can carry an optional `rate_limit_rpm`, and an organisation can have at most 25 open batches. This build sets neither and sends no batches. The 400 requests per minute figure belongs to Merge's Unified API Professional plan and does not apply to Gateway.

`PROVIDER_TPM` (default 200000) and `PROVIDER_RPM` (default 120) in `docker-compose.yml` are this build's own per-provider budget, split across tenants. They are not limits set by Merge.
