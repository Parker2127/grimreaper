"""Deterministic deletion. The LLM never calls these; only the CLI does, after a human approves."""

from __future__ import annotations

from collections.abc import Callable

import boto3

from .models import Resource
from .safety import protection_reason


class ReapError(Exception):
    pass


def _ec2(s: boto3.Session, r: Resource):
    return s.client("ec2", region_name=r.region)


def _ec2_instance(s, r):
    _ec2(s, r).terminate_instances(InstanceIds=[r.id])
    return "terminating"


def _ebs_volume(s, r):
    _ec2(s, r).delete_volume(VolumeId=r.id)
    return "deleted"


def _ebs_snapshot(s, r):
    _ec2(s, r).delete_snapshot(SnapshotId=r.id)
    return "deleted"


def _ami(s, r):
    ec2 = _ec2(s, r)
    image = ec2.describe_images(ImageIds=[r.id])["Images"][0]
    snaps = [m["Ebs"]["SnapshotId"] for m in image.get("BlockDeviceMappings", []) if "SnapshotId" in m.get("Ebs", {})]
    ec2.deregister_image(ImageId=r.id)
    for snap in snaps:
        ec2.delete_snapshot(SnapshotId=snap)
    return f"deregistered, deleted {len(snaps)} backing snapshot(s)"


def _elastic_ip(s, r):
    ec2 = _ec2(s, r)
    addr = ec2.describe_addresses(AllocationIds=[r.id])["Addresses"][0]
    if "AssociationId" in addr:
        ec2.disassociate_address(AssociationId=addr["AssociationId"])
    ec2.release_address(AllocationId=r.id)
    return "released"


def _nat_gateway(s, r):
    _ec2(s, r).delete_nat_gateway(NatGatewayId=r.id)
    return "deleting (its Elastic IP is not released automatically)"


def _vpc_endpoint(s, r):
    _ec2(s, r).delete_vpc_endpoints(VpcEndpointIds=[r.id])
    return "deleting"


def _load_balancer(s, r):
    s.client("elbv2", region_name=r.region).delete_load_balancer(LoadBalancerArn=r.id)
    return "deleted"


def _classic_load_balancer(s, r):
    s.client("elb", region_name=r.region).delete_load_balancer(LoadBalancerName=r.id)
    return "deleted"


def _rds_instance(s, r):
    s.client("rds", region_name=r.region).delete_db_instance(
        DBInstanceIdentifier=r.id, SkipFinalSnapshot=True, DeleteAutomatedBackups=True
    )
    return "deleting (no final snapshot)"


def _rds_snapshot(s, r):
    s.client("rds", region_name=r.region).delete_db_snapshot(DBSnapshotIdentifier=r.id)
    return "deleting"


def _rds_cluster_snapshot(s, r):
    s.client("rds", region_name=r.region).delete_db_cluster_snapshot(DBClusterSnapshotIdentifier=r.id)
    return "deleting"


def _secret(s, r):
    s.client("secretsmanager", region_name=r.region).delete_secret(SecretId=r.id, RecoveryWindowInDays=7)
    return "scheduled for deletion in 7 days (restorable until then)"


def _waf_web_acl(s, r):
    scope, name, acl_id = r.id.split("|")
    waf = s.client("wafv2", region_name="us-east-1" if scope == "CLOUDFRONT" else r.region)
    token = waf.get_web_acl(Name=name, Scope=scope, Id=acl_id)["LockToken"]
    waf.delete_web_acl(Name=name, Scope=scope, Id=acl_id, LockToken=token)
    return "deleted"


def _cloudfront_distribution(s, r):
    cf = s.client("cloudfront")
    resp = cf.get_distribution(Id=r.id)
    dist = resp["Distribution"]
    if dist["DistributionConfig"]["Enabled"] or dist["Status"] != "Deployed":
        raise ReapError("distribution must be disabled and fully deployed first")
    cf.delete_distribution(Id=r.id, IfMatch=resp["ETag"])
    return "deleted"


def _route53_zone(s, r):
    r53 = s.client("route53")
    zone_name = r53.get_hosted_zone(Id=r.id)["HostedZone"]["Name"]
    changes = []
    for page in r53.get_paginator("list_resource_record_sets").paginate(HostedZoneId=r.id):
        for rrset in page["ResourceRecordSets"]:
            if rrset["Type"] in ("NS", "SOA") and rrset["Name"] == zone_name:
                continue
            changes.append({"Action": "DELETE", "ResourceRecordSet": rrset})
    for i in range(0, len(changes), 500):
        r53.change_resource_record_sets(HostedZoneId=r.id, ChangeBatch={"Changes": changes[i:i + 500]})
    r53.delete_hosted_zone(Id=r.id)
    return f"deleted {len(changes)} record(s) and the zone"


def _s3_bucket(s, r):
    bucket = s.resource("s3").Bucket(r.id)
    bucket.object_versions.delete()
    bucket.objects.delete()
    bucket.delete()
    return "emptied (all versions) and deleted"


HANDLERS: dict[str, Callable[[boto3.Session, Resource], str]] = {
    "ec2_instance": _ec2_instance,
    "ebs_volume": _ebs_volume,
    "ebs_snapshot": _ebs_snapshot,
    "ami": _ami,
    "elastic_ip": _elastic_ip,
    "nat_gateway": _nat_gateway,
    "vpc_endpoint": _vpc_endpoint,
    "load_balancer": _load_balancer,
    "classic_load_balancer": _classic_load_balancer,
    "rds_instance": _rds_instance,
    "rds_snapshot": _rds_snapshot,
    "rds_cluster_snapshot": _rds_cluster_snapshot,
    "secret": _secret,
    "waf_web_acl": _waf_web_acl,
    "cloudfront_distribution": _cloudfront_distribution,
    "route53_zone": _route53_zone,
    "s3_bucket": _s3_bucket,
}


def reap(session: boto3.Session, resource: Resource) -> str:
    if reason := protection_reason(resource):
        raise ReapError(f"protected: {reason}")
    handler = HANDLERS.get(resource.kind)
    if handler is None:
        raise ReapError(f"no handler for {resource.kind}")
    return handler(session, resource)
