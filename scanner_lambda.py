import os
import json
import boto3
import logging
import urllib.request
import uuid
from datetime import datetime, timezone, timedelta

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ec2_client = boto3.client('ec2')
cloudwatch_client = boto3.client('cloudwatch')
secrets_client = boto3.client('secretsmanager')
sqs_client = boto3.client('sqs')

def get_secret(secret_id):
    try:
        response = secrets_client.get_secret_value(SecretId=secret_id)
        return json.loads(response['SecretString'])
    except Exception as e:
        logger.error(f"Failed to retrieve secret {secret_id}: {e}")
        return {}

def find_unattached_ebs_volumes():
    volumes = ec2_client.describe_volumes(Filters=[{'Name': 'status', 'Values': ['available']}])
    return [v['VolumeId'] for v in volumes.get('Volumes', [])]

def find_idle_ec2_instances():
    idle_instances = []
    instances = ec2_client.describe_instances(Filters=[{'Name': 'instance-state-name', 'Values': ['running']}])
    for reservation in instances.get('Reservations', []):
        for instance in reservation.get('Instances', []):
            instance_id = instance['InstanceId']
            # Get CPU utilization for the past week
            metrics = cloudwatch_client.get_metric_statistics(
                Namespace='AWS/EC2',
                MetricName='CPUUtilization',
                Dimensions=[{'Name': 'InstanceId', 'Value': instance_id}],
                StartTime=datetime.now(timezone.utc) - timedelta(days=7),
                EndTime=datetime.now(timezone.utc),
                Period=86400,
                Statistics=['Average']
            )
            datapoints = metrics.get('Datapoints', [])
            # If all data points represent < 1% CPU usage
            if datapoints and all(dp['Average'] < 1.0 for dp in datapoints):
                idle_instances.append(instance_id)
    return idle_instances

def find_old_snapshots():
    old_snapshots = []
    paginator = ec2_client.get_paginator('describe_snapshots')
    # Use OwnerIds=['self'] to prevent scanning public snapshots
    for page in paginator.paginate(OwnerIds=['self']):
        for snap in page.get('Snapshots', []):
            if snap['StartTime'] < datetime.now(timezone.utc) - timedelta(days=90):
                old_snapshots.append(snap['SnapshotId'])
    return old_snapshots

def find_untagged_resources():
    untagged = []
    # Scanning only EC2 for simplicity depending on requirement
    instances = ec2_client.describe_instances()
    for reservation in instances.get('Reservations', []):
        for instance in reservation.get('Instances', []):
            if not instance.get('Tags'):
                untagged.append(instance['InstanceId'])
    return untagged

def send_slack_notification(webhook_url, summary, batch_id, options, total_savings):
    if not options:
        options = [{"text": {"type": "plain_text", "text": "No specific resources"}, "value": "none"}]
    
    payload = {
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "🧹 HygieneBot: Action Required"}
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Estimated Waste: ${total_savings:.2f} / month*\n\n"
                            f"• {summary['ebs']} Unattached EBS Volumes\n"
                            f"• {summary['ec2']} Idle EC2 Instances (0% CPU)\n"
                            f"• {summary['snapshots']} Old Snapshots (> 90 days)\n"
                            f"• {summary['untagged']} Untagged Resources\n\n"
                            f"Batch ID: `{batch_id}`"
                }
            },
            {
                "type": "input",
                "block_id": "selections_block",
                "element": {
                    "type": "checkboxes",
                    "options": options,
                    "action_id": "checkbox_selections"
                },
                "label": {
                    "type": "plain_text",
                    "text": "Select resources to clean up/stop:"
                }
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Approve Selected"},
                        "style": "danger",
                        "value": json.dumps({"batch_id": batch_id, "action": "approve"}),
                        "action_id": "approve_cleanup"
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Deny"},
                        "value": json.dumps({"batch_id": batch_id, "action": "deny"}),
                        "action_id": "deny_cleanup"
                    }
                ]
            }
        ]
    }
    req = urllib.request.Request(webhook_url, data=json.dumps(payload).encode('utf-8'), headers={'Content-Type': 'application/json'})
    urllib.request.urlopen(req)

def lambda_handler(event, context):
    logger.info("Starting Scanner execution")
    batch_id = str(uuid.uuid4())
    
    ebs = find_unattached_ebs_volumes()
    ec2 = find_idle_ec2_instances()
    snapshots = find_old_snapshots()
    untagged = find_untagged_resources()
    
    options = []
    total_savings = 0
    seen = set()

    def add_option(res_id, res_type, cost, label):
        nonlocal total_savings
        val = f"{res_type}|{res_id}"
        if val not in seen and len(options) < 100:
            seen.add(val)
            total_savings += cost
            options.append({
                "text": {"type": "plain_text", "text": f"{label}: {res_id} (Save ${cost}/mo)"},
                "value": val
            })

    for r in ebs: add_option(r, "EBS", 2.0, "EBS Vol")
    for r in ec2: add_option(r, "EC2", 10.0, "Idle EC2")
    for r in snapshots: add_option(r, "SNAPSHOT", 1.0, "Old Snap")
    for r in untagged: add_option(r, "UNTAGGED", 10.0, "Untagged EC2")
    
    summary = {
        "ebs": len(ebs),
        "ec2": len(ec2),
        "snapshots": len(snapshots),
        "untagged": len(untagged)
    }
    
    total = sum(summary.values())
    if total > 0:
        try:
            cloudwatch_client.put_metric_data(
                Namespace='HygieneBot',
                MetricData=[
                    {'MetricName': 'ZombiesFound', 'Value': total, 'Unit': 'Count'},
                    {'MetricName': 'EstimatedSavings', 'Value': total_savings, 'Unit': 'None'}
                ]
            )
        except Exception as e:
            logger.error(f"CW PutMetricData failed: {e}")

        slack_secret = get_secret(os.environ.get('SLACK_SECRET_ID', 'hygienebot/slack'))
        webhook_url = slack_secret.get('webhook_url')
        if webhook_url:
            send_slack_notification(webhook_url, summary, batch_id, options, total_savings)
            logger.info("Slack notification sent successfully.")
        else:
            logger.error("No Slack webhook URL configured.")
            
    return {
        'statusCode': 200,
        'body': json.dumps({'message': f'Scan complete. {total} resources found.'})
    }
