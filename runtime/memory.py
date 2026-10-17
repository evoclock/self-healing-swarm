# Facts (including run summaries) live FACT_TTL_DAYS. Each agent keeps at most MAX_FACTS of them and MAX_FACT_BYTES
# in total; writing past either evicts the oldest. One fact is at most MAX_FACT_CHARS.
FACT_TTL_DAYS = 30
MAX_FACTS = 20
MAX_FACT_BYTES = 4000
MAX_FACT_CHARS = 500
SUMMARY_CHARS = 200
# Run summaries (kind 'summary') have their own budget: the last MAX_SUMMARIES, never counted against facts.
MAX_SUMMARIES = 5
EXPIRING = ("fact", "summary")


class MemoryRefused(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def memory_write(conn, tenant: str, agent_id: str, kind: str, body: str, source_ids: tuple[str, ...], run_id: str) -> str:
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    for source_id in source_ids:
        row = conn.execute("SELECT id FROM mail WHERE id = %s", (source_id,)).fetchone()
        if row is None:
            raise MemoryRefused("source_not_in_tenant")
    if kind == "fact" and len(body) > MAX_FACT_CHARS:
        raise MemoryRefused("too_large")
    flagged = _looks_like_instruction(body)
    # An expired copy is not a duplicate: drop it so the same text can be stored again with a fresh expiry.
    conn.execute(
        """
        DELETE FROM memory WHERE tenant_id = %s AND agent_id = %s AND run_id = %s AND kind = %s AND body = %s
          AND expires_at <= now()
        """,
        (tenant, agent_id, run_id, kind, body),
    )
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
    expires = f"{FACT_TTL_DAYS} days" if kind in EXPIRING else None
    conn.execute(
        """
        INSERT INTO memory (id, tenant_id, agent_id, kind, body, source_ids, run_id, flagged, expires_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now() + %s::interval)
        """,
        (memory_id, tenant, agent_id, kind, body, list(source_ids), run_id, flagged, expires),
    )
    if kind == "fact":
        evict(conn, tenant, agent_id)
    elif kind == "summary":
        evict_summaries(conn, tenant, agent_id)
    return memory_id


def evict_summaries(conn, tenant: str, agent_id: str) -> int:
    """Keep the agent's last MAX_SUMMARIES run summaries (and none expired)."""
    rows = conn.execute(
        """
        SELECT id, expires_at <= now() FROM memory WHERE tenant_id = %s AND agent_id = %s AND kind = 'summary'
        ORDER BY created_at DESC, id DESC
        """,
        (tenant, agent_id),
    ).fetchall()
    drop = [memory_id for n, (memory_id, expired) in enumerate(rows) if n >= MAX_SUMMARIES or expired]
    if drop:
        conn.execute("DELETE FROM memory WHERE id = ANY(%s)", (drop,))
    return len(drop)


def evict(conn, tenant: str, agent_id: str) -> int:
    """Drop expired facts, then the oldest until the agent is within MAX_FACTS and MAX_FACT_BYTES."""
    conn.execute(
        "DELETE FROM memory WHERE tenant_id = %s AND agent_id = %s AND kind = 'fact' AND expires_at <= now()",
        (tenant, agent_id),
    )
    rows = conn.execute(
        """
        SELECT id, octet_length(body) FROM memory
        WHERE tenant_id = %s AND agent_id = %s AND kind = 'fact'
        ORDER BY created_at DESC, id DESC
        """,
        (tenant, agent_id),
    ).fetchall()
    kept, total, drop = 0, 0, []
    for memory_id, size in rows:
        if kept < MAX_FACTS and total + size <= MAX_FACT_BYTES:
            kept += 1
            total += size
        else:
            drop.append(memory_id)
    if drop:
        conn.execute("DELETE FROM memory WHERE id = ANY(%s)", (drop,))
    return len(drop)


def run_summary(run_id: str, artifact: str) -> str:
    """A short summary of a successful run, kept as a fact: the brief's text (or first line), one line, capped."""
    import json

    text = artifact.split("\ncites:")[0].strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and parsed.get("text"):
            text = str(parsed["text"])
    except ValueError:
        pass
    text = " ".join(text.split())
    return f"Run {run_id}: {text[:SUMMARY_CHARS]}"


def _looks_like_instruction(body: str) -> bool:
    lowered = body.lower()
    return any(phrase in lowered for phrase in ("forward", "ignore previous", "send all"))
