import json

import psycopg


class ApproverRefused(Exception):
    def __init__(self):
        super().__init__("approver_refused")


def flag_reasons(to_addr: str, domains: tuple[str, ...], attachments: tuple) -> tuple[str, ...]:
    reasons = []
    domain = to_addr.rsplit("@", 1)[-1]
    if domain not in domains:
        reasons.append("external")
    if attachments:
        reasons.append("attachment")
    return tuple(reasons)


def message_id_for(draft_id: str) -> str:
    return f"<{draft_id}@outbox>"


def create_draft(conn, tenant: str, draft_id: str, to_addr: str, subject: str, body: str, domains: tuple[str, ...], attachments: tuple = ()) -> dict:
    reasons = flag_reasons(to_addr, domains, attachments)
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    conn.execute(
        """
        INSERT INTO outbox (draft_id, tenant_id, to_addr, subject, body, attachments, flagged, flag_reasons, state, message_id)
        VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, 'DRAFT', %s)
        ON CONFLICT (draft_id) DO NOTHING
        """,
        (
            draft_id,
            tenant,
            to_addr,
            subject,
            body,
            json.dumps(list(attachments)),
            bool(reasons),
            list(reasons),
            message_id_for(draft_id),
        ),
    )
    return fetch_draft(conn, draft_id)


def approve(conn, tenant: str, draft_id: str, approver_user: str) -> dict:
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    role = conn.execute(
        "SELECT role FROM memberships WHERE tenant_id = %s AND user_id = %s",
        (tenant, approver_user),
    ).fetchone()
    if role is None or role[0] != "approver":
        raise ApproverRefused()
    conn.execute(
        "UPDATE outbox SET state = 'APPROVED' WHERE draft_id = %s AND state = 'DRAFT'",
        (draft_id,),
    )
    return fetch_draft(conn, draft_id)


def deliver(conn, tenant: str, draft_id: str, sink: list, smtp: tuple[str, int] | None = None) -> dict:
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    draft = fetch_draft(conn, draft_id)
    message_id = draft["message_id"]
    already = any(item["message_id"] == message_id for item in sink)
    if draft["state"] == "SENT" or already:
        conn.execute(
            "UPDATE outbox SET state = 'SENT' WHERE draft_id = %s",
            (draft_id,),
        )
        return fetch_draft(conn, draft_id)
    if draft["state"] not in ("APPROVED", "SENDING"):
        raise ValueError(draft["state"])
    conn.execute("UPDATE outbox SET state = 'SENDING' WHERE draft_id = %s", (draft_id,))
    item = {"message_id": message_id, "to": draft["to_addr"], "body": draft["body"]}
    sink.append(item)
    if smtp is not None:
        from outbox.smtp_log import smtp_send

        smtp_send(smtp[0], smtp[1], message_id, draft["to_addr"], draft["body"])
    conn.execute("UPDATE outbox SET state = 'SENT' WHERE draft_id = %s", (draft_id,))
    return fetch_draft(conn, draft_id)


def fetch_draft(conn, draft_id: str) -> dict:
    row = conn.execute(
        """
        SELECT draft_id, to_addr, subject, body, flagged, flag_reasons, state, message_id
        FROM outbox WHERE draft_id = %s
        """,
        (draft_id,),
    ).fetchone()
    return {
        "draft_id": row[0],
        "to_addr": row[1],
        "subject": row[2],
        "body": row[3],
        "flagged": row[4],
        "flag_reasons": list(row[5]),
        "state": row[6],
        "message_id": row[7],
    }


def connect(database_url: str):
    return psycopg.connect(database_url, autocommit=True)
