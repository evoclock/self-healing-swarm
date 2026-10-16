from runtime.dispatch import dispatch_once
from runtime.scheduler import Job


def test_dispatch_starts_what_is_due_now_by_deadline_and_escalates_predicted_misses():
    jobs = [
        Job("later", "t-a", 10, deadline=80),
        Job("soon", "t-b", 10, deadline=15),
        Job("missed", "t-c", 10, deadline=5),
    ]
    launched, escalated = [], []
    dispatch_once(jobs, {"t-a": 1, "t-b": 1, "t-c": 1}, launched.append, escalated.append, host_cap=1)
    assert [job.job_id for job in launched] == ["soon"]
    assert [job.job_id for job in escalated] == ["missed"]


def test_a_job_placed_later_is_left_for_a_later_pass():
    jobs = [Job("a", "t-a", 10, deadline=30), Job("b", "t-a", 10, deadline=30)]
    launched, escalated = [], []
    dispatch_once(jobs, {"t-a": 1}, launched.append, escalated.append, host_cap=8)
    assert [job.job_id for job in launched] == ["a"]
    assert escalated == []
