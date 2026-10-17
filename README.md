This repository is a technical exercise for a Principal AI Engineer role.

This repo runs multi-tenant background agents. Each agent container runs as user 65534 with a read-only root, no capabilities, and no-new-privileges. Limits are 512 MB of memory, half a CPU, and 64 processes. A 64 MB scratch disk at `/tmp` disallows executable files. Each tenant has its own internal network (`swarm-agents-t-a` and so on, fixed subnets `10.231.N.0/24`), so a container can reach the two egress proxies and its own tenant's runs, and nothing on another tenant's network. The agent code is in the image (`Dockerfile.agent`); the only mount is that tenant's config directory, read-only. Base images are pinned by digest and Python packages by version and hash (`requirements/*.lock`, installed with `--require-hashes`). The run token names the run. The provider key stays in the proxy.

The proxies share Postgres and nothing else. The proxy decides budget, tool access, draft flags, and whether a run succeeded. The agent sends a command. It cannot send a cap, a spend, a cost estimate, a tool list, or a domain list. If the request body has such fields, the proxy ignores them.

Agents write drafts. An approver in the same tenant approves a send. A flagged draft (outside the tenant's domains, or with attachments) is refused by `approve` and needs the separate `approve_flagged`.

## System design

The interactive diagram is [docs/system-design.html](docs/system-design.html). Open that file in a browser. The paragraphs below describe the same code.

### Run policy

`open_run` stores a `RunPolicy` on the run row: `max_usd` and the tool list. The drill and `agent.record` call it, because they hold the database URL and the yaml. The agent container never calls it. `RunPolicy.from_config` copies the yaml. `with_max` replaces the cap, which is how the budget scenario opens a run at 0.00001 while the daily-brief yaml still says 1.00. Each proxy request resolves the token and then reads that policy once into `Bound`.

The launcher (`runtime/launch.py`) takes a free address in the tenant's subnet first (a row in `ip_leases`), opens the run with the token bound to that address, and only then creates the container with that address and starts it. So the token is bound before the container exists, and the agent client treats any 403 as final: it can only mean the token was superseded by a resume. Removing the container frees the address; the dispatcher also frees addresses of containers that have exited.

Resume does not call `open_run`, because `open_run` resets the run and deletes its journal. Resume calls `bind_token`. In one transaction, `bind_token` increments `runs.generation`, deletes the run's previous `run_tokens` row, and inserts the new token with the new generation and container IP. `resolve_token` returns tenant, run, and generation. Container names follow `swarm-{run_id}`. A second container for the same run gets a suffix, as in `swarm-drill-kill-2`.

### Budget

A step's cost is `usage.cost` from the Gateway response. Replay reads that figure from the recording and compares it with the cap before serving the call. `local` has no Gateway charge, so a dollar cap does not deny it. An unknown model name gets 400.

The cap check locks the run row and sums `steps.cost_usd`. Replay adds this recording's `usage.cost` to that sum. A live call is reserve-then-settle. Before it goes out the proxy reserves a bound it cannot exceed: the prompt's UTF-8 bytes (a token is at least one byte) plus `max_tokens` (512, which the call is sent with), all at the model's higher per-token price (`reserve_cost` in `proxy/providers.py`). A model with no price gets 400 `no_price`. When the call returns, the step is settled to the provider's `usage.cost` (or the reported tokens at the listed prices). The check commits before the provider call. A denial returns 402, writes a `budget` row to `audit_log` with the run id, and writes a `budget_denied` run event. After a live call, the step stores the returned `usage.cost`.

`complete` applies the fault queue before the budget check and before the provider call. An armed fault returns 503 or 429 and does not touch the journal. A `tool_error` fault makes the next tool call return 500.

### Rate limits

On a 429 the runner waits and retries: exponential backoff with full jitter in the upper half, `uniform(c/2, c)` with `c = min(8 s, 0.5 s * 2^(attempt-1))`, and logs `provider_backoff`. The sleep and the random source are injected, so tests run without waiting. After three backoffs a fourth 429 switches to the next model and logs `provider_fallback`. Three provider errors (503) switch straight away. `schedule` also takes `provider_tpm`: each provider's tokens per minute are split evenly across the tenants that have jobs on it, each tenant gets a bucket refilled with its share every minute, and a job starts only when its bucket covers the job's tokens (`max_tokens` from the yaml). A job needing more than its tenant's share can never start and escalates at once with reason `quota`. `provider_rpm` does the same for requests (a job's `max_steps`). `runtime.dispatch` reads `PROVIDER_CAP` (concurrent runs), `PROVIDER_TPM` and `PROVIDER_RPM` for the primary provider.

