import os

import boto3
import pytest
from moto import mock_aws

from grimreaper import scanners
from grimreaper.agent import validate
from grimreaper.models import ReapingReport, Resource, Verdict
from grimreaper.reaper import ReapError, reap
from grimreaper.safety import protection_reason

REGION = "us-east-1"


@pytest.fixture
def session():
    os.environ.update(AWS_ACCESS_KEY_ID="test", AWS_SECRET_ACCESS_KEY="test", AWS_DEFAULT_REGION=REGION)
    with mock_aws():
        yield boto3.Session(region_name=REGION)


def _find(found, kind):
    return [r for r in found if r.kind == kind]


def test_scan_finds_the_usual_leftovers(session):
    ec2 = session.client("ec2")
    vol = ec2.create_volume(AvailabilityZone=f"{REGION}a", Size=20, VolumeType="gp3")
    ec2.create_snapshot(VolumeId=vol["VolumeId"])
    ec2.allocate_address(Domain="vpc")
    session.client("s3").create_bucket(Bucket="old-lab-bucket")

    found = scanners.scan_all(session, [REGION])

    volume = _find(found, "ebs_volume")[0]
    assert volume.monthly_cost == pytest.approx(1.60)
    assert "UNATTACHED" in volume.detail
    assert _find(found, "ebs_snapshot")
    assert _find(found, "elastic_ip")[0].monthly_cost == pytest.approx(3.65)
    assert [b.id for b in _find(found, "s3_bucket")] == ["old-lab-bucket"]


@pytest.mark.parametrize("resource, protected", [
    (Resource("s3_bucket", "do-not-delete-ssm-diagnosis", "global"), True),
    (Resource("ebs_volume", "vol-1", REGION, tags={"grimreaper:keep": "true"}), True),
    (Resource("ebs_volume", "vol-2", REGION, tags={"aws:cloudformation:stack-name": "web"}), True),
    (Resource("eks_cluster", "prod", REGION, deletable=False), True),
    (Resource("ebs_volume", "vol-3", REGION, tags={"Name": "scratch"}), False),
])
def test_protection_rules(resource, protected):
    assert (protection_reason(resource) is not None) is protected


def test_reap_refuses_protected_resources(session):
    with pytest.raises(ReapError, match="protected"):
        reap(session, Resource("s3_bucket", "do-not-delete-logs", "global"))


def test_reap_snapshot_and_versioned_bucket(session):
    ec2 = session.client("ec2")
    vol = ec2.create_volume(AvailabilityZone=f"{REGION}a", Size=8)
    snap = ec2.create_snapshot(VolumeId=vol["VolumeId"])["SnapshotId"]
    s3 = session.client("s3")
    s3.create_bucket(Bucket="versioned-lab")
    s3.put_bucket_versioning(Bucket="versioned-lab", VersioningConfiguration={"Status": "Enabled"})
    for _ in range(3):
        s3.put_object(Bucket="versioned-lab", Key="terraform.tfstate", Body=b"{}")

    reap(session, Resource("ebs_snapshot", snap, REGION))
    reap(session, Resource("s3_bucket", "versioned-lab", "global"))

    assert snap not in {s["SnapshotId"] for s in ec2.describe_snapshots(OwnerIds=["self"])["Snapshots"]}
    assert s3.list_buckets()["Buckets"] == []


def test_reap_ami_also_deletes_backing_snapshots(session):
    ec2 = session.client("ec2")
    instance = ec2.run_instances(ImageId="ami-12c6146b", MinCount=1, MaxCount=1)["Instances"][0]
    ami = ec2.create_image(InstanceId=instance["InstanceId"], Name="golden-image")["ImageId"]
    image = ec2.describe_images(ImageIds=[ami])["Images"][0]
    backing = {m["Ebs"]["SnapshotId"] for m in image["BlockDeviceMappings"] if "SnapshotId" in m.get("Ebs", {})}
    assert backing

    reap(session, Resource("ami", ami, REGION))

    assert ami not in {i["ImageId"] for i in ec2.describe_images(Owners=["self"])["Images"]}
    remaining = {s["SnapshotId"] for s in ec2.describe_snapshots(OwnerIds=["self"])["Snapshots"]}
    assert not backing & remaining


def test_validate_drops_hallucinated_resources():
    inventory = {"ebs_volume:us-east-1:vol-1": Resource("ebs_volume", "vol-1", REGION)}
    report = ReapingReport(summary="", observed_monthly_spend=1.0, verdicts=[
        Verdict(resource_key="ebs_volume:us-east-1:vol-1", action="delete", reason="unattached", est_monthly_savings=1),
        Verdict(resource_key="ebs_volume:us-east-1:vol-made-up", action="delete", reason="?", est_monthly_savings=9),
    ])

    report, dropped = validate(report, inventory)

    assert [v.resource_key for v in report.verdicts] == ["ebs_volume:us-east-1:vol-1"]
    assert dropped == ["ebs_volume:us-east-1:vol-made-up"]


def test_idle_detector_reports_no_data_and_not_applicable(session):
    from grimreaper.utilization import utilization

    instance = session.client("ec2").run_instances(ImageId="ami-12c6146b", MinCount=1, MaxCount=1)["Instances"][0]

    assert utilization(session, Resource("ec2_instance", instance["InstanceId"], REGION))["signal"].startswith("no data")
    assert utilization(session, Resource("elastic_ip", "eipalloc-1", REGION))["signal"].startswith("not applicable")


def test_compare_windows_flags_new_rising_and_stopped(monkeypatch):
    from grimreaper import costs

    windows = iter([
        {("NatGateway-Hours", "EC2"): 3.24, ("WebACL", "WAF"): 1.0, ("Snapshot", "EC2"): 0.30},  # recent 3 days
        {("WebACL", "WAF"): 0.5, ("Snapshot", "EC2"): 0.30, ("Old-LB", "ELB"): 2.0},             # 3 days before
    ])
    monkeypatch.setattr(costs, "_grouped", lambda *a, **k: next(windows))

    view = costs.compare_windows(None, recent_days=3)

    status = {c["usage_type"]: c["status"] for c in view["usage_types"]}
    assert status == {"NatGateway-Hours": "new", "WebACL": "rising", "Snapshot": "steady", "Old-LB": "stopped"}
    assert view["usage_types"][0]["usage_type"] == "NatGateway-Hours"
    assert view["recent_usd_per_day"] == pytest.approx(1.5133, abs=1e-3)
