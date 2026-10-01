"""Always-on mode: diff the account against the last run, and only wake the agents when something changed."""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import boto3

from . import costs
from .models import ReapingReport, Resource

RISE_THRESHOLD_USD_PER_DAY = 0.05  # ignore pennies


def load_state(path: Path) -> dict | None:
    return json.loads(path.read_text()) if path.exists() else None


def save_state(path: Path, inventory: dict[str, Resource], cost_view: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "resources": {k: r.to_dict() for k, r in inventory.items()},
        "usd_per_day": cost_view["recent_usd_per_day"],
    }, indent=2, default=str))


def diff(previous: dict | None, inventory: dict[str, Resource], cost_view: dict) -> dict:
    """What changed since the last run. Empty lists mean nothing worth waking the agents for."""
    before = (previous or {}).get("resources", {})
    new = [r for k, r in inventory.items() if k not in before]
    gone = [k for k in before if k not in inventory]
    charges = [
        c for c in cost_view["usage_types"]
        if c["status"] in ("new", "rising")
        and c["recent_usd_per_day"] - c["previous_usd_per_day"] >= RISE_THRESHOLD_USD_PER_DAY
    ]
    return {
        "first_run": previous is None,
        "since": (previous or {}).get("checked_at"),
        "new_resources": [{"key": r.key, "est_monthly_usd": r.monthly_cost, "detail": r.detail} for r in new],
        "removed_resources": gone,
        "new_or_rising_charges": charges,
        "usd_per_day_now": cost_view["recent_usd_per_day"],
        "usd_per_day_before": cost_view["previous_usd_per_day"],
    }


def is_quiet(changes: dict) -> bool:
    return not changes["first_run"] and not changes["new_resources"] and not changes["new_or_rising_charges"]


def current_costs(session: boto3.Session) -> dict:
    return costs.compare_windows(session, recent_days=3)


def slack_message(report: ReapingReport, changes: dict) -> str:
    lines = [f":skull: *GrimReaper*: {report.summary}"]
    lines.append(f"Spend: ${changes['usd_per_day_before']:.2f}/day -> ${changes['usd_per_day_now']:.2f}/day")
    for v in report.verdicts:
        if v.action != "keep":
            lines.append(f"- *{v.action}* `{v.resource_key}` (~${v.est_monthly_savings:.2f}/mo): {v.reason}")
    lines.append("Run `grimreaper reap` to review and approve deletions.")
    return "\n".join(lines)


def post_to_slack(webhook_url: str, text: str) -> None:
    req = urllib.request.Request(
        webhook_url, data=json.dumps({"text": text}).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()
