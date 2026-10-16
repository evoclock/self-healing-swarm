Recorded token counts, and where a real dollar amount comes from.

The daily brief, tenant t-a, eight synthetic emails: 204 input tokens, 231 output tokens, `usage.cost` 0.00005844 dollars.

The document agent, tenant t-b, spreadsheet doc-b-budget: 84 input tokens, 48 output tokens, `usage.cost` 0.00001464 dollars.

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

Four projects now exist, one per tenant. Each has a monthly hard limit of 1 dollar. A recorded model call sends the tenant's project id as `X-Project-Id`. Spend after that lands on `GET /v1/projects/{project_id}/usage`. The two charges above are one brief call and one document call from the latest recording. They are not a per-agent day.

A read of organization usage for 1 Oct 2026 through 16 Oct 2026 showed 0.025478 dollars and 9 requests, on one key with no project. That read is not recomputed here. The documentation sample amounts are not this measurement.

The Unified API rate limit is a different product. On the Professional plan it is 400 requests per minute for each linked account. Gateway model traffic is not that limit.
