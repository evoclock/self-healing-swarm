import os

import psycopg

URL = os.environ["DATABASE_URL"]


def connect(tenant):
    conn = psycopg.connect(URL, autocommit=True)
    if tenant is not None:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    return conn


def ids(conn, table, tenant_id):
    rows = conn.execute(
        f"SELECT id FROM {table} WHERE tenant_id = %s ORDER BY id",
        (tenant_id,),
    ).fetchall()
    return [row[0] for row in rows]


def test_tenant_a_cannot_see_tenant_b_mail_or_runs():
    with connect("t-b") as own:
        own_runs = ids(own, "runs", "t-b")
        assert "m-b-1" in ids(own, "mail", "t-b")
        assert "r-b-1" in own_runs
    with connect("t-a") as other:
        assert ids(other, "mail", "t-b") == []
        assert ids(other, "runs", "t-b") == []
        assert "m-a-1" in ids(other, "mail", "t-a")
        assert "r-a-1" in ids(other, "runs", "t-a")


def test_unset_tenant_sees_no_mail_or_runs():
    with connect(None) as conn:
        mail = conn.execute("SELECT id FROM mail ORDER BY id").fetchall()
        runs = conn.execute("SELECT id FROM runs ORDER BY id").fetchall()
    assert mail == []
    assert runs == []
