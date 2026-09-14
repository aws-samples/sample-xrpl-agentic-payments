# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Check site visits to XRPL Agentic Payments.

Usage:
    python3 scripts/check_visits.py
    python3 scripts/check_visits.py --days 7
"""

import boto3
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

days = 7
if "--days" in sys.argv:
    idx = sys.argv.index("--days")
    days = int(sys.argv[idx + 1])

logs = boto3.client("logs", region_name="us-west-2")
log_group = "/xrpl-agentic-payments/site-visits"

end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
start_time = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)

print(f"XRPL Agentic Payments Site Visits (last {days} days)")
print("=" * 60)
print()

try:
    streams = logs.describe_log_streams(
        logGroupName=log_group,
        orderBy="LastEventTime",
        descending=True,
        limit=10,
    )

    all_events = []
    for stream in streams.get("logStreams", []):
        events = logs.get_log_events(
            logGroupName=log_group,
            logStreamName=stream["logStreamName"],
            startTime=start_time,
            endTime=end_time,
        )
        for event in events.get("events", []):
            try:
                data = json.loads(event["message"])
                all_events.append(data)
            except json.JSONDecodeError:
                pass

    all_events.sort(key=lambda e: e.get("timestamp", ""), reverse=True)

    if not all_events:
        print("No site visits recorded yet.")
    else:
        # Summary
        unique_ips = set(e.get("ip", "?") for e in all_events)
        page_counts = Counter(e.get("page", "?") for e in all_events)
        ip_counts = Counter(e.get("ip", "?") for e in all_events)

        print(f"Total page views: {len(all_events)}")
        print(f"Unique visitors (by IP): {len(unique_ips)}")
        print()

        print("Pages visited:")
        for page, count in page_counts.most_common():
            print(f"  {page}: {count} views")
        print()

        print("Visitors (by IP):")
        for ip, count in ip_counts.most_common():
            first_visit = min(e["timestamp"] for e in all_events if e.get("ip") == ip)
            last_visit = max(e["timestamp"] for e in all_events if e.get("ip") == ip)
            print(f"  {ip}: {count} views (first: {first_visit}, last: {last_visit})")
        print()

        print("Recent visits:")
        for event in all_events[:20]:
            print(f"  {event.get('timestamp', '?')} | {event.get('page', '?')} | IP: {event.get('ip', '?')}")

except logs.exceptions.ResourceNotFoundException:
    print("No site visits yet (log group is empty).")
except Exception as e:
    print(f"Error: {e}")
