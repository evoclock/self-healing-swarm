This repo runs multi-tenant background agents. Each run is a locked container. The container can talk only to an egress proxy. The proxy checks the run token and the container IP, applies the budget, injects the provider credential, and writes the step journal in Postgres. Two proxy processes share that database and nothing else. Agents write drafts. Sending is a separate step that needs an approver in the same tenant.

## System design

[Open the interactive system design](https://evoclock.github.io/fieldnotes/system-design.html). It covers the architecture, trust boundaries, scheduling, sandbox, failure handling, tenants, and drafts.

The picture has three moving parts.

The scheduler takes an agent config with a deadline and starts or resumes a run. Two hundred briefs due at the same minute spread across the window before that deadline. A tenant concurrency limit and a host cap of 8 containers hold the starts. If the deadline would be missed, the run goes to a person.

The run container is new for that run. It is a regular user on a read-only filesystem, with a small scratch disk, no extra capabilities, and CPU, memory, and process limits. It receives a run token, which names the run and is not a credential. The only peer it can reach is the proxy.

Two egress proxies share Postgres and nothing else. The proxy binds the token to the container IP, checks the tenant and the budget, writes STARTED before a model call and COMPLETED or FAILED before it replies, and adds the provider key on the way out. The cloud model is tried first. A rate limit or a run of provider errors falls back to the local model. The step key is the run, the step number, and a hash of the messages and tools, so the model can change without looking like a new step. Drill mode replays a recorded call and does not open a socket.

Postgres holds the journal, the outbox, memberships, memory, and eval records. Row-level security is forced. A query with no tenant set returns no rows. The synthetic mailbox is the connector interface the proxy calls. A succeeded run must cite message ids from its own tenant.

Sending is a separate action. An approver in the same tenant approves it. The message id is the draft id, so a second deliver does not send again. A draft that leaves the tenant's domains or attaches documents stays flagged.

Evals score a recorded baseline. Hard checks cover tenant-only citations, unflagged external drafts, and spreadsheet totals. A later prompt is kept only when those checks pass and the mean drops by at most 0.3. The first record for an agent uses the hard checks alone.

A killed container leaves the journal. The scheduler starts a new container on the same run and skips completed steps. A step that died after STARTED is rerun and marked cost-uncertain. Loops, budget stops, a third resume, and a third sandbox denial stop the run for a person.

## One command

`make drill` builds the agent image, starts Postgres and both proxies in replay mode, and runs the scenarios. It writes docs/drill-output.txt. The process exits non-zero if any scenario fails. Replay does not need a provider key.

A passing run prints PASS for proxies, kill, provider, injection, escape, budget, hang, loop, eval_gate, empty, and send. Each scenario line includes the run id and the event names that justify it.

## Prerequisites

Docker and make.

`make record` also needs the merge gateway key in the macOS login keychain, service `MERGE_GATEWAY_API_KEY`, account `merge-gateway`, and a local model endpoint reachable by the proxy over the configured network that advertises GLM 5.3 Flash. The key is passed only into the proxy process. The agent container does not receive it.

`make capture` records the eval cases against that local model. `make evals` scores a recorded candidate offline.

`make test` runs the unit tests and the sandbox probe.

## Layout

migrations holds the schema, row-level security, and the synthetic mailbox. runtime holds config parsing, the deadline scheduler, and the container start flags. proxy is the egress proxy and the provider cassette. agent is the in-container loop. synthetic and outbox are the mailbox and the draft send path. evals holds the golden cases, the judge recordings, and the gate. drill runs the scenarios. recordings/steps holds provider cassettes. recordings/evals holds one JSON output per golden case. agents holds one config per tenant.

## Record, then replay

`make record` starts the proxies in record mode and runs the daily brief and the document agent once through proxy-a. The gateway model serves primary. The local model serves the fallback call. Responses are stored under recordings/steps, keyed by a hash of the messages and the tools. The model name is not part of the hash, so a later fallback replays the same cassette.

`make capture` runs each golden case for the daily brief, the weakened brief, the harmless wording change, and the document agent. The judge call is temperature 0 and is stored in the case file as judge_score. The gate writes an eval_records row. The first promoted row for an agent is kept when the hard checks pass. A later candidate is kept only when the hard checks pass and the mean is at most 0.3 below the last promoted mean.

`make drill` then replays those files. A missing cassette or a missing eval recording fails the scenario that needs it.

## What the gate blocks

Hard checks are code. A cited id must belong to the case. An external draft must be flagged. The injection case must not produce an unflagged draft or an attachment. Spreadsheet figures must match the expected total. The weakened prompt's recording fails the injection case and the cross-tenant cases. The scheduler refuses to load that config because its hash has no promoted row. The harmless wording change passes and can be loaded.

A brand new prompt has no recording. The gate cannot score it offline. Run `make capture` once, with the local model up, before expecting `make evals` to judge it.

## What I would add with more time

To harden and complete the system, I would:

- **Sandboxing:** Replace ordinary containers with gVisor or Firecracker so tenant workloads do not share the host kernel.
- **Credentials and service identity:** Move the long-lived provider key out of the proxy into a vault, issue short-lived tokens through a broker, and use mutual TLS between the gateway, broker, and ledger.
- **Availability and scale:** Add a Postgres replica and multi-host scheduling.
- **Identity and integrations:** Add OIDC, live mailbox connectors, and a web UI.
- **Memory limits:** Make runs skip mail older than the saved cursor, expire facts, and cap their size.
- **Delivery:** Replace the append-only SMTP sink with an external mail service while keeping same-tenant approval and idempotent delivery.
- **Evaluation coverage:** Validate the local judge against known cases and expand gateway recordings beyond the sample agent runs. Cases without mailbox rows or a real budget sheet currently use synthetic messages or generated CSVs.
- **Evidence review:** Build a review panel in which three models from different providers answer independently, then challenge specific claims. Keep claims only when they have cited evidence; report agreement, contested claims, and minority views. Treat unanimity without evidence as weak.
