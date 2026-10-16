import os
import threading
from decimal import Decimal

import psycopg

from runtime.policy import RunPolicy
from runtime.runs import open_run
from runtime.watcher import watch_once

URL = os.environ["DATABASE_URL"]


def expire(run_id, tenant="t-a"):
    with psycopg.connect(URL, autocommit=True) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
        conn.execute("UPDATE runs SET lease_until = now() - interval '1 minute' WHERE id = %s", (run_id,))


def state(run_id, tenant="t-a"):
    with psycopg.connect(URL, autocommit=True) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
        return conn.execute("SELECT state, resume_count FROM runs WHERE id = %s", (run_id,)).fetchone()


def opened(run_id, tenant="t-a", agent="daily-brief"):
    with psycopg.connect(URL, autocommit=True) as conn:
        open_run(conn, tenant, run_id, agent, f"tok-{run_id}", "10.9.9.8", RunPolicy(Decimal("1"), frozenset()))


def test_an_expired_lease_is_resumed_and_a_live_one_is_left_alone():
    opened("watch-a")
    opened("watch-live", tenant="t-b", agent="daily-brief")
    with psycopg.connect(URL, autocommit=True) as conn:
        conn.execute("SELECT set_config('app.tenant_id', 't-b', false)")
        conn.execute("UPDATE runs SET lease_until = now() + interval '1 hour' WHERE id = 'watch-live'")
    expire("watch-a")
    resumed = []
    with psycopg.connect(URL, autocommit=True) as conn:
        watch_once(conn, lambda tenant, run_id: resumed.append((tenant, run_id)))
    assert ("t-a", "watch-a") in resumed
    assert ("t-b", "watch-live") not in resumed
    assert state("watch-a") == ("RUNNING", 1)


def test_the_third_expiry_escalates_instead_of_resuming():
    opened("watch-esc")
    resumed = []
    with psycopg.connect(URL, autocommit=True) as conn:
        for _ in range(3):
            expire("watch-esc")
            watch_once(conn, lambda tenant, run_id: resumed.append(run_id))
    assert resumed.count("watch-esc") == 2
    assert state("watch-esc") == ("ESCALATED", 3)


def test_two_watchers_on_one_expired_run_resume_it_exactly_once():
    opened("watch-race")
    expire("watch-race")
    resumed = []
    barrier = threading.Barrier(2)

    def watcher():
        with psycopg.connect(URL, autocommit=True) as conn:
            barrier.wait()
            watch_once(conn, lambda tenant, run_id: resumed.append(run_id), only={"watch-race"})

    threads = [threading.Thread(target=watcher) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert resumed == ["watch-race"]
    assert state("watch-race") == ("RUNNING", 1)
    with psycopg.connect(URL, autocommit=True) as conn:
        conn.execute("SELECT set_config('app.tenant_id', 't-a', false)")
        events = conn.execute("SELECT event FROM run_events WHERE run_id = 'watch-race'").fetchall()
    assert events == [("lease_resumed",)]