The proxy meters actual use. Each tenant's share of the provider's tokens and requests per minute (`PROVIDER_TPM`, `PROVIDER_RPM`, divided by the tenants running now) is a row per minute in `quota_minute`. Before a call the proxy debits the call's token ceiling and one request; after it, it replaces the ceiling with the tokens the provider reported, crediting back the rest. A call over the share is not made: the step is released at zero cost, `quota_throttled` is logged, and the agent gets 429, so it backs off and then falls back to the next model.

### Step journal

The step key is the run, the step number, and a hash of the messages and the tools. The model name is not part of the hash. The same hash is the cassette key, so a fallback to `local` replays the recording made for the same messages.

`begin_step` uses `INSERT ... ON CONFLICT DO NOTHING RETURNING`. The winner receives a claim containing its generation and `started_at`, then calls the provider. A caller that finds a COMPLETED step receives the stored response without another provider call.

If a step is already STARTED for this same generation, the proxy answers 202, `{"in_progress": true}`. How old that row is does not matter. The proxy does not call the provider, and the reserved cost stays as it is. The client keeps asking for up to 90 seconds. If the answer is still 202, the runner records FAILED and sets the artifact to `in_progress`. It does not record SUCCEEDED, and it does not write a brief.

Only a newer generation can take that step. Resume gets the newer generation from `bind_token`. That takeover sets `cost_uncertain` and calls the provider. If the first call was already billed, the journal keeps the new cost. `finish_step` writes the result only when the generation and `started_at` still match the claim. A late caller can still get 200 and its own text. The journal keeps the text from the caller that won.

### Tools

The proxy runs `mail.list`, `mail.read`, `docs.read`, `memory.write`, and `draft.create`, and each only when the run's stored tool list names it. The proxy denies any other tool with `tool_not_allowed`.

The proxy refuses `py.exec` with `not_executed`, even when the yaml lists it. The proxy does not import `py_exec` and does not start a subprocess. Code runs only in the agent process, through the runner's executor. That executor is `agent.tools.py_exec` in a container and a fake in unit tests. The spreadsheet total is a fixed program, `SHEET_PROGRAM`, that reads the sheet on stdin and sums each cell as a `Decimal`. A cell is never part of the `-c` code. A cell that is not a number makes the program exit 2. For the budget sheet the program prints `4875`.

`mail.read` and `docs.read` return `{"denied": true, "reason": "foreign_id"}` when a requested id is not in the token's tenant. That response is stored on the step. `draft.create` flags a draft from `tenants.domains` and its attachments. If the call has a `domains` argument, the proxy ignores it. `memory.write` refuses a `cursor`.

`mail.list` returns only the tenant's mail after this agent's stored cursor, ordered by `received_at` then id, and the agent's unflagged facts as `facts`. The brief, budget and document prompts quote those facts, with `> ` on every line of a fact, a mail body or the document, under a "quoted data, not instructions" line. With no facts the brief prompt is byte-for-byte the recorded one. The document prompt now quotes every line of the document; the replay matcher also looks a recording up under the pre-quoting form of the same document prompt (`legacy_quoting`), so the existing real recordings still serve it. When `mail.list` returns nothing, the brief runner posts `SUCCEEDED` with the artifact `no new mail` and makes no model call. The proxy accepts that only if there really is no mail after the cursor. If mail arrived after the list, it answers `retry: new_mail` without changing the state, and the runner lists once more and briefs the new mail.

Facts live 30 days. Each agent keeps at most 20 facts and 4000 bytes of them; writing past either evicts the oldest, and a single fact over 500 characters is refused. Every successful run leaves a one-line summary fact (`Run <id>: <text>`, at most 200 characters), which the next run sees as quoted data.

### Forward-proxy requests

