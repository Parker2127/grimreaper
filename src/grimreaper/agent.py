"""The GrimReaper agent, running on the OpenAI Agents API.

One agent investigates with read-only tools that run locally (see runtime.py):
Cost Explorer, a multi-region inventory, CloudWatch utilization, and CloudTrail history.
It returns a typed ReapingReport. Deletion lives in reaper.py and runs from the CLI after a human approves.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

import boto3

from . import costs, scanners, trail, utilization
from .models import ReapingReport, Resource
from .runtime import DEFAULT_MODEL, Progress, Tool, run_session
from .safety import protection_reason


@dataclass
class ReaperContext:
    session: boto3.Session
    regions: list[str]
    inventory: dict[str, Resource] = field(default_factory=dict)
    scan_errors: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def ensure_inventory(self) -> dict[str, Resource]:
        with self._lock:  # tools can run concurrently; scan once
            if not self.inventory:
                found = scanners.scan_all(self.session, self.regions, on_error=self.scan_errors.append)
                self.inventory = {r.key: r for r in found}
        return self.inventory


def _schema(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


INT = {"type": "integer"}
STR = {"type": "string"}


def build_tools(ctx: ReaperContext) -> list[Tool]:
    s = ctx.session

    def scan_inventory(args: dict) -> dict:
        kind, region = args.get("kind"), args.get("region")
        items = [
            {"key": r.key, "name": r.name, "est_monthly_usd": r.monthly_cost, "detail": r.detail,
             "protected": protection_reason(r)}
            for r in ctx.ensure_inventory().values()
            if (not kind or r.kind == kind) and (not region or r.region == region)
        ]
        return {"resources": items, "scan_errors": ctx.scan_errors[:10]}

    def check_utilization(args: dict) -> dict:
        key = args["resource_key"]
        resource = ctx.ensure_inventory().get(key)
        if resource is None:
            return {"error": f"unknown resource_key {key}; use a key returned by scan_inventory"}
        return {"resource_key": key} | utilization.utilization(s, resource, int(args.get("days") or 14))

    return [
        Tool("get_cost_by_service", "Usage cost (credits excluded) by AWS service and region.",
             _schema({"days": INT}), lambda a: costs.cost_by_service(s, int(a.get("days") or 30))),
        Tool("get_cost_by_usage_type", "Usage cost by usage type (e.g. USE2-EBS:SnapshotUsage). Short windows show what bills right now.",
             _schema({"days": INT}), lambda a: costs.cost_by_usage_type(s, int(a.get("days") or 3))),
        Tool("get_daily_cost_trend", "Daily usage cost, to see if spend is steady, rising, or stopped.",
             _schema({"days": INT}), lambda a: costs.daily_trend(s, int(a.get("days") or 14))),
        Tool("compare_cost_windows", "Compare the last N days with the N days before, per usage type. Flags new, rising, and stopped charges.",
             _schema({"recent_days": INT}), lambda a: costs.compare_windows(s, int(a.get("recent_days") or 3))),
        Tool("get_recent_changes", "CloudTrail create/delete events across regions over N days. Explains charges whose resource was "
             "already deleted (Cost Explorer lags 24-48h) and charges from newly created resources.",
             _schema({"days": INT}), lambda a: trail.recent_changes(s, ctx.regions, int(a.get("days") or 7))),
        Tool("scan_inventory", "Billable resources across all enabled regions and global services. Use each `key` verbatim.",
             _schema({"kind": STR, "region": STR}), scan_inventory),
        Tool("check_utilization", "CloudWatch usage for one resource over N days: is it actually idle or in use?",
             _schema({"resource_key": STR, "days": INT}, ["resource_key"]), check_utilization),
    ]


INSTRUCTIONS = """\
You are GrimReaper. You find AWS resources that cost money for nothing, and you prove it with evidence.

Investigate in this order:
1. Money: where it goes (get_cost_by_service, 30 days), what bills right now (get_cost_by_usage_type, 3 days),
   and what is new or rising (compare_cost_windows).
2. History: Cost Explorer lags 24-48h, so call get_recent_changes to see which charges come from resources that
   were already deleted, and which resources were created recently.
3. Resources: scan_inventory, then check_utilization on every resource that has a usage metric (instances,
   volumes, load balancers, NAT gateways, RDS instances). Separate "unattached but used recently" from "truly idle".
4. Reconcile: match each charge to the resource behind it. A charge with no live resource is "already stopped"
   when CloudTrail shows the deletion; say so, with the event and date. Only call a charge unexplained when
   neither the inventory nor CloudTrail accounts for it.

Verdict rules:
- Only use resource_key values returned by scan_inventory. Never invent one.
- delete: abandoned or idle, with evidence (zero traffic, unattached and old, copies nobody uses), and not protected.
- review: might be in use, or evidence is mixed (running instances with some CPU, live databases, very new resources).
- keep: protected, in use, or free and harmless.
- Biggest savings first. Each reason cites its evidence in one or two sentences.
"""

WATCH_INSTRUCTIONS = """\
You are GrimReaper's watchdog, running on a schedule. You are given what changed since the last run.
Explain only the changes; do not re-report things that were already there.

Use compare_cost_windows and get_cost_by_usage_type to explain new or rising charges, get_recent_changes to see
who created what and when, and check_utilization to see whether new resources are being used. Give a verdict for
every new resource, and mention unexplained new charges in the summary.

Only use resource_key values from scan_inventory. Never invent one.
"""


def report_schema() -> dict:
    schema = ReapingReport.model_json_schema()
    _close(schema)
    return schema


def _close(node) -> None:
    """Structured outputs want every object closed and every property required."""
    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
            node["additionalProperties"] = False
            node["required"] = list(node["properties"])
        for value in node.values():
            _close(value)
    elif isinstance(node, list):
        for value in node:
            _close(value)


def validate(report: ReapingReport, inventory: dict[str, Resource]) -> tuple[ReapingReport, list[str]]:
    """Drop any verdict that doesn't point at a resource the scanners actually found."""
    dropped = [v.resource_key for v in report.verdicts if v.resource_key not in inventory]
    report.verdicts = [v for v in report.verdicts if v.resource_key in inventory]
    return report, dropped


def _run(client, ctx: ReaperContext, instructions: str, task: str, model: str | None,
         on_progress: Callable[[Progress], None]) -> tuple[ReapingReport, list[str]]:
    text = run_session(
        client, instructions=instructions, task=task, tools=build_tools(ctx), output_schema=report_schema(),
        model=model or DEFAULT_MODEL, on_progress=on_progress,
    )
    report = ReapingReport.model_validate_json(text)
    return validate(report, ctx.ensure_inventory())


def investigate(client, ctx: ReaperContext, model: str | None = None,
                on_progress: Callable[[Progress], None] = lambda _: None) -> tuple[ReapingReport, list[str]]:
    return _run(client, ctx, INSTRUCTIONS,
                "Find what is costing money in this AWS account and what can be safely reaped.", model, on_progress)


def explain_changes(client, ctx: ReaperContext, changes: dict, model: str | None = None,
                    on_progress: Callable[[Progress], None] = lambda _: None) -> tuple[ReapingReport, list[str]]:
    task = "Changes since the last run:\n" + json.dumps(changes, indent=2, default=str)
    return _run(client, ctx, WATCH_INSTRUCTIONS, task, model, on_progress)
