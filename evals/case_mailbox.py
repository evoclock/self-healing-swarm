from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

Message = dict[str, Any]
MemoryFact = dict[str, Any]
Document = dict[str, Any]


@dataclass(frozen=True)
class CaseMailbox:
    tenant: str
    messages: tuple[Message, ...]
    memory: tuple[MemoryFact, ...]
    document: Document | None
    planted_messages: tuple[Message, ...] = ()

    def quoted_block(self) -> str:
        lines = ["--- mailbox (quoted data) ---"]
        for message in self.messages:
            lines.append(f"> {message['id']}: {message['body']}")
        for message in self.planted_messages:
            lines.append(f"> {message['id']}: {message['body']}")
        if self.memory:
            lines.append("--- memory (quoted data) ---")
            for fact in self.memory:
                sources = ", ".join(fact.get("source_ids") or [])
                lines.append(f"> fact [{sources}]: {fact['body']}")
        if self.document:
            lines.append("--- document (quoted data) ---")
            lines.append(f"> {self.document['id']}: {self.document['body']}")
        return "\n".join(lines)


def load_case_mailbox(case: dict) -> CaseMailbox:
    messages = tuple(case.get("mailbox") or ())
    memory = tuple(case.get("memory") or ())
    document = case.get("document")
    planted = tuple(case.get("planted_mailbox") or ())
    if not messages and document is None and not planted:
        raise ValueError(f"case {case['id']} has no mailbox emails or document")
    return CaseMailbox(case["tenant"], messages, memory, document, planted)


def tenant_for_mail_id(message_id: str) -> str:
    parts = message_id.split("-")
    if len(parts) >= 2 and parts[0] == "m":
        return f"t-{parts[1]}"
    raise ValueError(message_id)


def fetch_mail_row(conn, message_id: str) -> Message | None:
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant_for_mail_id(message_id),))
    row = conn.execute(
        """
        SELECT id, tenant_id, from_addr, to_addr, subject, body, attachment_id
        FROM mail
        WHERE id = %s
        """,
        (message_id,),
    ).fetchone()
    if row is None:
        return None
    return _message_row(row)


def fetch_document_row(conn, document_id: str, tenant: str) -> Document | None:
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    row = conn.execute(
        "SELECT id, name, body FROM documents WHERE id = %s",
        (document_id,),
    ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "name": row[1], "body": row[2]}


def synthetic_sheet(document_id: str, expected_total: int) -> Document:
    row_total = expected_total // 3
    body = "\n".join(
        [
            "item,amount",
            f"line-a,{row_total}",
            f"line-b,{row_total}",
            f"line-c,{expected_total - 2 * row_total}",
        ]
    )
    return {"id": document_id, "name": f"{document_id}.csv", "body": body}


def synthetic_day_mail(tenant: str, message_id: str) -> Message:
    day = message_id.rsplit("-", 1)[-1]
    domain = {"t-a": "a.example", "t-b": "b.example", "t-c": "c.example", "t-d": "d.example"}.get(
        tenant, "example.com"
    )
    return {
        "id": message_id,
        "tenant_id": tenant,
        "from_addr": f"ada@{domain}",
        "to_addr": f"team@{domain}",
        "subject": f"Day {day} update",
        "body": f"Day {day} notes for {tenant}. No action required beyond the brief.",
        "attachment_id": None,
    }


def resolve_message(conn, tenant: str, message_id: str) -> Message:
    row = fetch_mail_row(conn, message_id)
    if row is not None:
        return row
    if "-day-" in message_id:
        return synthetic_day_mail(tenant, message_id)
    raise KeyError(message_id)


def build_case_payload(conn, case: dict) -> dict:
    tenant = case["tenant"]
    own_ids = list(case["own_ids"])
    document = None
    mailbox: list[Message] = []
    if own_ids and own_ids[0].startswith("doc-"):
        document = fetch_document_row(conn, own_ids[0], tenant)
        if document is None:
            total = case.get("expected_total")
            if total is None:
                raise KeyError(own_ids[0])
            document = synthetic_sheet(own_ids[0], int(total))
    else:
        mailbox = [resolve_message(conn, tenant, message_id) for message_id in own_ids]

    memory: list[MemoryFact] = []
    planted_mailbox: list[Message] = []
    for planted_id in case.get("planted_ids") or ():
        planted = fetch_mail_row(conn, planted_id)
        if planted is None:
            continue
        if case["id"] == "brief-03-cross-tenant-memory":
            memory.append(
                {
                    "kind": "fact",
                    "body": planted["body"],
                    "source_ids": [planted_id],
                    "flagged": False,
                }
            )
        else:
            planted_mailbox.append(planted)

    updated = dict(case)
    if mailbox:
        updated["mailbox"] = mailbox
    if memory:
        updated["memory"] = memory
    if planted_mailbox:
        updated["planted_mailbox"] = planted_mailbox
    if document:
        updated["document"] = document
    return updated


def hydrate_golden_tree(conn, golden_root: Path) -> int:
    count = 0
    for agent_dir in sorted(golden_root.iterdir()):
        if not agent_dir.is_dir():
            continue
        for path in sorted(agent_dir.glob("*.json")):
            case = json.loads(path.read_text())
            if case.get("mailbox"):
                continue
            payload = build_case_payload(conn, case)
            path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")
            count += 1
    return count


def _message_row(row) -> Message:
    return {
        "id": row[0],
        "tenant_id": row[1],
        "from_addr": row[2],
        "to_addr": row[3],
        "subject": row[4],
        "body": row[5],
        "attachment_id": row[6],
    }
