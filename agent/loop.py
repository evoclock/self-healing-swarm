def journal_action(status: str) -> str:
    if status == "COMPLETED":
        return "skip"
    if status == "STARTED":
        return "rerun_uncertain"
    if status == "FAILED":
        return "rerun"
    raise ValueError(status)


def after_tool_error(attempts: int) -> str:
    if attempts < 3:
        return "retry"
    return "to_model"


def after_hang(attempts: int) -> str:
    if attempts < 1:
        return "retry"
    return "cancel"


THROTTLE_RETRIES = 3


def after_provider_pressure(errors_in_window: int, throttles: int, models: tuple[str, ...], current: str) -> tuple[str, str]:
    """503s: stay until three, then fall back. 429s: back off and retry the same model three times, then fall back."""
    if errors_in_window >= 3 or throttles > THROTTLE_RETRIES:
        index = models.index(current)
        if index + 1 >= len(models):
            return ("escalate", current)
        return ("fallback", models[index + 1])
    if throttles:
        return ("backoff", current)
    return ("stay", current)


BACKOFF_CAP_S = 8.0


def backoff_delay(attempt: int, uniform, base: float = 0.5, cap: float = BACKOFF_CAP_S) -> float:
    """Exponential backoff with jitter: attempt n waits between half and all of base * 2**(n-1), capped at 8 s."""
    ceiling = min(cap, base * 2 ** (attempt - 1))
    return uniform(ceiling / 2, ceiling)


def is_loop(recent_digests: tuple[str, ...], steps_without_new_data: int) -> bool:
    if len(recent_digests) >= 3 and len(set(recent_digests[-3:])) == 1:
        return True
    return steps_without_new_data >= 5


def classify_artifact(artifact: str, cited_ids: tuple[str, ...], own_ids: frozenset[str]) -> str:
    if artifact.strip() == "":
        return "FAILED"
    if not cited_ids or any(cited not in own_ids for cited in cited_ids):
        return "FAILED"
    return "SUCCEEDED"


def stop_with_progress(kind: str, artifact: str) -> tuple[str, str]:
    if kind not in ("STOPPED_LOOP", "STOPPED_BUDGET"):
        raise ValueError(kind)
    return (kind, artifact)
