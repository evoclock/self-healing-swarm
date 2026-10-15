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


@dataclass(frozen=True)
class Placement:
    job_id: str
    tenant_id: str
    start: int
    escalated: bool


def load_config(config, promoted: set[str]):
    from evals.gate import accept_config, config_hash

    accept_config(config_hash(config), promoted)
    return config


def schedule(jobs: tuple[Job, ...], tenant_caps: dict[str, int], host_cap: int = 8, window: int = 90) -> tuple[Placement, ...]:
    ordered = tuple(sorted(jobs, key=lambda job: (job.job_id,)))
    running: list[tuple[int, str, str]] = []
    placed: list[Placement] = []
    minute = 0
    pending = list(ordered)
    while pending and minute < window:
        running = [item for item in running if item[0] > minute]
        for job in list(pending):
            if len(running) >= host_cap:
                break
            tenant_running = sum(1 for _end, tenant, _jid in running if tenant == job.tenant_id)
            cap = tenant_caps.get(job.tenant_id, 0)
            if tenant_running >= cap:
                continue
            running.append((minute + job.duration, job.tenant_id, job.job_id))
            placed.append(Placement(job.job_id, job.tenant_id, minute, False))
            pending.remove(job)
        minute += 1
    for job in pending:
        placed.append(Placement(job.job_id, job.tenant_id, -1, True))
    return tuple(placed)
