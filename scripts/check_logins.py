# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Check who has logged into XRPL Agentic Payments.

Usage:
    python3 scripts/check_logins.py
    python3 scripts/check_logins.py --days 7
"""

import boto3
import json
import sys
from datetime import datetime, timedelta, timezone

days = 7
if "--days" in sys.argv:
    idx = sys.argv.index("--days")
    days = int(sys.argv[idx + 1])

logs = boto3.client("logs", region_name="us-west-2")
log_group = "/xrpl-agentic-payments/login-activity"

end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
start_time = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)

print(f"XRPL Agentic Payments Login Activity (last {days} days)")
print("=" * 60)
print()

try:
    # Get all streams
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

    # Sort by timestamp
    all_events.sort(key=lambda e: e.get("timestamp", ""), reverse=True)

    if not all_events:
        print("No login events found.")
    else:
        # Summary
        unique_emails = set(e.get("email", "?") for e in all_events if e.get("success"))
        print(f"Total logins: {len(all_events)}")
        print(f"Successful: {sum(1 for e in all_events if e.get('success'))}")
        print(f"Failed: {sum(1 for e in all_events if not e.get('success'))}")
        print(f"Unique users: {len(unique_emails)}")
        print()

        # Per-user breakdown
        print("By user:")
        from collections import Counter
        user_counts = Counter(e.get("email", "?") for e in all_events if e.get("success"))
        for email, count in user_counts.most_common():
            last_login = next(
                e["timestamp"] for e in all_events if e.get("email") == email and e.get("success")
            )
            print(f"  {email}: {count} logins (last: {last_login})")
        print()

        # Recent activity
        print("Recent events:")
        for event in all_events[:15]:
            status = "OK" if event.get("success") else "FAIL"
            print(f"  {event.get('timestamp', '?')} | {event.get('email', '?')} | {status} | IP: {event.get('ip', '?')}")

except logs.exceptions.ResourceNotFoundException:
    print("No login activity yet (log group is empty).")
except Exception as e:
    print(f"Error: {e}")
