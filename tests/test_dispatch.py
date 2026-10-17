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


def test_dispatch_splits_provider_tokens_per_tenant():
    from runtime.dispatch import dispatch_once
    from runtime.scheduler import Job

    jobs = [Job("a1", "t-a", 1, 30, tokens=1000), Job("a2", "t-a", 1, 30, tokens=1000), Job("b1", "t-b", 1, 30, tokens=1000)]
    started = []
    placements = dispatch_once(jobs, {"t-a": 2, "t-b": 2}, lambda job: started.append(job.job_id), lambda job: None, provider_tpm={"primary": 3000})
    assert sorted(started) == ["a1", "b1"]
    assert {p.job_id: p.start for p in placements}["a2"] == 1


def test_jobs_for_today_carry_the_token_limit():
    from datetime import datetime
    from pathlib import Path

    from runtime.dispatch import jobs_for_today

    jobs = jobs_for_today(Path("agents"), datetime(2026, 10, 17, 6, 0))
    assert all(job.tokens == config.limits.max_tokens for job, config, _path in jobs.values())
    assert all(job.requests == config.limits.max_steps for job, config, _path in jobs.values())


def test_main_reads_provider_cap_and_tpm(monkeypatch):
    import runtime.dispatch as d

    seen = {}

    def fake_dispatch_once(jobs, caps, launch, escalate, **kwargs):
        seen.update(kwargs)
        raise SystemExit

    class Conn:
        def execute(self, *a, **k):
            class R:
                def fetchone(self):
                    return (1,)
            return R()

    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setenv("PROVIDER_CAP", "4")
    monkeypatch.setenv("PROVIDER_TPM", "9000")
    monkeypatch.setenv("PROVIDER_RPM", "60")
    monkeypatch.setattr(d, "reap_exited", lambda conn: [])
    monkeypatch.setattr(d, "connect", lambda url: Conn())
    monkeypatch.setattr(d, "dispatch_once", fake_dispatch_once)
    import pytest
    with pytest.raises(SystemExit):
        d.main()
    assert seen == {"provider_caps": {"primary": 4}, "provider_tpm": {"primary": 9000}, "provider_rpm": {"primary": 60}}
