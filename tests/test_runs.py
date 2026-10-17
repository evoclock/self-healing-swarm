import pytest

from agent.loop import (
    after_hang,
    after_provider_pressure,
    after_tool_error,
    classify_artifact,
    is_loop,
    journal_action,
    stop_with_progress,
)
from runtime.scheduler import RUN_STATES, TRANSITIONS, IllegalTransition, on_lease_expired, transition


def test_every_run_state_transition():
    for src in RUN_STATES:
        for dst in RUN_STATES:
            if dst in TRANSITIONS[src]:
                assert transition(src, dst) == dst
            else:
                with pytest.raises(IllegalTransition) as caught:
                    transition(src, dst)
                assert caught.value.src == src
                assert caught.value.dst == dst


def test_empty_artifact_is_failed_and_own_citation_succeeds():
    own = frozenset({"m-a-1", "m-a-inject"})
    assert classify_artifact("", ("m-a-1",), own) == "FAILED"
    assert classify_artifact("   ", (), own) == "FAILED"
    assert classify_artifact("Brief cites m-b-1", ("m-b-1",), own) == "FAILED"
    assert classify_artifact("Brief cites m-a-1", ("m-a-1",), own) == "SUCCEEDED"


def test_lease_expiry_escalates_on_the_third_resume():
    assert on_lease_expired(0) == ("RUNNING", 1)
    assert on_lease_expired(1) == ("RUNNING", 2)
    assert on_lease_expired(2) == ("ESCALATED", 3)


def test_journal_skips_completed_and_marks_started_uncertain():
    assert journal_action("COMPLETED") == "skip"
    assert journal_action("STARTED") == "rerun_uncertain"
    assert journal_action("FAILED") == "rerun"


def test_tool_errors_retry_three_times_then_return_to_the_model():
    assert after_tool_error(1) == "retry"
    assert after_tool_error(2) == "retry"
    assert after_tool_error(3) == "to_model"


def test_hang_is_retried_once():
    assert after_hang(0) == "retry"
    assert after_hang(1) == "cancel"


def test_provider_pressure_switches_model():
    models = ("primary", "local")
    assert after_provider_pressure(3, 0, models, "primary") == ("fallback", "local")
    assert after_provider_pressure(0, 1, models, "primary") == ("backoff", "primary")
    assert after_provider_pressure(0, 3, models, "primary") == ("backoff", "primary")
    assert after_provider_pressure(0, 4, models, "primary") == ("fallback", "local")
    assert after_provider_pressure(1, 0, models, "primary") == ("stay", "primary")
    assert after_provider_pressure(3, 0, models, "local") == ("escalate", "local")


def test_loop_and_budget_stop_keep_the_artifact():
    assert is_loop(("h", "h", "h"), 0) is True
    assert is_loop(("a", "b", "c"), 5) is True
    assert is_loop(("a", "b", "c"), 4) is False
    assert stop_with_progress("STOPPED_BUDGET", "partial brief") == ("STOPPED_BUDGET", "partial brief")
    assert stop_with_progress("STOPPED_LOOP", "partial brief") == ("STOPPED_LOOP", "partial brief")
