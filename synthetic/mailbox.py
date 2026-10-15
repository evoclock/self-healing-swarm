import psycopg


class Mailbox:
    def __init__(self, database_url: str):
        self.database_url = database_url

    def list_messages(self, tenant: str) -> list[dict]:
        with self._connect(tenant) as conn:
            rows = conn.execute(
                """
                SELECT id, from_addr, to_addr, subject, body, attachment_id
                FROM mail
                WHERE tenant_id = %s
                ORDER BY id
                """,
                (tenant,),
            ).fetchall()
        return [_message(row) for row in rows]

    def get_message(self, tenant: str, message_id: str) -> dict | None:
        with self._connect(tenant) as conn:
            row = conn.execute(
                """
                SELECT id, from_addr, to_addr, subject, body, attachment_id
                FROM mail
                WHERE tenant_id = %s AND id = %s
                """,
                (tenant, message_id),
            ).fetchone()
        if row is None:
            return None
        return _message(row)

    def get_document(self, tenant: str, document_id: str) -> dict | None:
        with self._connect(tenant) as conn:
            row = conn.execute(
                """
                SELECT id, name, body
                FROM documents
                WHERE tenant_id = %s AND id = %s
                """,
                (tenant, document_id),
            ).fetchone()
        if row is None:
            return None
        return {"id": row[0], "name": row[1], "body": row[2]}

    def _connect(self, tenant: str):
        conn = psycopg.connect(self.database_url, autocommit=True)
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
        return conn


def _message(row) -> dict:
    return {
        "id": row[0],
        "from_addr": row[1],
        "to_addr": row[2],
        "subject": row[3],
        "body": row[4],
        "attachment_id": row[5],
    }