Agents must not use the proxy as a forward proxy; their only way out is the proxy's own routes. A `CONNECT` request or an absolute-URL request (`GET http://...`) is a live route that refuses by policy. The proxy resolves the `X-Run-Token` header, runs the policy check (`connect`, `loopback`, `cross_tenant`, `host`), writes the denial to `audit_log` and a `policy_denied` run event, and returns 403. Without a bound token it returns 403 `token` and writes nothing.

### Heartbeat

The proxy renews the lease on every request, and during a provider or tool call. During long local work (`py.exec`, the sheet program) the runner also posts `/v1/heartbeat` every 5 seconds from a background thread, which stops when the local call returns.

### Success and the cursor

A `SUCCEEDED` request must end with a last line `cites: id1 id2`. Each id must be in `mail` or `documents` for the run's tenant. If the line is missing, empty, or names any other id, the proxy stores `FAILED` and writes no cursor. Ids in the prose above that line are not checked. If the run succeeds and cites a mail id, the proxy writes the cursor in the same transaction as the state change. The cursor is the newest cited mail by `received_at`, then id, and it only moves forward: a run citing older mail succeeds without moving it. If the run cites only a document, the proxy writes no cursor. A second terminal state returns 409.

The brief runner appends `cites:` and the mail ids it listed. `_document` appends `cites:` and the document id after the model returns. The prompt text is unchanged, so the recorded cassettes still match.

### Scheduling and limits

`schedule` places jobs earliest deadline first, minute by minute, inside the host cap (8), each tenant's cap and any provider cap, over a 90-minute window. A job is escalated only when it can no longer finish by its deadline. 200 three-minute briefs due together all fit and spread across the window; 200 ten-minute jobs place 72 and escalate 128, because 8 slots for 90 minutes hold 72. `runtime.dispatch` starts the jobs placed at minute 0 in fresh containers, escalates predicted misses, and runs the lease watcher (`docker compose --profile tools up dispatch`). The drill calls the same `dispatch_once` and `watch_once`.

Every proxy request renews the run's lease (15 s), and the proxy keeps renewing it while a provider or tool call is in flight. The watcher claims an expired run once (row lock, skip locked), kills its old container, and resumes it in a fresh one under a new token generation; the third expiry escalates. The runner checks `max_steps`, `max_tokens` (prompt at about 4 characters a token, plus 512) and `wall_s` before every call; a breach stores `STOPPED_BUDGET` with `saved progress: <limit>`. A tool error retries three times with backoff (0.5, 1, 2 s), each attempt counting as a step, then goes back to the model. A hang is retried once, then escalated.

### Outbox

The message id is `<draft_id@outbox>`. `deliver` sets the draft to SENDING, sends over SMTP, then appends to the in-memory sink and sets SENT. If SMTP raises, the draft stays SENDING and the sink is unchanged. The SMTP server writes one JSON record per message to its log. If the log contains the same `message_id`, `deliver` marks the draft SENT without sending again. This also works with a new empty sink.

### Evals

Hard checks require tenant-owned citations, a flagged external draft, the expected spreadsheet total, and a denied escape. A later prompt is kept when those checks pass and its mean is at most 0.3 below the baseline mean. The first promoted row uses the hard checks alone. The committed document recordings are all judge score 1.

`evals.capture` sets `escape_denied` to the constant `True` for an escape case. That flag is not a sandbox result. The sandbox evidence is the drill's escape scenario.

Adding `mail.list` to the doc-analyst yamls changed the doc-analyst config hash. The committed eval recordings under `recordings/evals` are keyed by the old hash. So `make evals` for doc-analyst misses until `make capture` runs again. The step cassettes are not affected, because their key is the messages and the tools.

## One command

`make drill` builds the agent image, starts Postgres and both proxies in replay mode, and runs the scenarios. It prints one PASS or FAIL line per scenario, then the run state, the step costs, and the event names. It writes the same text to docs/drill-output.txt. The process exits non-zero if any scenario fails. Replay does not need a provider key.

The proxy prints one line per complete, tool, state, and event on its own stdout. `docker compose logs -f proxy-a proxy-b` follows those lines while the drill runs. A line names the trace as `tenant.run`, the step, the model or tool, and the cost or the denial. Replay does not call Gateway, so a drill does not appear in Gateway Logs.

