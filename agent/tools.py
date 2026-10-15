import subprocess
import sys

from agent.loop import classify_artifact
from runtime.memory import MemoryRefused, memory_write

__all__ = ["MemoryRefused", "memory_write", "py_exec", "sheet_total", "daily_brief", "finish_brief"]

# Fixed program. The sheet arrives on stdin, so a cell is parsed as a Decimal and never runs as code.
SHEET_PROGRAM = """import sys
from decimal import Decimal, InvalidOperation
total = Decimal(0)
for line in sys.stdin:
    line = line.strip()
    if not line or line.startswith("item,"):
        continue
    cell = line.split(",", 1)[1]
    try:
        total += Decimal(cell)
    except InvalidOperation:
        raise SystemExit(2)
print(total)
"""


def py_exec(code: str, timeout: float, stdin: str | None = None) -> str:
    """Runs in the agent container, never in the proxy."""
    proc = subprocess.run(
        [sys.executable, "-c", code],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "py_exec_failed")
    return proc.stdout.strip()


def sheet_total(body: str, timeout: float = 5, executor=py_exec) -> str:
    return executor(SHEET_PROGRAM, timeout, stdin=body)


def daily_brief(mailbox, tenant: str) -> tuple[str, tuple[str, ...]]:
    messages = mailbox.list_messages(tenant)
    cited = tuple(message["id"] for message in messages)
    quoted = "\n".join(f"> {message['id']}: {message['body']}" for message in messages)
    artifact = "Brief cites " + ", ".join(cited) + "\n" + quoted
    return artifact, cited


def finish_brief(conn, mailbox, tenant: str, agent_id: str, run_id: str) -> str:
    artifact, cited = daily_brief(mailbox, tenant)
    own = frozenset(cited)
    outcome = classify_artifact(artifact, cited, own)
    if outcome == "SUCCEEDED" and cited:
        memory_write(conn, tenant, agent_id, "cursor", cited[-1], (cited[-1],), run_id)
    return outcome
