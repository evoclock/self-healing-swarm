from dataclasses import dataclass

RUN_STATES = (
    "QUEUED",
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
    "STOPPED_LOOP",
    "STOPPED_BUDGET",
    "ESCALATED",
)

TRANSITIONS = {
    "QUEUED": frozenset({"RUNNING"}),
    "RUNNING": frozenset({"SUCCEEDED", "FAILED", "STOPPED_LOOP", "STOPPED_BUDGET", "ESCALATED"}),
    "SUCCEEDED": frozenset(),
    "FAILED": frozenset(),
    "STOPPED_LOOP": frozenset(),
    "STOPPED_BUDGET": frozenset(),
    "ESCALATED": frozenset(),
}


class IllegalTransition(Exception):
    def __init__(self, src: str, dst: str):
        self.src = src
        self.dst = dst
        super().__init__(f"illegal_transition: {src} -> {dst}")


def transition(src: str, dst: str) -> str:
    if src not in TRANSITIONS or dst not in TRANSITIONS[src]:
        raise IllegalTransition(src, dst)
    return dst


def on_lease_expired(resume_count: int) -> tuple[str, int]:
    nxt = resume_count + 1
    if nxt >= 3:
        return ("ESCALATED", nxt)
    return ("RUNNING", nxt)


@dataclass(frozen=True)
class Job:
    job_id: str
    tenant_id: str
    duration: int
    deadline: int = 90
    provider: str = "primary"
    tokens: int = 0


@dataclass(frozen=True)
class Placement:
    job_id: str
    tenant_id: str
    start: int
    escalated: bool
    duration: int


def load_config(config, promoted: set[str]):
    from evals.gate import accept_config, config_hash

    accept_config(config_hash(config), promoted)
    return config


def schedule(
    jobs: tuple[Job, ...],
    tenant_caps: dict[str, int],
    host_cap: int = 8,
    window: int = 90,
    provider_caps: dict[str, int] | None = None,
    provider_tpm: dict[str, int] | None = None,
) -> tuple[Placement, ...]:
    """Earliest deadline first, minute by minute, inside host, tenant and provider limits.

    provider_tpm splits each provider's tokens per minute evenly across the tenants that use it: each tenant
    has a token bucket refilled with its share every minute, and a job starts only if its bucket covers its tokens.
    A job is escalated only when it can no longer finish by its deadline (or the window end).
    """
    provider_caps = provider_caps or {}
    provider_tpm = provider_tpm or {}
    share: dict[tuple[str, str], int] = {}
    for provider, tpm in provider_tpm.items():
        tenants = sorted({job.tenant_id for job in jobs if job.provider == provider})
        for tenant in tenants:
            share[(provider, tenant)] = tpm // len(tenants)
    buckets = dict(share)
    pending = sorted(jobs, key=lambda job: (job.deadline, job.job_id))
    running: list[tuple[int, Job]] = []
    placed: list[Placement] = []
    for minute in range(window):
        running = [item for item in running if item[0] > minute]
        if minute:
            buckets = {key: share[key] for key in share}
        for job in list(pending):
            if minute + job.duration > min(job.deadline, window):
                placed.append(Placement(job.job_id, job.tenant_id, -1, True, job.duration))
                pending.remove(job)
                continue
            if len(running) >= host_cap:
                continue
            if sum(1 for _end, other in running if other.tenant_id == job.tenant_id) >= tenant_caps.get(job.tenant_id, 0):
                continue
            cap = provider_caps.get(job.provider)
            if cap is not None and sum(1 for _end, other in running if other.provider == job.provider) >= cap:
                continue
            key = (job.provider, job.tenant_id)
            if key in buckets:
                if buckets[key] < job.tokens:
                    continue
                buckets[key] -= job.tokens
            running.append((minute + job.duration, job))
            placed.append(Placement(job.job_id, job.tenant_id, minute, False, job.duration))
            pending.remove(job)
        if not pending:
            break
    for job in pending:
        placed.append(Placement(job.job_id, job.tenant_id, -1, True, job.duration))
    return tuple(placed)
