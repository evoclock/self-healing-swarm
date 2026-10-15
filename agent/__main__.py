import json
import os
import sys
from pathlib import Path

from agent.client import ProxyClient
from agent.runner import execute
from runtime.config import load_agent


def main() -> int:
    client = ProxyClient(os.environ["PROXY_URL"], os.environ["RUN_TOKEN"])
    pause = os.environ.get("DRILL_PAUSE_AFTER_STEP")
    if pause:
        client.pause_after = int(pause)
        client.pause_after_seconds = int(os.environ.get("DRILL_PAUSE_SECONDS", "300"))
    config = load_agent(Path(os.environ["AGENT_CONFIG"]))
    scenario = os.environ.get("SCENARIO")
    if not scenario:
        scenario = "doc" if config.id == "doc-analyst" else "brief"
    result = execute(client, config, os.environ["RUN_ID"], scenario)
    print(json.dumps(result), flush=True)
    return 0 if result.get("state") in ("SUCCEEDED", "STOPPED_BUDGET", "STOPPED_LOOP", "FAILED", "ESCALATED") else 1


if __name__ == "__main__":
    sys.exit(main())
