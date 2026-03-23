import json
import logging
import os
import urllib.request
from datetime import datetime, timezone, timedelta

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ── Config ────────────────────────────────────────────────────────────────────
SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]
AWS_REGION        = os.environ.get("AWS_REGION", "us-east-1")
SNAPSHOT_AGE_DAYS = int(os.environ.get("SNAPSHOT_AGE_DAYS", "90"))
CPU_LOOKBACK_DAYS = int(os.environ.get("CPU_LOOKBACK_DAYS", "7"))
CPU_THRESHOLD     = float(os.environ.get("CPU_THRESHOLD", "1.0"))

ec2 = boto3.client("ec2", region_name=AWS_REGION)
cw  = boto3.client("cloudwatch", region_name=AWS_REGION)


# ══════════════════════════════════════════════════════════════════════════════
# SCANNERS
# ══════════════════════════════════════════════════════════════════════════════

def scan_unattached_ebs_volumes() -> list[dict]:
    paginator = ec2.get_paginator("describe_volumes")
    results = []
    for page in paginator.paginate(Filters=[{"Name": "status", "Values": ["available"]}]):
        for vol in page["Volumes"]:
            name = _get_tag(vol.get("Tags", []), "Name") or "(no name)"
            results.append({
                "resource_type": "ebs",
                "id":      vol["VolumeId"],
                "label":   f"{name} — {vol['Size']} GB {vol['VolumeType']}",
                "created": vol["CreateTime"].strftime("%Y-%m-%d"),
            })
    return results


def scan_idle_ec2_instances() -> list[dict]:
    now   = datetime.now(timezone.utc)
    start = now - timedelta(days=CPU_LOOKBACK_DAYS)
    results = []
    paginator = ec2.get_paginator("describe_instances")
    for page in paginator.paginate(Filters=[{"Name": "instance-state-name", "Values": ["running"]}]):
        for res in page["Reservations"]:
            for inst in res["Instances"]:
                avg_cpu = _get_avg_cpu(inst["InstanceId"], start, now)
                if avg_cpu is not None and avg_cpu <= CPU_THRESHOLD:
                    name = _get_tag(inst.get("Tags", []), "Name") or "(no name)"
                    results.append({
                        "resource_type": "ec2",
                        "id":      inst["InstanceId"],
                        "label":   f"{name} — {inst['InstanceType']}, avg CPU {round(avg_cpu, 2)}%",
                        "created": inst["LaunchTime"].strftime("%Y-%m-%d"),
                    })
    return results


def scan_old_snapshots() -> list[dict]:
    cutoff  = datetime.now(timezone.utc) - timedelta(days=SNAPSHOT_AGE_DAYS)
    account = boto3.client("sts").get_caller_identity()["Account"]
    results = []
    for page in ec2.get_paginator("describe_snapshots").paginate(OwnerIds=[account]):
        for snap in page["Snapshots"]:
            if snap["StartTime"] < cutoff:
                name = _get_tag(snap.get("Tags", []), "Name") or "(no name)"
                age  = (datetime.now(timezone.utc) - snap["StartTime"]).days
                results.append({
                    "resource_type": "snapshot",
                    "id":      snap["SnapshotId"],
                    "label":   f"{name} — {snap['VolumeSize']} GB, {age} days old",
                    "created": snap["StartTime"].strftime("%Y-%m-%d"),
                })
    return results


# ══════════════════════════════════════════════════════════════════════════════
# SLACK BLOCK BUILDER — interactive buttons per resource
# ══════════════════════════════════════════════════════════════════════════════

SECTION_ICONS = {"ebs": "💾", "ec2": "🖥️", "snapshot": "📸"}
SECTION_TITLES = {
    "ebs":      "Unattached EBS volumes",
    "ec2":      f"Idle EC2 instances (avg CPU ≤ {CPU_THRESHOLD}%)",
    "snapshot": f"Snapshots older than {SNAPSHOT_AGE_DAYS} days",
}


