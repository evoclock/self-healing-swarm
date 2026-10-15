This repo runs multi-tenant background agents. Each run is a locked container. The container can talk only to an egress proxy. The proxy checks the run token and the container IP, applies the budget, injects the provider credential, and writes the step journal in Postgres. Two proxy processes share that database and nothing else. Agents write drafts. Sending is a separate step that needs an approver in the same tenant.

## System design

Open [docs/system-design.html](docs/system-design.html) in a browser. It is the architecture diagram: components, trust boundaries, and the notes for scheduling, sandbox, failure, tenants, drafts, and what is not built. The file is self-contained.

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

`make record` also needs the merge gateway key in the macOS login keychain, service MERGE_GATEWAY_API_KEY, account merge-gateway, and a local model at http://127.0.0.1:18894/v1 that advertises GLM-5.3-Flash-EXL3-TF-FNC7. The key is passed only into the proxy process. The agent container does not receive it.

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

## Panel

The panel is design only. Three models from three providers would answer through the proxy, first blind, then two rounds of challenging specific claims. A claim would survive only with cited evidence. The parent answer would list agreed claims, contested claims with the evidence that settled them, and any minority view. A unanimous claim with no evidence would be marked weak. That path is not built.

## Gaps

Live mailbox connectors, a web UI, OIDC, a Postgres replica, multi-host scheduling, gVisor, and Firecracker are not built. Ordinary containers share the host kernel.

The proxy still holds the long-lived provider key. There is no vault, no five-minute token broker, and no mutual TLS split between gateway, broker, and ledger.

The daily brief lists the tenant's mail and then writes the cursor. It does not yet skip mail older than the cursor on the next run. Facts are flagged when the text looks like an instruction, and a fact must cite a message in the tenant. Facts do not expire, and there is no write size cap.

The send path is a small SMTP listener that appends one JSON line per accepted message. It is not an external mail server. A second deliver of the same draft does not send again.

The eval judge is the local model, not the gateway model. Gateway recordings cover the sample agent runs used by the drill. Day cases that are not rows in the mailbox carry a short synthetic message so the case still has an email body. The document cases other than the real budget sheet carry a generated CSV with the expected total.

`docs/plan.md` is the build plan and is not part of the drill output.
