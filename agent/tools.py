import subprocess
import sys

import psycopg

from agent.loop import classify_artifact
from synthetic.mailbox import Mailbox


class MemoryRefused(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def py_exec(code: str, timeout: float) -> str:
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "py_exec_failed")
    return proc.stdout.strip()


def sheet_total(body: str, timeout: float = 5) -> str:
    amounts = []
    for line in body.splitlines():
        if not line or line.startswith("item,"):
            continue
        amounts.append(line.split(",", 1)[1])
    code = "print(" + "+".join(amounts) + ")"
    return py_exec(code, timeout)


def daily_brief(mailbox: Mailbox, tenant: str) -> tuple[str, tuple[str, ...]]:
    messages = mailbox.list_messages(tenant)
    cited = tuple(message["id"] for message in messages)
    quoted = "\n".join(f"> {message['id']}: {message['body']}" for message in messages)
    artifact = "Brief cites " + ", ".join(cited) + "\n" + quoted
    return artifact, cited


def finish_brief(conn, mailbox: Mailbox, tenant: str, agent_id: str, run_id: str) -> str:
    artifact, cited = daily_brief(mailbox, tenant)
    own = frozenset(cited)
    outcome = classify_artifact(artifact, cited, own)
    if outcome == "SUCCEEDED" and cited:
        memory_write(conn, tenant, agent_id, "cursor", cited[-1], (cited[-1],), run_id)
    return outcome


def memory_write(conn, tenant: str, agent_id: str, kind: str, body: str, source_ids: tuple[str, ...], run_id: str) -> str:
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    for source_id in source_ids:
        row = conn.execute("SELECT id FROM mail WHERE id = %s", (source_id,)).fetchone()
        if row is None:
            raise MemoryRefused("source_not_in_tenant")
    flagged = _looks_like_instruction(body)
    existing = conn.execute(
        """
        SELECT id FROM memory
        WHERE tenant_id = %s AND agent_id = %s AND run_id = %s AND kind = %s AND body = %s
        """,
        (tenant, agent_id, run_id, kind, body),
    ).fetchone()
    if existing:
        return existing[0]
    memory_id = f"{run_id}:{kind}:{body}"[:80]
    conn.execute(
        """
        INSERT INTO memory (id, tenant_id, agent_id, kind, body, source_ids, run_id, flagged)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (memory_id, tenant, agent_id, kind, body, list(source_ids), run_id, flagged),
    )
    return memory_id


def _looks_like_instruction(body: str) -> bool:
    lowered = body.lower()
    return any(phrase in lowered for phrase in ("forward", "ignore previous", "send all"))