def _resource_block(resource: dict) -> list[dict]:
    """
    Returns a two-element block list for a single resource:
      1. section block  — resource details
      2. actions block  — Approve / Deny buttons
    """
    rtype = resource["resource_type"]
    rid   = resource["id"]
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*`{rid}`*  {resource['label']}\n_Created: {resource['created']}_",
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type":      "button",
                    "text":      {"type": "plain_text", "text": "✅ Approve deletion"},
                    "style":     "danger",
                    "action_id": f"approve_{rtype}_{rid}",
                    "value":     f"{rtype}::{rid}",
                    "confirm": {
                        "title":   {"type": "plain_text", "text": "Confirm deletion"},
                        "text":    {"type": "plain_text", "text": f"Permanently delete {rid}?"},
                        "confirm": {"type": "plain_text", "text": "Yes, delete it"},
                        "deny":    {"type": "plain_text", "text": "Cancel"},
                    },
                },
                {
                    "type":      "button",
                    "text":      {"type": "plain_text", "text": "❌ Deny"},
                    "action_id": f"deny_{rtype}_{rid}",
                    "value":     f"deny::{rid}",
                },
            ],
        },
    ]


def build_slack_payload(all_resources: list[dict]) -> dict:
    scan_date = datetime.now(timezone.utc).strftime("%A %d %B %Y, %H:%M UTC")
    total     = len(all_resources)

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "🧹 HygieneBot — Weekly AWS Cleanup Report"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Scan:* {scan_date}  |  *Region:* `{AWS_REGION}`  |  *Flagged:* *{total}*",
            },
        },
    ]

    if not all_resources:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "✅ No zombie resources found this week."},
        })
        return {"blocks": blocks}

    for rtype in ("ebs", "ec2", "snapshot"):
        group = [r for r in all_resources if r["resource_type"] == rtype]
        if not group:
            continue
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"{SECTION_ICONS[rtype]} *{SECTION_TITLES[rtype]}* — {len(group)} found"},
        })
        for resource in group[:5]:          # Slack 50-block safety cap
            blocks.extend(_resource_block(resource))
        if len(group) > 5:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"_...and {len(group) - 5} more. Check AWS Console._"},
            })

    blocks += [
        {"type": "divider"},
        {
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": ":lock: Deletions require explicit approval above. All actions logged to CloudWatch.",
            }],
        },
    ]
    return {"blocks": blocks}


def post_to_slack(payload: dict) -> None:
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(
        SLACK_WEBHOOK_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = resp.read().decode()
        if resp.status != 200 or body != "ok":
            raise RuntimeError(f"Slack error {resp.status}: {body}")


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _get_tag(tags: list[dict], key: str) -> str | None:
    return next((t["Value"] for t in tags if t["Key"] == key), None)


def _get_avg_cpu(instance_id: str, start: datetime, end: datetime) -> float | None:
    resp = cw.get_metric_statistics(
        Namespace="AWS/EC2",
        MetricName="CPUUtilization",
        Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
        StartTime=start,
        EndTime=end,
        Period=int((end - start).total_seconds()),
        Statistics=["Average"],
        Unit="Percent",
    )
    dp = resp.get("Datapoints", [])
    return dp[0]["Average"] if dp else None


# ══════════════════════════════════════════════════════════════════════════════
# ENTRYPOINT
# ══════════════════════════════════════════════════════════════════════════════

def lambda_handler(event: dict, context) -> dict:
    logger.info("HygieneBot scan started")

    volumes   = scan_unattached_ebs_volumes()
    instances = scan_idle_ec2_instances()
    snapshots = scan_old_snapshots()
    all_resources = volumes + instances + snapshots

    logger.info("Scan results | ebs=%d ec2=%d snapshots=%d",
                len(volumes), len(instances), len(snapshots))

    payload = build_slack_payload(all_resources)
    post_to_slack(payload)

    logger.info("Slack report sent successfully")
    return {
        "statusCode": 200,
        "body": json.dumps({
            "ebs": len(volumes),
            "ec2": len(instances),
            "snapshots": len(snapshots),
        }),
    }