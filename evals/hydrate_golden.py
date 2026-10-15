import os
import sys
from pathlib import Path

import psycopg

from evals.case_mailbox import hydrate_golden_tree


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is required", file=sys.stderr)
        return 1
    root = Path(__file__).resolve().parents[1] / "evals" / "golden"
    with psycopg.connect(url, autocommit=True) as conn:
        updated = hydrate_golden_tree(conn, root)
    print(f"hydrated {updated} golden cases with mailbox emails")
    return 0


if __name__ == "__main__":
    sys.exit(main())
