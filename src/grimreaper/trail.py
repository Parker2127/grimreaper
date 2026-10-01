"""CloudTrail: what was created or deleted recently? Explains charges that outlive their resources.

Cost Explorer lags 24-48 hours, so a resource deleted this morning still shows up in "last 3 days" billing.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError

DELETE_PREFIXES = ("Delete", "Terminate", "Release", "Deregister", "Disable", "Remove")
CREATE_PREFIXES = ("Create", "Run", "Allocate", "Register", "Copy", "Put", "Start")
NOISY = {"CreateLogStream", "PutLogEvents", "CreateSession", "StartQuery", "PutMetricData", "CreateGrant"}


def _classify(name: str) -> str | None:
    if name in NOISY:
        return None
    if name.startswith(DELETE_PREFIXES):
        return "deleted"
    if name.startswith(CREATE_PREFIXES):
        return "created"
    return None


def _region_events(session: boto3.Session, region: str, start: datetime, limit: int) -> list[dict]:
    ct = session.client("cloudtrail", region_name=region)
    out = []
    pages = ct.get_paginator("lookup_events").paginate(
        LookupAttributes=[{"AttributeKey": "ReadOnly", "AttributeValue": "false"}],
        StartTime=start, PaginationConfig={"MaxItems": 2000, "PageSize": 50},
    )
    for page in pages:
        for e in page["Events"]:
            action = _classify(e["EventName"])
            if action is None:
                continue
            out.append({
                "time": e["EventTime"].isoformat(timespec="minutes"),
                "region": region,
                "action": action,
                "event": e["EventName"],
                "source": e.get("EventSource", "").removesuffix(".amazonaws.com"),
                "resources": [r.get("ResourceName", "") for r in e.get("Resources", [])][:5] or _request_ids(e),
            })
            if len(out) >= limit:
                return out
    return out


ID_FIELDS = ("name", "id", "Id", "Name", "bucketName", "functionName", "loadBalancerArn", "dBInstanceIdentifier")


def _request_ids(event: dict) -> list[str]:
    """Some events (WAF, CloudFront) carry no Resources; fall back to identifiers in the request."""
    try:
        params = json.loads(event.get("CloudTrailEvent", "{}")).get("requestParameters") or {}
    except ValueError:
        return []
    return [f"{k}={params[k]}" for k in ID_FIELDS if isinstance(params.get(k), str)][:3]


def recent_changes(session: boto3.Session, regions: list[str], days: int = 7, limit: int = 150) -> dict:
    """Create/delete events across regions. Global services (CloudFront, WAF global, Route 53) log to us-east-1."""
    start = datetime.now(timezone.utc) - timedelta(days=days)
    errors: list[str] = []

    def run(region: str) -> list[dict]:
        try:
            return _region_events(session, region, start, limit)
        except (ClientError, BotoCoreError) as e:
            errors.append(f"{region}: {e}")
            return []

    with ThreadPoolExecutor(max_workers=8) as pool:
        events = [e for batch in pool.map(run, regions) for e in batch]
    events.sort(key=lambda e: e["time"], reverse=True)
    return {
        "days": days,
        "note": "Cost Explorer lags 24-48h: a resource deleted here can still appear in recent billing.",
        "events": events[:limit],
        "truncated": len(events) > limit,
        "errors": errors[:5],
    }
