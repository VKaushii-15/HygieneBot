"""
mlflow_tracking.py — MLflow experiment tracker for HygieneBot scan runs.

Logs detection thresholds as parameters, scan results as metrics,
and the Slack payload as an artifact so you can compare different
threshold configurations across runs.

Usage:
    # Via MLflow CLI (recommended)
    mlflow run . -e scan -P snapshot_age_days=60 -P cpu_threshold=2.0

    # Direct execution
    python mlflow_tracking.py --snapshot-age-days 60 --cpu-threshold 2.0 --dry-run 1
"""

import argparse
import json
import logging
import os
import tempfile
from datetime import datetime, timezone, timedelta

import boto3
import mlflow

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
logger = logging.getLogger(__name__)

# ── MLflow Config ────────────────────────────────────────────────────────────
EXPERIMENT_NAME = "HygieneBot-Scans"
TRACKING_URI = os.environ.get("MLFLOW_TRACKING_URI", "mlruns")


# ══════════════════════════════════════════════════════════════════════════════
# SCANNERS (mirrors Lambda logic but decoupled for local experiment runs)
# ══════════════════════════════════════════════════════════════════════════════

def _get_tag(tags: list[dict], key: str) -> str | None:
    return next((t["Value"] for t in tags if t["Key"] == key), None)


def scan_unattached_ebs(ec2_client) -> list[dict]:
    """Find EBS volumes in 'available' state (not attached to any instance)."""
    results = []
    for page in ec2_client.get_paginator("describe_volumes").paginate(
        Filters=[{"Name": "status", "Values": ["available"]}]
    ):
        for vol in page["Volumes"]:
            name = _get_tag(vol.get("Tags", []), "Name") or "(no name)"
            results.append({
                "resource_type": "ebs",
                "id": vol["VolumeId"],
                "label": f"{name} — {vol['Size']} GB {vol['VolumeType']}",
                "size_gb": vol["Size"],
                "created": vol["CreateTime"].isoformat(),
            })
    return results


def scan_idle_ec2(ec2_client, cw_client, lookback_days: int, cpu_threshold: float) -> list[dict]:
    """Find running EC2 instances whose average CPU over `lookback_days` <= `cpu_threshold`."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=lookback_days)
    results = []

    for page in ec2_client.get_paginator("describe_instances").paginate(
        Filters=[{"Name": "instance-state-name", "Values": ["running"]}]
    ):
        for res in page["Reservations"]:
            for inst in res["Instances"]:
                iid = inst["InstanceId"]
                resp = cw_client.get_metric_statistics(
                    Namespace="AWS/EC2",
                    MetricName="CPUUtilization",
                    Dimensions=[{"Name": "InstanceId", "Value": iid}],
                    StartTime=start,
                    EndTime=now,
                    Period=int((now - start).total_seconds()),
                    Statistics=["Average"],
                    Unit="Percent",
                )
                dp = resp.get("Datapoints", [])
                avg_cpu = dp[0]["Average"] if dp else None
                if avg_cpu is not None and avg_cpu <= cpu_threshold:
                    name = _get_tag(inst.get("Tags", []), "Name") or "(no name)"
                    results.append({
                        "resource_type": "ec2",
                        "id": iid,
                        "label": f"{name} — {inst['InstanceType']}, avg CPU {round(avg_cpu, 2)}%",
                        "avg_cpu": round(avg_cpu, 2),
                        "created": inst["LaunchTime"].isoformat(),
                    })
    return results


def scan_old_snapshots(ec2_client, age_days: int) -> list[dict]:
    """Find self-owned snapshots older than `age_days`."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=age_days)
    account = boto3.client("sts").get_caller_identity()["Account"]
    results = []
    for page in ec2_client.get_paginator("describe_snapshots").paginate(OwnerIds=[account]):
        for snap in page["Snapshots"]:
            if snap["StartTime"] < cutoff:
                name = _get_tag(snap.get("Tags", []), "Name") or "(no name)"
                age = (datetime.now(timezone.utc) - snap["StartTime"]).days
                results.append({
                    "resource_type": "snapshot",
                    "id": snap["SnapshotId"],
                    "label": f"{name} — {snap['VolumeSize']} GB, {age} days old",
                    "age_days": age,
                    "size_gb": snap["VolumeSize"],
                    "created": snap["StartTime"].isoformat(),
                })
    return results


def scan_untagged_instances(ec2_client) -> list[dict]:
    """Find EC2 instances with zero tags."""
    results = []
    for page in ec2_client.get_paginator("describe_instances").paginate():
        for res in page["Reservations"]:
            for inst in res["Instances"]:
                if not inst.get("Tags"):
                    results.append({
                        "resource_type": "untagged",
                        "id": inst["InstanceId"],
                        "label": f"{inst['InstanceType']} — no tags",
                        "created": inst["LaunchTime"].isoformat(),
                    })
    return results


# ══════════════════════════════════════════════════════════════════════════════
# COST ESTIMATION
# ══════════════════════════════════════════════════════════════════════════════

COST_MAP = {"ebs": 2.0, "ec2": 10.0, "snapshot": 1.0, "untagged": 10.0}


