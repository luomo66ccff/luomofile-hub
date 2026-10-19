import json
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import init_db
from app.subscriptions import reconcile_subscription_identities


def main() -> int:
    init_db()
    result = reconcile_subscription_identities()
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
