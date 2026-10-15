from runtime.scheduler import Job, schedule


def test_two_hundred_agents_stay_inside_host_and_tenant_caps():
    jobs = tuple(Job(f"job-{index:03d}", f"t-{index % 20}", 10) for index in range(200))
    caps = {f"t-{index}": 2 for index in range(20)}
    placed = schedule(jobs, caps, host_cap=8, window=90)
    started = [item for item in placed if not item.escalated]
    escalated = [item for item in placed if item.escalated]
    assert len(placed) == 200
    assert started
    assert escalated
    assert min(item.start for item in started) != max(item.start for item in started)
    for minute in range(90):
        active = [item for item in started if item.start <= minute < item.start + 10]
        assert len(active) <= 8
        for tenant in caps:
            assert sum(1 for item in active if item.tenant_id == tenant) <= caps[tenant]
    print(f"escalated {len(escalated)} of 200")