def estimate_savings(resources: list[dict]) -> float:
    return sum(COST_MAP.get(r["resource_type"], 0) for r in resources)


# ══════════════════════════════════════════════════════════════════════════════
# MLFLOW RUN
# ══════════════════════════════════════════════════════════════════════════════

def run_tracked_scan(
    snapshot_age_days: int = 90,
    cpu_lookback_days: int = 7,
    cpu_threshold: float = 1.0,
    dry_run: bool = True,
) -> None:
    """Execute a full scan and log everything to MLflow."""

    mlflow.set_tracking_uri(TRACKING_URI)
    mlflow.set_experiment(EXPERIMENT_NAME)

    region = os.environ.get("AWS_DEFAULT_REGION", os.environ.get("AWS_REGION", "us-east-1"))
    ec2_client = boto3.client("ec2", region_name=region)
    cw_client = boto3.client("cloudwatch", region_name=region)

    with mlflow.start_run(run_name=f"scan-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}") as run:
        logger.info("MLflow run started: %s", run.info.run_id)

        # ── Log parameters ───────────────────────────────────────────
        mlflow.log_params({
            "snapshot_age_days": snapshot_age_days,
            "cpu_lookback_days": cpu_lookback_days,
            "cpu_threshold": cpu_threshold,
            "aws_region": region,
            "dry_run": dry_run,
        })

        # ── Execute scans ────────────────────────────────────────────
        logger.info("Scanning unattached EBS volumes …")
        ebs = scan_unattached_ebs(ec2_client)

        logger.info("Scanning idle EC2 instances (CPU ≤ %.1f%% over %d days) …", cpu_threshold, cpu_lookback_days)
        ec2 = scan_idle_ec2(ec2_client, cw_client, cpu_lookback_days, cpu_threshold)

        logger.info("Scanning snapshots older than %d days …", snapshot_age_days)
        snapshots = scan_old_snapshots(ec2_client, snapshot_age_days)

        logger.info("Scanning untagged EC2 instances …")
        untagged = scan_untagged_instances(ec2_client)

        all_resources = ebs + ec2 + snapshots + untagged
        estimated_savings = estimate_savings(all_resources)

        # ── Log metrics ──────────────────────────────────────────────
        mlflow.log_metrics({
            "total_zombies_found":     len(all_resources),
            "unattached_ebs_count":    len(ebs),
            "idle_ec2_count":          len(ec2),
            "old_snapshot_count":      len(snapshots),
            "untagged_instance_count": len(untagged),
            "estimated_monthly_savings_usd": estimated_savings,
        })

        # ── Log total EBS waste in GB ────────────────────────────────
        total_ebs_gb = sum(r.get("size_gb", 0) for r in ebs)
        total_snap_gb = sum(r.get("size_gb", 0) for r in snapshots)
        mlflow.log_metrics({
            "wasted_ebs_gb": total_ebs_gb,
            "wasted_snapshot_gb": total_snap_gb,
        })

        # ── Log scan results as JSON artifact ────────────────────────
        with tempfile.TemporaryDirectory() as tmpdir:
            report = {
                "scan_time": datetime.now(timezone.utc).isoformat(),
                "parameters": {
                    "snapshot_age_days": snapshot_age_days,
                    "cpu_lookback_days": cpu_lookback_days,
                    "cpu_threshold": cpu_threshold,
                    "region": region,
                },
                "summary": {
                    "ebs": len(ebs),
                    "ec2": len(ec2),
                    "snapshots": len(snapshots),
                    "untagged": len(untagged),
                    "estimated_monthly_savings_usd": estimated_savings,
                },
                "resources": all_resources,
            }
            report_path = os.path.join(tmpdir, "scan_report.json")
            with open(report_path, "w") as f:
                json.dump(report, f, indent=2, default=str)
            mlflow.log_artifact(report_path, artifact_path="reports")

        # ── Tag the run ──────────────────────────────────────────────
        mlflow.set_tags({
            "project": "HygieneBot",
            "run_type": "dry_run" if dry_run else "live",
            "scan_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        })

        logger.info(
            "Scan complete — %d zombies found | Est. savings: $%.2f/mo | Run: %s",
            len(all_resources), estimated_savings, run.info.run_id,
        )


# ══════════════════════════════════════════════════════════════════════════════
# CLI ENTRYPOINT
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="HygieneBot MLflow-tracked scan")
    parser.add_argument("--snapshot-age-days", type=int, default=90,
                        help="Flag snapshots older than N days (default: 90)")
    parser.add_argument("--cpu-lookback-days", type=int, default=7,
                        help="CPU metric lookback window in days (default: 7)")
    parser.add_argument("--cpu-threshold", type=float, default=1.0,
                        help="CPU%% threshold below which an instance is 'idle' (default: 1.0)")
    parser.add_argument("--dry-run", type=int, default=1,
                        help="1 = scan only (default), 0 = live mode")
    args = parser.parse_args()

    run_tracked_scan(
        snapshot_age_days=args.snapshot_age_days,
        cpu_lookback_days=args.cpu_lookback_days,
        cpu_threshold=args.cpu_threshold,
        dry_run=bool(args.dry_run),
    )


if __name__ == "__main__":
    main()
