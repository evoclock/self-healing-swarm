import argparse
import os
import sys
from pathlib import Path

import psycopg

from evals.gate import config_hash, gate, load_golden, load_recordings
from evals.store import latest_mean, write_eval_record
from runtime.config import load_agent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", required=True)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--golden", type=Path, default=Path("evals/golden"))
    parser.add_argument("--recordings", type=Path, default=Path("recordings/evals"))
    parser.add_argument("--baseline", type=float, default=None)
    args = parser.parse_args(argv)
    config = load_agent(args.candidate)
    digest = config_hash(config)
    cases = load_golden(args.golden, args.agent)
    try:
        outputs = load_recordings(args.recordings, digest, cases)
    except Exception as exc:
        print(exc)
        return 1
    baseline = args.baseline
    database_url = os.environ.get("DATABASE_URL")
    if baseline is None and database_url:
        with psycopg.connect(database_url, autocommit=True) as conn:
            baseline = latest_mean(conn, config.tenant_id, config.id)
    result = gate(cases, outputs, baseline, digest)
    if database_url:
        with psycopg.connect(database_url, autocommit=True) as conn:
            write_eval_record(conn, config.tenant_id, config.id, result)
    if result.failures:
        print(f"delta {result.delta}")
        for failure in result.failures:
            print(failure)
    else:
        print(f"promoted mean {result.mean} delta {result.delta} hash {digest}")
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
