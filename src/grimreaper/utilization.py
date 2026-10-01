"""CloudWatch usage signals: is a resource actually idle, or just looks idle?"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import boto3

from .models import Resource


def _daily(session: boto3.Session, region: str, namespace: str, metric: str, dims: dict[str, str],
           stat: str, days: int) -> list[float]:
    cw = session.client("cloudwatch", region_name=region)
    end = datetime.now(timezone.utc)
    resp = cw.get_metric_statistics(
        Namespace=namespace, MetricName=metric, Dimensions=[{"Name": k, "Value": v} for k, v in dims.items()],
        StartTime=end - timedelta(days=days), EndTime=end, Period=86400, Statistics=[stat],
    )
    points = sorted(resp["Datapoints"], key=lambda p: p["Timestamp"])
    return [round(p[stat], 3) for p in points]


def _summary(name: str, values: list[float], idle_below: float, unit: str) -> dict:
    if not values:
        return {"metric": name, "datapoints": 0, "signal": "no data (often means never used)"}
    peak = max(values)
    return {
        "metric": name, "unit": unit, "datapoints": len(values), "daily": values,
        "max": peak, "avg": round(sum(values) / len(values), 3),
        "signal": "idle" if peak < idle_below else "in use",
    }


def utilization(session: boto3.Session, resource: Resource, days: int = 14) -> dict:
    r = resource
    if r.kind == "ec2_instance":
        values = _daily(session, r.region, "AWS/EC2", "CPUUtilization", {"InstanceId": r.id}, "Maximum", days)
        return _summary("max CPU per day", values, idle_below=5.0, unit="percent")
    if r.kind == "ebs_volume":
        reads = _daily(session, r.region, "AWS/EBS", "VolumeReadOps", {"VolumeId": r.id}, "Sum", days)
        writes = _daily(session, r.region, "AWS/EBS", "VolumeWriteOps", {"VolumeId": r.id}, "Sum", days)
        return _summary("read+write ops per day", [a + b for a, b in zip(reads, writes)] or reads or writes, 1, "ops")
    if r.kind == "load_balancer":
        lb_dim = r.id.split(":loadbalancer/")[-1]  # app/name/id or net/name/id
        if lb_dim.startswith("net/"):
            values = _daily(session, r.region, "AWS/NetworkELB", "NewFlowCount", {"LoadBalancer": lb_dim}, "Sum", days)
            return _summary("new flows per day", values, 1, "flows")
        values = _daily(session, r.region, "AWS/ApplicationELB", "RequestCount", {"LoadBalancer": lb_dim}, "Sum", days)
        return _summary("requests per day", values, 1, "requests")
    if r.kind == "classic_load_balancer":
        values = _daily(session, r.region, "AWS/ELB", "RequestCount", {"LoadBalancerName": r.id}, "Sum", days)
        return _summary("requests per day", values, 1, "requests")
    if r.kind == "nat_gateway":
        values = _daily(session, r.region, "AWS/NATGateway", "BytesOutToDestination", {"NatGatewayId": r.id}, "Sum", days)
        return _summary("bytes out per day", values, 1024 * 1024, "bytes")
    if r.kind == "rds_instance":
        values = _daily(session, r.region, "AWS/RDS", "DatabaseConnections", {"DBInstanceIdentifier": r.id}, "Maximum", days)
        return _summary("max connections per day", values, 1, "connections")
    return {
        "metric": None,
        "signal": "not applicable: this kind has no usage metric; judge by attachment, age, and description",
    }
