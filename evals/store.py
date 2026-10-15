import json
import time


def latest_mean(conn, tenant: str, agent_id: str) -> float | None:
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    row = conn.execute(
        """
        SELECT mean FROM eval_records
        WHERE agent_id = %s AND promoted
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (agent_id,),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return float(row[0])


def promoted_hashes(conn, tenant: str, agent_id: str) -> set[str]:
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    rows = conn.execute(
        """
        SELECT config_hash FROM eval_records
        WHERE agent_id = %s AND promoted
        """,
        (agent_id,),
    ).fetchall()
    return {row[0] for row in rows}


def write_eval_record(conn, tenant: str, agent_id: str, result, version: str = "v1") -> str:
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    record_id = f"{result.config_hash[:12]}:{int(time.time())}"
    conn.execute(
        """
        INSERT INTO eval_records (
          id, tenant_id, agent_id, config_hash, golden_version, mean,
          hard_checks_passed, promoted, detail
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        """,
        (
            record_id,
            tenant,
            agent_id,
            result.config_hash,
            version,
            result.mean,
            result.exit_code == 0,
            result.promoted,
            json.dumps({"failures": list(result.failures), "delta": result.delta}),
        ),
    )
    return record_id
