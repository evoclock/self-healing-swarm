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


def after_provider_pressure(errors_in_window: int, throttled: bool, models: tuple[str, ...], current: str) -> tuple[str, str]:
    if errors_in_window < 3 and not throttled:
        return ("stay", current)
    index = models.index(current)
    if index + 1 >= len(models):
        return ("escalate", current)
    return ("fallback", models[index + 1])


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
