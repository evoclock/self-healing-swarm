import os
import sys
from pathlib import Path

from agent.client import ProxyClient
from agent.runner import execute
from runtime.config import load_agent
from runtime.runs import connect, open_run


def main() -> int:
    database_url = os.environ["DATABASE_URL"]
    proxy_url = os.environ["PROXY_A_URL"]
    conn = connect(database_url)
    jobs = (
        ("t-a", "daily-brief", "agents/t-a/daily_brief.yaml", "brief"),
        ("t-b", "doc-analyst", "agents/t-b/doc_analyst.yaml", "doc"),
    )
    for tenant, agent_id, config_path, scenario in jobs:
        run_id = f"record-{agent_id}"
        token = f"token-{run_id}"
        client = ProxyClient(proxy_url, token)
        open_run(conn, tenant, run_id, agent_id, token, client.whoami())
        result = execute(client, load_agent(Path(config_path)), run_id, scenario)
        print(f"{scenario} {result.get('state')}", flush=True)
        if result.get("state") not in ("SUCCEEDED", "STOPPED_BUDGET", "STOPPED_LOOP"):
            print(result, flush=True)
            return 1
    probe = ProxyClient(proxy_url, "token-record-loop")
    open_run(conn, "t-a", "record-loop", "daily-brief", "token-record-loop", probe.whoami())
    looped = probe.complete([{"role": "user", "content": "loop probe"}], "primary", 1, 1.0)
    local = probe.complete([{"role": "user", "content": "Reply with the single word pong."}], "local", 2, 1.0)
    print(f"loop {looped.get('status')} local {local.get('text')}", flush=True)
    if local.get("status") != 200:
        print(local, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
