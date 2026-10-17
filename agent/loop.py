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


RETRY_AFTER_CAP_S = 60.0
GATEWAY_DAILY_LIMIT = "free_tier_daily_limit"


def throttle_delay(attempt: int, uniform, retry_after_s=None) -> float:
    """Wait the longer of the jittered backoff and the provider's Retry-After, Retry-After capped at 60 s."""
    delay = backoff_delay(attempt, uniform)
    try:
        hinted = min(RETRY_AFTER_CAP_S, max(0.0, float(retry_after_s)))
    except (TypeError, ValueError):
        hinted = 0.0
    return max(delay, hinted)


def after_daily_limit(models: tuple[str, ...], gateway_models: frozenset[str]) -> tuple[str, str | None]:
    """free_tier_daily_limit: never retry, never another Gateway model. First non-Gateway model, else escalate."""
    for model in models:
        if model not in gateway_models:
            return ("fallback", model)
    return ("escalate", None)


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
