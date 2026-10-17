from runtime.scheduler import Job, schedule


def _active(placed, minute):
    return [item for item in placed if not item.escalated and item.start <= minute < item.start + item.duration]


def test_two_hundred_ten_minute_agents_escalate_only_predicted_misses():
    jobs = tuple(Job(f"job-{index:03d}", f"t-{index % 20}", 10, deadline=90) for index in range(200))
    caps = {f"t-{index}": 2 for index in range(20)}
    placed = schedule(jobs, caps, host_cap=8, window=90)
    started = [item for item in placed if not item.escalated]
    escalated = [item for item in placed if item.escalated]
    assert len(placed) == 200
    assert all(item.start + item.duration <= 90 for item in started)
    # 8 slots of 10 minutes in a 90-minute window hold 72 runs; the other 128 cannot meet the deadline.
    assert len(started) == 72 and len(escalated) == 128
    for minute in range(90):
        active = _active(placed, minute)
        assert len(active) <= 8
        for tenant in caps:
            assert sum(1 for item in active if item.tenant_id == tenant) <= caps[tenant]


def test_two_hundred_briefs_due_together_spread_across_the_window():
    jobs = tuple(Job(f"job-{index:03d}", f"t-{index % 20}", 3, deadline=90) for index in range(200))
    caps = {f"t-{index}": 2 for index in range(20)}
    placed = schedule(jobs, caps, host_cap=8, window=90)
    assert not [item for item in placed if item.escalated]
    starts = sorted(item.start for item in placed)
    assert starts[0] == 0 and starts[-1] >= 60
    assert max(len(_active(placed, minute)) for minute in range(90)) <= 8


def test_earliest_deadline_runs_first_regardless_of_id():
    jobs = (Job("a", "t-1", 10, deadline=60), Job("b", "t-1", 10, deadline=20), Job("c", "t-1", 10, deadline=40))
    placed = {item.job_id: item for item in schedule(jobs, {"t-1": 1}, host_cap=1, window=90)}
    assert placed["b"].start == 0
    assert placed["c"].start == 10
    assert placed["a"].start == 20


def test_a_job_that_cannot_meet_its_deadline_is_escalated_without_blocking_others():
    jobs = (Job("late", "t-1", 10, deadline=5), Job("ok", "t-1", 10, deadline=30))
    placed = {item.job_id: item for item in schedule(jobs, {"t-1": 1}, host_cap=1, window=90)}
    assert placed["late"].escalated
    assert not placed["ok"].escalated and placed["ok"].start == 0


def test_provider_concurrency_is_a_limit():
    jobs = tuple(Job(f"j{index}", f"t-{index}", 10, deadline=90, provider="primary") for index in range(6))
    placed = schedule(jobs, {f"t-{index}": 1 for index in range(6)}, host_cap=8, window=90, provider_caps={"primary": 2})
    assert not [item for item in placed if item.escalated]
    assert max(len(_active(placed, minute)) for minute in range(90)) == 2


def test_provider_tokens_per_minute_are_split_across_tenants():
    # 2000 tokens a minute on primary, two tenants: 1000 each. t-a's backlog cannot use t-b's share.
    jobs = tuple(Job(f"a{index}", "t-a", 1, deadline=90, tokens=1000) for index in range(5)) + (
        Job("b0", "t-b", 1, deadline=90, tokens=1000),
    )
    placed = {item.job_id: item for item in schedule(jobs, {"t-a": 8, "t-b": 8}, host_cap=8, window=90, provider_tpm={"primary": 2000})}
    starts_a = sorted(placed[f"a{index}"].start for index in range(5))
    assert starts_a == [0, 1, 2, 3, 4]
    assert placed["b0"].start == 0


def test_a_tenant_short_of_tokens_waits_and_escalates_only_at_its_deadline():
    jobs = (Job("a0", "t-a", 1, deadline=90, tokens=1000), Job("a1", "t-a", 1, deadline=1, tokens=1000))
    placed = {item.job_id: item for item in schedule(jobs, {"t-a": 8}, host_cap=8, window=90, provider_tpm={"primary": 1000})}
    assert placed["a1"].start == 0
    assert placed["a0"].start == 1
