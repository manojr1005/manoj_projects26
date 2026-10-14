#!/usr/bin/env python3
"""Recovery tool: re-enqueue reports stuck in QUEUED/PROCESSING (or FAILED with --include-failed).

Uses GSI1 (no table scan). Safe to run repeatedly: the worker is idempotent.
Usage: TABLE_NAME=... REPORT_QUEUE_URL=... python scripts/requeue_stale_reports.py [--minutes 15] [--include-failed] [--dry-run]
"""
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from repositories import report_repository as repo  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=15, help="only reports older than this")
    ap.add_argument("--include-failed", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=args.minutes)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    statuses = ["QUEUED", "PROCESSING"] + (["FAILED"] if args.include_failed else [])
    n = 0
    for status in statuses:
        for item in repo.list_by_status(status, cutoff):
            print(f"{'would requeue' if args.dry_run else 'requeue'} {item['reportId']} ({status}, since {item['gsi1sk']})")
            if not args.dry_run:
                repo.publish_job(item["userId"], item["reportId"], item.get("statusFilter"))
            n += 1
    print(f"{n} report(s) {'found' if args.dry_run else 'requeued'}")


if __name__ == "__main__":
    main()
