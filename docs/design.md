# Multi-tenant background agents

Julen Gamboa. Principal AI Engineer take-home. 14 Oct 2026, revised 15 Oct 2026.

## Solution design

Three parts. `schedule` decides which jobs fit in a window. A locked container is the sandbox when a run is started as a container. The egress proxy holds the provider key, checks the budget, and writes the journal. Two proxies share Postgres and nothing else.

## Stack

Postgres 16 holds the journal, outbox, memberships and eval records, with forced row-level security. Python 3.12 runs the scheduler, the proxy and the agent loop. Docker runs Postgres and the agent containers. `make drill` applies migrations and runs the failure scenarios on recorded calls. Replay needs no provider key. `make record` is the separate command that writes those recordings.

## Scheduling 200 agents

`schedule` orders jobs by `job_id`. It does not order by deadline. The host cap is 8 running jobs. Each tenant has its own cap. The window is 90 minutes. A job that does not fit is escalated. With 200 jobs of 10 minutes, 72 are placed and 128 are escalated. `make test` calls `schedule`. `make drill` does not.

The yaml stores a deadline, and `schedule` does not read it. A 503, then a 429, falls back to `local` and logs `provider_fallback`.

## Sandbox

A container run uses user 65534, a read-only root, all capabilities dropped, no new privileges, 512 MB of memory, half a CPU, 64 processes, and a 64 MB `/tmp` that cannot execute files. It joins `swarm-agents`, an internal network, so it cannot resolve the public internet. The run token is not the provider key.

The host repo is mounted read-only at `/src`. The proxy refuses `py.exec` with `not_executed` and does not start a process. Spreadsheet code runs in the agent process. The program is fixed, cell text arrives on stdin, and a cell is never part of the `-c` string.

The kill scenario and the agent half of escape use this container. The other drill scenarios run the same runner inside the drill process, against the proxy.

## Failure and resume

A step is keyed by the run, the step number, and a hash of the messages and the tools. The model name is outside the hash, so a fallback replays the same recording. The proxy commits the reserved cost before the provider call.

A caller that finds `STARTED` for its own generation receives 202, however old the row is. It does not call the provider and does not replace the reservation. The client polls for about 90 seconds. If the answer is still 202, the runner stores `FAILED` with artifact `in_progress`. It does not store `SUCCEEDED`.

Only a newer generation takes over a `STARTED` step. Resume gets that generation from `bind_token`. The takeover sets `cost_uncertain` and calls the provider. `finish_step` writes only when the generation and `started_at` still match the claim. Completed steps are skipped and their costs stay.

`on_lease_expired` resumes a killed run. The first and second call return `RUNNING`. The third returns `ESCALATED`. The drill calls this. Nothing watches leases in the background.

The hang scenario runs a 30-second sleep with a 1-second timeout, retries once, then stores `FAILED`. `max_steps` and `wall_s` are parsed from the yaml and not enforced. Three identical digests stop as `STOPPED_LOOP` and keep the last text. A recorded `usage.cost` past `max_usd` returns 402 before that replay is served, and the run stops as `STOPPED_BUDGET` with the text "saved progress". An empty artifact is stored as `FAILED`. A rule of three tool-error retries, then back to the model, is tested as a function. The runner does not call it.

## Tenants, evals, memory, panel, drafts

Each membership is one tenant and a role: viewer, approver, or admin. The proxy resolves the token to that tenant. Row-level security hides every other tenant, and an unset tenant sees no rows. The build has no signup flow.

Live evals sync cases, trigger a run, and poll until the status is completed, failed, or cancelled, then branch on `passed`. Results list each case with `cost_usd` and `grader_results`. A suite has one schedule, every 6, 12, 24, or 168 hours. The model key calls these routes. The organization may trigger 10 runs a minute, and a suite may have 3 in progress. `make drill` still scores 20 recorded cases offline, with hard checks and a mean drop of at most 0.3, because that command has no key. The committed document recordings are all judge score 1.

On `SUCCEEDED`, a cited mail id is stored as a cursor in the same transaction. The agent cannot write that cursor. `mail.list` does not read it, so the next run still sees the same mail. `memory.write` can store a fact. Those facts are not loaded into the next prompt.

Panel, design only. Three models answer blind, then challenge claims with evidence for two rounds. The parent returns agreed claims, contested claims with how each was settled, and a minority view that was kept.

Agents write drafts. Only an approver in the same tenant can approve a send. The message id is the draft id. SMTP runs before the sink is appended. A later deliver that finds that id in the SMTP log does not send again. A recipient outside the tenant's domains is flagged and stays a draft.

Live prompt injection is `GET .../pi-settings` with a management key that has `manage_projects`. The body is `override`, `effective`, and `inherited_fields`. Direct and indirect modes are separate, each off, alert, or block. The mailbox mail is indirect. The drill still requires a flagged draft and does not call this route.

Each tenant stores a Gateway project id. A recorded model call sends that id as `X-Project-Id`. Replay does not call Gateway. Project spend is `total_spend`, `request_count`, and `model_spend` from the Gateway usage endpoint. `model_spend` is per model and per month, so one model shared by both agents does not yield a per-agent day. A single call's charge is `usage.cost` on the response. The journal stores that field. Replay adds the stored figure and can refuse before serving the recording. A live call is refused when the sum already stored is past `max_usd`. The call that crosses still goes out. The project hard limit can refuse it before the vendor, and the returned `usage.cost` is what the next check sees.

A recorded cloud call sends trace headers for the tenant, the run, and the step. The next call on that proxy nests under the previous request id. Replay and the local model send none of these. The proxy prints one line per complete, tool, state, and event.

## Not building

Live connectors, a web UI, OIDC, a replica, multi-host scheduling, and the panel. No gVisor or Firecracker, so runs share the host kernel. `max_steps` and `wall_s` are not enforced. The cursor does not skip mail. Most drill scenarios do not run in the locked container.

## The weakest part

The proxy. It holds the provider key, the tenant check, and the budget. Two instances can survive a crash, and they run the same code, so one bug is every tenant's bug. Next would be a per-run microVM, short-lived keys, and a watcher that calls `on_lease_expired` when a container dies.
