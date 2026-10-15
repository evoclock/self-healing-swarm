import json
from pathlib import Path

from outbox.smtp_log import serve_smtp, smtp_send


def test_smtp_log_keeps_one_message(tmp_path: Path):
    path = tmp_path / "smtp.log"
    server, _thread = serve_smtp(path, "127.0.0.1", 0)
    port = server.getsockname()[1]
    try:
        smtp_send("127.0.0.1", port, "<draft-1@outbox>", "ada@a.example", "hello")
    finally:
        server.close()
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["message_id"] == "<draft-1@outbox>"
