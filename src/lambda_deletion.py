import hashlib
import hmac
import json
import logging
import os
import time
import urllib.parse
import urllib.request

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ── Config ────────────────────────────────────────────────────────────────────
SLACK_SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]
ALLOWED_APPROVERS    = set(os.environ.get("ALLOWED_APPROVER_IDS", "").split(","))
AWS_REGION           = os.environ.get("AWS_REGION", "us-east-1")

ec2 = boto3.client("ec2", region_name=AWS_REGION)


# ══════════════════════════════════════════════════════════════════════════════
# SLACK SIGNATURE VERIFICATION
# ══════════════════════════════════════════════════════════════════════════════

def verify_slack_signature(headers: dict, raw_body: str) -> None:
    """
    Raise ValueError if the request does not have a valid Slack signature.
    Protects the endpoint from spoofed requests.
    """
    timestamp  = headers.get("x-slack-request-timestamp", "")
    sig_header = headers.get("x-slack-signature", "")

    # Reject replayed requests older than 5 minutes
    if abs(time.time() - int(timestamp)) > 300:
        raise ValueError("Request timestamp is stale — possible replay attack")

    sig_basestring = f"v0:{timestamp}:{raw_body}"
    computed = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(),
        sig_basestring.encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(computed, sig_header):
        raise ValueError("Invalid Slack signature")


# ══════════════════════════════════════════════════════════════════════════════
# DELETION HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

def delete_ebs_volume(volume_id: str) -> str:
    ec2.delete_volume(VolumeId=volume_id)
    return f"EBS volume {volume_id} deleted"


def delete_snapshot(snapshot_id: str) -> str:
    ec2.delete_snapshot(SnapshotId=snapshot_id)
    return f"Snapshot {snapshot_id} deleted"


def stop_ec2_instance(instance_id: str) -> str:
    """
    For idle EC2 instances we stop rather than terminate —
    safer default; termination can be a follow-up action.
    """
    ec2.stop_instances(InstanceIds=[instance_id])
    return f"EC2 instance {instance_id} stopped (not terminated)"


DELETION_HANDLERS = {
    "ebs":      delete_ebs_volume,
    "snapshot": delete_snapshot,
    "ec2":      stop_ec2_instance,
}


# ══════════════════════════════════════════════════════════════════════════════
# SLACK RESPONSE HELPER
# ══════════════════════════════════════════════════════════════════════════════

def _post_slack_response(response_url: str, text: str, color: str = "good") -> None:
    """Update the original Slack message to show outcome."""
    payload = {
        "replace_original": True,
        "attachments": [{"color": color, "text": text}],
    }
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(
        response_url,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=10)


# ══════════════════════════════════════════════════════════════════════════════
# LAMBDA ENTRYPOINT
# ══════════════════════════════════════════════════════════════════════════════

def lambda_handler(event: dict, context) -> dict:
    """
    Receives an API Gateway POST from Slack.
    Event body is URL-encoded (Slack interactive payloads are form-encoded).
    """
    raw_body = event.get("body", "")
    headers  = {k.lower(): v for k, v in event.get("headers", {}).items()}

    # 1. Verify Slack signature
    try:
        verify_slack_signature(headers, raw_body)
    except ValueError as exc:
        logger.warning("Signature verification failed: %s", exc)
        return {"statusCode": 403, "body": json.dumps({"error": str(exc)})}

    # 2. Decode Slack payload (form-encoded: payload=<json>)
    parsed      = urllib.parse.parse_qs(raw_body)
    payload_str = parsed.get("payload", ["{}"])[0]
    payload     = json.loads(payload_str)

    approver_id  = payload.get("user", {}).get("id", "")
    response_url = payload.get("response_url", "")
    actions      = payload.get("actions", [])

    if not actions:
        return {"statusCode": 400, "body": "No actions in payload"}

    action = actions[0]
    action_id = action.get("action_id", "")   # e.g. "approve_ebs_vol-0abc123"
    value     = action.get("value", "")       # e.g. "ebs::vol-0abc123"

    # 3. Check authorised approver
    if ALLOWED_APPROVERS and approver_id not in ALLOWED_APPROVERS:
        logger.warning("Unauthorised approver attempt: %s", approver_id)
        _post_slack_response(response_url, f":no_entry: <@{approver_id}> is not authorised to approve deletions.", color="danger")
        return {"statusCode": 403, "body": "Not authorised"}

    # 4. Handle deny action immediately
    if action_id.startswith("deny_") or value.startswith("deny::"):
        resource_id = value.split("::")[-1]
        logger.info("DENIAL logged | resource=%s | approver=%s", resource_id, approver_id)
        _post_slack_response(response_url, f":white_check_mark: Deletion of `{resource_id}` *denied* by <@{approver_id}>.")
        return {"statusCode": 200, "body": "Denial logged"}

    # 5. Parse resource type and ID from value (format: "resource_type::resource_id")
    parts = value.split("::")
    if len(parts) != 2:
        return {"statusCode": 400, "body": f"Unexpected value format: {value}"}

    resource_type, resource_id = parts
    handler = DELETION_HANDLERS.get(resource_type)

    if not handler:
        return {"statusCode": 400, "body": f"Unknown resource type: {resource_type}"}

    # 6. Execute deletion
    try:
        result_msg = handler(resource_id)
        logger.info("DELETION_SUCCESS | type=%s | id=%s | approver=%s", resource_type, resource_id, approver_id)
        _post_slack_response(
            response_url,
            f":white_check_mark: `{resource_id}` ({resource_type}) *deleted* by <@{approver_id}>.\n_{result_msg}_",
        )
        return {"statusCode": 200, "body": json.dumps({"deleted": resource_id})}

    except Exception as exc:
        logger.error("DELETION_FAILED | type=%s | id=%s | error=%s", resource_type, resource_id, exc)
        _post_slack_response(
            response_url,
            f":x: Failed to delete `{resource_id}`: {exc}",
            color="danger",
        )
        return {"statusCode": 500, "body": json.dumps({"error": str(exc)})}