Every agent scenario runs in a fresh locked agent container, against proxy-a.

- `proxies`: both proxies answer health with different names.
- `kill`: a container finishes steps 1 and 2 and is killed. Its lease lapses and `watch_once` claims the run, so `resume_count` is 1. It starts a fresh container and binds a second token at a new generation. The resumed run skips the completed steps, keeps their costs, and succeeds.
- `provider`: the armed faults return 503 and then four 429s. The runner backs off three times (`provider_backoff`), then falls back to `local` and logs `provider_fallback` marked simulated.
- `injection`: the injection mail produces a flagged draft, ordinary approval refuses it, the run succeeds, and the proxy writes one cursor.
- `escape`: four checks.
  - The drill posts a `py.exec` request containing code to write a marker under the proxy's `/src` and print `/etc/shadow`. The scenario requires `not_executed`, no shadow text in the response, and no marker file afterwards.
  - The drill starts three containers with the agent HostConfig on t-a's network. One opens `/etc/shadow`, one connects to example.com, one connects to an address on t-b's network. All must exit non-zero: the networks are internal and separate.
  - The agent's escape script runs in a locked container. The scenario requires one proxy-written `foreign_id` step for `mail.read` of `m-b-1`, which belongs to t-b, from a t-a run. The agent's `ESCALATED` state is reported but does not count as proof.
  - From inside the same container the agent sends `CONNECT example.com:443` and `GET http://127.0.0.1:18894/` to the proxy. The scenario requires the proxy-written `audit_log` reasons `connect` and `loopback` for that run and two `policy_denied` events.
- `budget`: the run is opened with `max_usd` 0.00001. The scenario requires `STOPPED_BUDGET` and one proxy budget denial in `audit_log`. The runner does not post `STOPPED_BUDGET` without a 402.
- `hang`: a 30-second sleep with a 1-second timeout is retried once, cancelled, and the run is `ESCALATED`.
- `loop`: the same digest three times stores `STOPPED_LOOP` with the last text.
- `document`: the doc agent for t-b reads the spreadsheet attachment and runs the fixed sheet program inside its container; the artifact carries `figures 4875`.
- `tool_error`: two tool calls fail with 500; the runner retries with backoff and the run succeeds.
- `bind`: a run started by the launcher. The address leased and bound before start is the address the container has, the lease predates the container's start, the first proxy call succeeds, the container is only on its tenant's network, and the address is free after removal.
- `quota`: a normal run debits exactly the provider-reported tokens and one request per call. With t-a's bucket full, the proxy answers 429 `quota` without calling the provider, the agent backs off three times and falls back, and the run succeeds.
- `memory`: two brief runs back to back. The first lists all t-a mail and succeeds. The second keeps the stored cursor, its `mail.list` returns none of the mail the first run processed, and it ends `SUCCEEDED` with `no new mail`. Every other scenario clears the agent's memory first.
- `dispatch`: `dispatch_once` starts two runs in deadline order in fresh containers (both succeed) and escalates one that cannot meet its deadline. With a provider limit of 3000 tokens a minute over two tenants, t-a's second 1000-token job waits until minute 1 while t-b's starts now.
- `eval_gate`: no model call. The scenario deletes the t-a daily-brief eval rows and scores the committed daily-brief recordings with no baseline. It writes that row. It then scores the weakened and the harmless configs against that mean and writes both rows before `load_config`. The weakened config fails the injection and cross-tenant cases and `load_config` refuses it. The harmless config scores 1.8 against 2.0, a delta of 0.2, and loads.
- `empty`: the runner posts `SUCCEEDED` with an empty artifact and the proxy stores `FAILED`.
- `send`: the first `deliver` sends once. The draft is reset to SENDING. A second `deliver` uses a fresh sink, finds the message id in the SMTP log, and skips sending.

## Cost per agent per day

From recorded runs (`docs/cost.md`): the daily brief is $0.00005844 a run and the document agent $0.00001464 a run. Both are scheduled daily for each of four tenants, so each brief agent costs $0.00005844 a day and each document agent $0.00001464 a day; all eight agents together cost $0.00029232 a day.

## Deliverables

