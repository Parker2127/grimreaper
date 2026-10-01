"""Deterministic inventory of billable AWS resources.

Costs are rough us-east-1 list prices, good enough to rank what matters.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from .models import Resource

HOURS = 730

EBS_GB_MONTH = {"gp3": 0.08, "gp2": 0.10, "io1": 0.125, "io2": 0.125, "st1": 0.045, "sc1": 0.015, "standard": 0.05}
SNAPSHOT_GB_MONTH = 0.05
RDS_SNAPSHOT_GB_MONTH = 0.095
EC2_HOURLY = {
    "t2.nano": 0.0058, "t2.micro": 0.0116, "t2.small": 0.023, "t2.medium": 0.0464, "t2.large": 0.0928,
    "t3.nano": 0.0052, "t3.micro": 0.0104, "t3.small": 0.0208, "t3.medium": 0.0416, "t3.large": 0.0832,
    "t3a.micro": 0.0094, "t3a.small": 0.0188, "t3a.medium": 0.0376,
    "m5.large": 0.096, "m5.xlarge": 0.192, "c5.large": 0.085, "r5.large": 0.126,
}

Scanner = Callable[[boto3.Session, str], list[Resource]]


def _tags(tag_list: list[dict] | None) -> dict[str, str]:
    return {t["Key"]: t.get("Value", "") for t in (tag_list or [])}


def scan_ec2_instances(session: boto3.Session, region: str) -> list[Resource]:
    ec2 = session.client("ec2", region_name=region)
    out = []
    for page in ec2.get_paginator("describe_instances").paginate(
        Filters=[{"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}]
    ):
        for res in page["Reservations"]:
            for i in res["Instances"]:
                tags = _tags(i.get("Tags"))
                state = i["State"]["Name"]
                hourly = EC2_HOURLY.get(i["InstanceType"], 0.0) if state == "running" else 0.0
                out.append(Resource(
                    "ec2_instance", i["InstanceId"], region, tags.get("Name", ""), round(hourly * HOURS, 2),
                    f"{i['InstanceType']} {state}, launched {i['LaunchTime']:%Y-%m-%d}", tags,
                ))
    return out


def scan_ebs_volumes(session: boto3.Session, region: str) -> list[Resource]:
    ec2 = session.client("ec2", region_name=region)
    out = []
    for page in ec2.get_paginator("describe_volumes").paginate():
        for v in page["Volumes"]:
            tags = _tags(v.get("Tags"))
            attached = ", ".join(a["InstanceId"] for a in v.get("Attachments", [])) or "UNATTACHED"
            cost = v["Size"] * EBS_GB_MONTH.get(v["VolumeType"], 0.08)
            out.append(Resource(
                "ebs_volume", v["VolumeId"], region, tags.get("Name", ""), round(cost, 2),
                f"{v['Size']} GiB {v['VolumeType']}, {attached}", tags,
            ))
    return out


def scan_ebs_snapshots(session: boto3.Session, region: str) -> list[Resource]:
    ec2 = session.client("ec2", region_name=region)
    out = []
    for page in ec2.get_paginator("describe_snapshots").paginate(OwnerIds=["self"]):
        for s in page["Snapshots"]:
            tags = _tags(s.get("Tags"))
            out.append(Resource(
                "ebs_snapshot", s["SnapshotId"], region, tags.get("Name", ""),
                round(s["VolumeSize"] * SNAPSHOT_GB_MONTH, 2),
                f"{s['VolumeSize']} GiB, created {s['StartTime']:%Y-%m-%d}: {s.get('Description', '')[:80]}", tags,
            ))
    return out


def scan_amis(session: boto3.Session, region: str) -> list[Resource]:
    ec2 = session.client("ec2", region_name=region)
    out = []
    for img in ec2.describe_images(Owners=["self"])["Images"]:
        snaps = [m["Ebs"]["SnapshotId"] for m in img.get("BlockDeviceMappings", []) if "Ebs" in m and "SnapshotId" in m["Ebs"]]
        out.append(Resource(
            "ami", img["ImageId"], region, img.get("Name", ""), 0.0,
            f"backed by {', '.join(snaps) or 'no snapshots'} (cost counted on the snapshots; reaping the AMI also deletes them)",
            _tags(img.get("Tags")),
        ))
    return out


def scan_elastic_ips(session: boto3.Session, region: str) -> list[Resource]:
    ec2 = session.client("ec2", region_name=region)
    out = []
    for a in ec2.describe_addresses()["Addresses"]:
        attached = a.get("InstanceId") or a.get("NetworkInterfaceId") or "UNATTACHED"
        out.append(Resource(
            "elastic_ip", a["AllocationId"], region, a.get("PublicIp", ""), round(0.005 * HOURS, 2),
            f"{a.get('PublicIp')} -> {attached}", _tags(a.get("Tags")),
        ))
    return out


def scan_nat_gateways(session: boto3.Session, region: str) -> list[Resource]:
    ec2 = session.client("ec2", region_name=region)
    out = []
    for n in ec2.describe_nat_gateways(Filter=[{"Name": "state", "Values": ["pending", "available"]}])["NatGateways"]:
        tags = _tags(n.get("Tags"))
        out.append(Resource(
            "nat_gateway", n["NatGatewayId"], region, tags.get("Name", ""), round(0.045 * HOURS, 2),
            f"in {n['VpcId']} (plus $0.045/GB processed)", tags,
        ))
    return out


def scan_vpc_endpoints(session: boto3.Session, region: str) -> list[Resource]:
    ec2 = session.client("ec2", region_name=region)
    out = []
    for e in ec2.describe_vpc_endpoints()["VpcEndpoints"]:
        if e["VpcEndpointType"] == "Gateway":  # S3/DynamoDB gateway endpoints are free
            continue
        azs = max(len(e.get("SubnetIds", [])), 1)
        out.append(Resource(
            "vpc_endpoint", e["VpcEndpointId"], region, e["ServiceName"], round(0.01 * HOURS * azs, 2),
            f"{e['VpcEndpointType']} endpoint in {azs} AZ(s)", _tags(e.get("Tags")),
        ))
    return out


def scan_load_balancers(session: boto3.Session, region: str) -> list[Resource]:
    elb = session.client("elbv2", region_name=region)
    out = []
    for page in elb.get_paginator("describe_load_balancers").paginate():
        for lb in page["LoadBalancers"]:
            out.append(Resource(
                "load_balancer", lb["LoadBalancerArn"], region, lb["LoadBalancerName"], round(0.0225 * HOURS, 2),
                f"{lb['Type']} LB, {lb['Scheme']}, created {lb['CreatedTime']:%Y-%m-%d} (plus LCU charges)",
            ))
    classic = session.client("elb", region_name=region)
    for lb in classic.describe_load_balancers()["LoadBalancerDescriptions"]:
        out.append(Resource(
            "classic_load_balancer", lb["LoadBalancerName"], region, lb["LoadBalancerName"], round(0.025 * HOURS, 2),
            f"classic LB with {len(lb.get('Instances', []))} instance(s)",
        ))
    return out


def scan_rds(session: boto3.Session, region: str) -> list[Resource]:
    rds = session.client("rds", region_name=region)
    out = []
    for db in rds.describe_db_instances()["DBInstances"]:
        in_cluster = bool(db.get("DBClusterIdentifier"))
        out.append(Resource(
            "rds_instance", db["DBInstanceIdentifier"], region, db["DBInstanceIdentifier"], 0.0,
            f"{db['DBInstanceClass']} {db['Engine']} {db['DBInstanceStatus']}, {db.get('AllocatedStorage', 0)} GiB"
            + (f", member of cluster {db['DBClusterIdentifier']}" if in_cluster else ""),
            _tags(db.get("TagList")), deletable=not in_cluster,
        ))
    for c in rds.describe_db_clusters()["DBClusters"]:
        out.append(Resource(
            "rds_cluster", c["DBClusterIdentifier"], region, c["DBClusterIdentifier"], 0.0,
            f"{c['Engine']} cluster {c['Status']} with {len(c.get('DBClusterMembers', []))} member(s)",
            _tags(c.get("TagList")), deletable=False,
        ))
    for s in rds.describe_db_snapshots(SnapshotType="manual")["DBSnapshots"]:
        out.append(Resource(
            "rds_snapshot", s["DBSnapshotIdentifier"], region, s["DBSnapshotIdentifier"],
            round(s.get("AllocatedStorage", 0) * RDS_SNAPSHOT_GB_MONTH, 2),
            f"manual snapshot of {s.get('DBInstanceIdentifier')}, {s.get('AllocatedStorage', 0)} GiB",
            _tags(s.get("TagList")),
        ))
    for s in rds.describe_db_cluster_snapshots(SnapshotType="manual")["DBClusterSnapshots"]:
        out.append(Resource(
            "rds_cluster_snapshot", s["DBClusterSnapshotIdentifier"], region, s["DBClusterSnapshotIdentifier"],
            round(s.get("AllocatedStorage", 0) * RDS_SNAPSHOT_GB_MONTH, 2),
            f"manual snapshot of cluster {s.get('DBClusterIdentifier')}", _tags(s.get("TagList")),
        ))
    return out


def scan_eks(session: boto3.Session, region: str) -> list[Resource]:
    eks = session.client("eks", region_name=region)
    return [
        Resource("eks_cluster", name, region, name, round(0.10 * HOURS, 2),
                 "control plane only; node groups billed as EC2", deletable=False)
        for name in eks.list_clusters()["clusters"]
    ]


def scan_efs(session: boto3.Session, region: str) -> list[Resource]:
    efs = session.client("efs", region_name=region)
    out = []
    for fs in efs.describe_file_systems()["FileSystems"]:
        gb = fs["SizeInBytes"]["Value"] / 1024**3
        out.append(Resource(
            "efs", fs["FileSystemId"], region, fs.get("Name", ""), round(gb * 0.30, 2),
            f"{gb:.2f} GiB, {fs['NumberOfMountTargets']} mount target(s)", _tags(fs.get("Tags")), deletable=False,
        ))
    return out


def scan_secrets(session: boto3.Session, region: str) -> list[Resource]:
    sm = session.client("secretsmanager", region_name=region)
    out = []
    for page in sm.get_paginator("list_secrets").paginate():
        for s in page["SecretList"]:
            out.append(Resource(
                "secret", s["ARN"], region, s["Name"], 0.40,
                f"last accessed {s.get('LastAccessedDate', 'never')}", _tags(s.get("Tags")),
            ))
    return out


def scan_waf_regional(session: boto3.Session, region: str) -> list[Resource]:
    return _scan_waf(session, region, "REGIONAL")


def _scan_waf(session: boto3.Session, region: str, scope: str) -> list[Resource]:
    waf = session.client("wafv2", region_name=region)
    out = []
    for acl in waf.list_web_acls(Scope=scope)["WebACLs"]:
        full = waf.get_web_acl(Name=acl["Name"], Scope=scope, Id=acl["Id"])["WebACL"]
        rules = len(full.get("Rules", []))
        out.append(Resource(
            "waf_web_acl", f"{scope}|{acl['Name']}|{acl['Id']}", region, acl["Name"], 5.0 + rules,
            f"{scope} web ACL with {rules} rule(s)",
        ))
    return out


# --- global services (scanned once, reported as region "global") ---

def scan_global(session: boto3.Session) -> list[Resource]:
    out = [
        Resource(r.kind, r.id, "global", r.name, r.monthly_cost, r.detail, r.tags, r.deletable)
        for r in _scan_waf(session, "us-east-1", "CLOUDFRONT")
    ]

    cf = session.client("cloudfront")
    for d in cf.list_distributions().get("DistributionList", {}).get("Items", []):
        disabled = not d["Enabled"]
        out.append(Resource(
            "cloudfront_distribution", d["Id"], "global", d["DomainName"], 0.0,
            f"{'DISABLED' if disabled else 'enabled'}, status {d['Status']}"
            + (f", protected by WAF {d['WebACLId'].split('/')[-2]}" if d.get("WebACLId") else ""),
            deletable=disabled,  # enabled distributions must be disabled + deployed first
        ))

    r53 = session.client("route53")
    for z in r53.list_hosted_zones()["HostedZones"]:
        zone_id = z["Id"].split("/")[-1]
        out.append(Resource(
            "route53_zone", zone_id, "global", z["Name"], 0.50,
            f"{'private' if z['Config'].get('PrivateZone') else 'public'} zone, {z['ResourceRecordSetCount']} records",
        ))

    s3 = session.client("s3")
    for b in s3.list_buckets()["Buckets"]:
        try:
            tags = _tags(s3.get_bucket_tagging(Bucket=b["Name"])["TagSet"])
        except ClientError:
            tags = {}
        out.append(Resource(
            "s3_bucket", b["Name"], "global", b["Name"], 0.0,
            f"created {b['CreationDate']:%Y-%m-%d} (storage cost not estimated)", tags,
        ))
    return out


REGIONAL_SCANNERS: list[Scanner] = [
    scan_ec2_instances, scan_ebs_volumes, scan_ebs_snapshots, scan_amis, scan_elastic_ips,
    scan_nat_gateways, scan_vpc_endpoints, scan_load_balancers, scan_rds, scan_eks, scan_efs,
    scan_secrets, scan_waf_regional,
]


def enabled_regions(session: boto3.Session) -> list[str]:
    ec2 = session.client("ec2", region_name=session.region_name or "us-east-1")
    return sorted(r["RegionName"] for r in ec2.describe_regions()["Regions"])


def scan_all(session: boto3.Session, regions: list[str], on_error: Callable[[str], None] = lambda _: None) -> list[Resource]:
    """Run every scanner across every region in parallel."""

    def run(job: tuple[Scanner, str]) -> list[Resource]:
        scanner, region = job
        try:
            return scanner(session, region)
        except (ClientError, BotoCoreError) as e:
            on_error(f"{scanner.__name__} in {region}: {e}")
            return []

    jobs = [(s, r) for r in regions for s in REGIONAL_SCANNERS]
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = [r for batch in pool.map(run, jobs) for r in batch]
    try:
        results += scan_global(session)
    except (ClientError, BotoCoreError) as e:
        on_error(f"global scan: {e}")
    return sorted(results, key=lambda r: r.monthly_cost, reverse=True)
