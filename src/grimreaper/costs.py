"""Cost Explorer queries. Credits are excluded so you see what you'd actually pay."""

from __future__ import annotations

from datetime import date, timedelta

import boto3

USAGE_ONLY = {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Usage"]}}


def _window(days: int, end: date | None = None) -> dict[str, str]:
    end = end or date.today()
    return {"Start": (end - timedelta(days=days)).isoformat(), "End": end.isoformat()}


def _grouped(session: boto3.Session, period: dict[str, str], *keys: str) -> dict[tuple[str, ...], float]:
    ce = session.client("ce", region_name="us-east-1")
    rows: dict[tuple[str, ...], float] = {}
    kwargs = dict(
        TimePeriod=period, Granularity="MONTHLY", Metrics=["UnblendedCost"], Filter=USAGE_ONLY,
        GroupBy=[{"Type": "DIMENSION", "Key": k} for k in keys],
    )
    while True:
        resp = ce.get_cost_and_usage(**kwargs)
        for result in resp["ResultsByTime"]:
            for g in result["Groups"]:
                key = tuple(g["Keys"])
                rows[key] = rows.get(key, 0.0) + float(g["Metrics"]["UnblendedCost"]["Amount"])
        if not resp.get("NextPageToken"):
            break
        kwargs["NextPageToken"] = resp["NextPageToken"]
    return rows


def _ranked(rows: dict[tuple[str, ...], float]) -> list[tuple[tuple[str, ...], float]]:
    return sorted(((k, v) for k, v in rows.items() if v >= 0.005), key=lambda kv: kv[1], reverse=True)


def cost_by_service(session: boto3.Session, days: int = 30) -> list[dict]:
    rows = _grouped(session, _window(days), "SERVICE", "REGION")
    return [{"service": k[0], "region": k[1], "usd": round(v, 2)} for k, v in _ranked(rows)]


def cost_by_usage_type(session: boto3.Session, days: int = 3) -> list[dict]:
    rows = _grouped(session, _window(days), "USAGE_TYPE", "SERVICE")
    return [{"usage_type": k[0], "service": k[1], "usd": round(v, 4)} for k, v in _ranked(rows)]


def daily_trend(session: boto3.Session, days: int = 14) -> list[dict]:
    ce = session.client("ce", region_name="us-east-1")
    resp = ce.get_cost_and_usage(TimePeriod=_window(days), Granularity="DAILY", Metrics=["UnblendedCost"], Filter=USAGE_ONLY)
    return [
        {"date": p["TimePeriod"]["Start"], "usd": round(float(p["Total"]["UnblendedCost"]["Amount"]), 4)}
        for p in resp["ResultsByTime"]
    ]


def compare_windows(session: boto3.Session, recent_days: int = 3) -> dict:
    """Compare the last `recent_days` with the window right before it, per usage type.

    Flags usage types that are brand new (no cost before) or rose by more than 50%.
    """
    today = date.today()
    recent = _grouped(session, _window(recent_days, today), "USAGE_TYPE", "SERVICE")
    previous = _grouped(session, _window(recent_days, today - timedelta(days=recent_days)), "USAGE_TYPE", "SERVICE")

    changes = []
    for key in set(recent) | set(previous):
        now, before = recent.get(key, 0.0), previous.get(key, 0.0)
        if max(now, before) < 0.005:
            continue
        if before < 0.005:
            status = "new"
        elif now < 0.005:
            status = "stopped"
        elif now > before * 1.5:
            status = "rising"
        elif now < before * 0.5:
            status = "falling"
        else:
            status = "steady"
        changes.append({
            "usage_type": key[0], "service": key[1], "status": status,
            "recent_usd_per_day": round(now / recent_days, 4), "previous_usd_per_day": round(before / recent_days, 4),
        })
    order = {"new": 0, "rising": 1, "steady": 2, "falling": 3, "stopped": 4}
    changes.sort(key=lambda c: (order[c["status"]], -c["recent_usd_per_day"]))
    return {
        "window_days": recent_days,
        "recent_usd_per_day": round(sum(recent.values()) / recent_days, 4),
        "previous_usd_per_day": round(sum(previous.values()) / recent_days, 4),
        "usage_types": changes,
    }
