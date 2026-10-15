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