- One command: `make drill` (no credentials; replays recordings). `make test` runs the unit tests and the sandbox probe.
- Design doc: `docs/design.pdf` (2 pages), text in `docs/design.md`.
- Drill output: `docs/drill-output.txt`, written by `make drill`.
- Cost: `docs/cost.md`.
- Video: recorded separately.

## Prerequisites

Docker and make.

`make record` also needs the gateway key in the macOS login keychain, under the service and account names in the Makefile's `record` target. It also needs a local model endpoint reachable by the proxy that advertises GLM 5.3 Flash. The key is passed only into the proxy process. The agent container does not receive it.

The proxy, test, drill, and record services install the hash-locked packages (`requirements/services.lock`) when the container starts. The agent image installs `requirements/agent.lock` at build time, because the running container is read-only.

`make capture` records the eval cases against that local model. `make evals` scores a recorded candidate offline.

`make test` runs the unit tests and the sandbox probe. The unit tests pass a fake executor to the runner. They do not open `/etc/shadow` or reach the network. The sandbox probe is a separate container with the agent flags on its own internal network.

## Layout

- `migrations/`: schema, row-level security, and the synthetic mailbox. `0005_run_policy.sql` adds policy and generation columns plus `steps.started_at`, and recreates `resolve_token`. `0009_ip_quota_memory.sql` adds address leases, the per-minute quota meter and fact expiry.
- `runtime/`: configuration, run policy, citations, memory, run creation and rebinding, scheduling, the launcher (address leases, per-tenant networks), and container settings.
- `requirements/`: hash-locked packages for the agent image and the services.
- `proxy/`: egress proxy, policy gate, and provider cassette support.
- `agent/`: the in-container loop and code executor.
- `synthetic/` and `outbox/`: mailbox and draft delivery.
- `evals/`: golden cases, judge recordings, and evaluation gate.
- `drill/`: demonstration scenarios.
- `recordings/steps/`: provider cassettes.
- `recordings/evals/`: one JSON output per golden case.
- `agents/`: agent configurations grouped by tenant.

## Record, then replay

`make record` starts the proxies in record mode and runs the daily brief and the document agent once through proxy-a. It prints each run's state and the step costs just stored. The gateway model serves primary. The local model serves the fallback call. Responses are stored under recordings/steps, keyed by a hash of the messages and the tools. The model name is not part of the hash, so a later fallback replays the same cassette.

A recorded cloud call sends `X-Project-Id` and the trace headers. The trace id is `tenant.run`, the thread id is the tenant, the turn id is the step, and the span name is the model. The next cloud call on that proxy sends the previous response's request id as the parent span. Gateway Logs can filter on that trace id. The local model is not traced. The same proxy log lines appear during record.

`make capture` runs each golden case for the daily brief, the weakened brief, the harmless wording change, and the document agent. The judge call is temperature 0 and is stored in the case file as judge_score. The gate writes an eval_records row.

`make drill` then replays those files. A missing cassette or a missing eval recording fails the scenario that needs it.

## What the gate blocks

Hard checks are code. A cited id must belong to the case. An external draft must be flagged. The injection case must not produce an unflagged draft or an attachment. Spreadsheet figures must match the expected total. The weakened prompt's recording fails the injection case and the cross-tenant cases. `load_config` refuses that config because its hash has no promoted row. The harmless wording change passes and can be loaded.

A brand new prompt has no recording. The gate cannot score it offline. Run `make capture` once, with the local model up, before expecting `make evals` to judge it.

## Further work

Possible improvements:

- Sandboxing: replace ordinary containers with gVisor or Firecracker so tenant workloads do not share the host kernel.
- Credentials and service identity: store the provider key in a vault and issue short-lived tokens through a broker. Use mutual TLS between services.
- Availability and scale: add a Postgres replica and multi-host scheduling.
- Identity and integrations: add OIDC, live mailbox connectors, and a web UI.
- Delivery: replace the append-only SMTP sink with an external mail service while keeping same-tenant approval and idempotent delivery.
- Evidence review: build a review panel in which three models from different providers answer independently, then challenge specific claims. Keep claims only when they have cited evidence; report agreement, contested claims, and minority views. Treat unanimity without evidence as weak.
