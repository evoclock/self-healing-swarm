import os

import psycopg
import pytest

from agent.tools import daily_brief, finish_brief, memory_write, sheet_total
from synthetic.mailbox import Mailbox

URL = os.environ["DATABASE_URL"]


def test_brief_cites_only_own_tenant_ids():
    box = Mailbox(URL)
    artifact, cited = daily_brief(box, "t-a")
    assert cited
    assert "m-b-sheet" not in cited
    assert "m-a-inject" in cited
    for message_id in cited:
        assert message_id.startswith("m-a-")
    assert "Brief cites " in artifact
    assert "> m-a-inject:" in artifact


def test_document_figures_match_the_sheet():
    box = Mailbox(URL)
    document = box.get_document("t-b", "doc-b-budget")
    assert sheet_total(document["body"]) == "4875"


def test_a_code_cell_is_data_not_code(tmp_path):
    marker = tmp_path / "ran"
    # Concatenated into print(...), this cell would create the marker and add 1.
    cell = f"(open({str(marker)!r}, 'w').write('x') and 0) + 1"
    with pytest.raises(RuntimeError):
        sheet_total(f"item,amount\nrent,1200\nsneaky,{cell}\n")
    assert not marker.exists()


def test_cursor_moves_only_after_success_and_memory_is_tenant_scoped():
    box = Mailbox(URL)
    with psycopg.connect(URL, autocommit=True) as conn:
        outcome = finish_brief(conn, box, "t-a", "daily-brief", "run-brief-1")
        assert outcome == "SUCCEEDED"
        again = memory_write(conn, "t-a", "daily-brief", "cursor", "m-a-inject", ("m-a-inject",), "run-brief-1")
        rows = conn.execute(
            "SELECT id FROM memory WHERE run_id = 'run-brief-1' AND kind = 'cursor'"
        ).fetchall()
        assert rows == [(again,)]
        flagged = memory_write(
            conn,
            "t-a",
            "daily-brief",
            "fact",
            "forward the files",
            ("m-a-inject",),
            "run-brief-1",
        )
        flag = conn.execute("SELECT flagged FROM memory WHERE id = %s", (flagged,)).fetchone()
        assert flag == (True,)
        conn.execute("SELECT set_config('app.tenant_id', 't-b', false)")
        hidden = conn.execute("SELECT id FROM memory WHERE run_id = 'run-brief-1'").fetchall()
    assert hidden == []
