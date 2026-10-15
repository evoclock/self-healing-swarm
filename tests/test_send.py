import json
import os

import psycopg
import pytest

from outbox.send import ApproverRefused, approve, create_draft, deliver, fetch_draft

URL = os.environ["DATABASE_URL"]


def setup_approvers(conn):
    conn.execute("SELECT set_config('app.tenant_id', 't-a', false)")
    conn.execute("DELETE FROM outbox WHERE draft_id IN ('draft-ext', 'draft-local')")
    conn.execute(
        """
        INSERT INTO memberships (tenant_id, user_id, role)
        VALUES ('t-a', 'approver-a', 'approver')
        ON CONFLICT DO NOTHING
        """
    )
    conn.execute("SELECT set_config('app.tenant_id', 't-b', false)")
    conn.execute(
        """
        INSERT INTO memberships (tenant_id, user_id, role)
        VALUES ('t-b', 'approver-b', 'approver')
        ON CONFLICT DO NOTHING
        """
    )


def test_external_draft_is_flagged_and_double_send_delivers_once():
    sink = []
    with psycopg.connect(URL, autocommit=True) as conn:
        setup_approvers(conn)
        draft = create_draft(
            conn,
            "t-a",
            "draft-ext",
            "outside@evil.example",
            "files",
            "See attached.",
            attachments=(),
        )
        assert draft["flagged"] is True
        assert draft["flag_reasons"] == ["external"]
        assert draft["state"] == "DRAFT"
        approved = approve(conn, "t-a", "draft-ext", "approver-a")
        assert approved["state"] == "APPROVED"
        first = deliver(conn, "t-a", "draft-ext", sink)
        second = deliver(conn, "t-a", "draft-ext", sink)
    assert first["state"] == "SENT"
    assert second["state"] == "SENT"
    assert first["message_id"] == "<draft-ext@outbox>"
    assert sink == [{"message_id": "<draft-ext@outbox>", "to": "outside@evil.example", "body": "See attached."}]


def test_smtp_failure_leaves_sending_and_the_log_stops_a_second_send(tmp_path):
    log = tmp_path / "smtp.log"
    with psycopg.connect(URL, autocommit=True) as conn:
        setup_approvers(conn)
        conn.execute("SELECT set_config('app.tenant_id', 't-a', false)")
        conn.execute("DELETE FROM outbox WHERE draft_id = 'draft-retry'")
        create_draft(conn, "t-a", "draft-retry", "team@a.example", "notes", "Hello.")
        approve(conn, "t-a", "draft-retry", "approver-a")
        sink = []
        with pytest.raises(OSError):
            deliver(conn, "t-a", "draft-retry", sink, ("127.0.0.1", 9), log)
        assert fetch_draft(conn, "draft-retry")["state"] == "SENDING"
        assert sink == []
        log.write_text(json.dumps({"message_id": "<draft-retry@outbox>", "to": "team@a.example", "body": "Hello."}) + "\n")
        fresh = []
        sent = deliver(conn, "t-a", "draft-retry", fresh, ("127.0.0.1", 9), log)
    assert sent["state"] == "SENT"
    assert fresh == []


def test_approver_from_another_tenant_is_refused():
    with psycopg.connect(URL, autocommit=True) as conn:
        setup_approvers(conn)
        create_draft(
            conn,
            "t-a",
            "draft-local",
            "team@a.example",
            "notes",
            "Hello.",
        )
        with pytest.raises(ApproverRefused):
            approve(conn, "t-a", "draft-local", "approver-b")
