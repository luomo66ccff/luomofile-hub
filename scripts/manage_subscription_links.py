import argparse
import json
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import init_db
from app.subscriptions import (
    create_subscription_link,
    list_subscription_links,
    protect_subscription_file,
    revoke_subscription_link,
    subscription_files,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage protected VPN subscription links.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="Create a link and print its URL once.")
    create.add_argument("--file-id", required=True)
    create.add_argument("--label", required=True)
    create.add_argument("--days", type=int, default=30)

    revoke = subparsers.add_parser("revoke", help="Revoke an issued link by database ID.")
    revoke.add_argument("id", type=int)

    protect = subparsers.add_parser("protect", help="Disable the legacy public URL for a subscription file.")
    protect.add_argument("--file-id", required=True)

    subparsers.add_parser("list", help="List grants without revealing link secrets.")
    subparsers.add_parser("files", help="List eligible subscription files.")

    args = parser.parse_args()
    init_db()
    if args.command == "create":
        result = create_subscription_link(args.file_id, args.label, None, args.days)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if args.command == "revoke":
        print(json.dumps({"revoked": revoke_subscription_link(args.id)}))
        return 0
    if args.command == "protect":
        print(json.dumps({"protected": protect_subscription_file(args.file_id)}))
        return 0
    if args.command == "files":
        print(json.dumps(subscription_files(), ensure_ascii=False))
        return 0
    print(json.dumps(list_subscription_links(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
