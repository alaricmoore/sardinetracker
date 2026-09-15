"""
Make a new API client entry for config.json.

    python api_clients.py new sardinessync health_sync flare_status --user 1
    python api_clients.py new wearable uv_ingest --user 1 --counter

Prints the entry with a freshly generated secret. Paste it into the
"api_clients" object in config.json on the server, give the same secret to the
client, and restart the app. Nothing is written for you: config.json holds
every other secret too, and a script that edits it is one more way to lose it.
"""

import argparse
import json
import secrets
import sys

from api_signing import PERMITS


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    new = sub.add_parser("new", help="print a new client entry with a fresh secret")
    new.add_argument("name")
    new.add_argument("permits", nargs="+", metavar="PERMIT",
                     help="one or more of: " + ", ".join(sorted(PERMITS)))
    new.add_argument("--user", type=int, action="append", required=True, dest="user_ids",
                     help="a user id this client may act for; repeat for more")
    new.add_argument("--counter", action="store_true",
                     help="the client has no clock and signs a boot id and uptime counter")
    args = parser.parse_args(argv)

    unknown = sorted(set(args.permits) - PERMITS)
    if unknown:
        parser.error(f"unknown permit(s): {', '.join(unknown)}")

    entry = {"secret": secrets.token_hex(32), "permits": sorted(set(args.permits)),
             "user_ids": sorted(set(args.user_ids))}
    if args.counter:
        entry["clock"] = "counter"
    print(f'"{args.name}": {json.dumps(entry)}')
    return 0


if __name__ == "__main__":
    sys.exit(main())
