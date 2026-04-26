import os
import json
import boto3
import hmac
import hashlib
import time
import urllib.parse
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

secrets_client = boto3.client('secretsmanager')
sqs_client = boto3.client('sqs')
ec2_client = boto3.client('ec2')

def get_slack_secret():
    secret_id = os.environ.get('SLACK_SECRET_ID', 'hygienebot/slack')
    try:
        response = secrets_client.get_secret_value(SecretId=secret_id)
        return json.loads(response['SecretString']).get('signing_secret', '')
    except Exception as e:
        logger.error(f"Failed to fetch Slack secret: {e}")
        return ""

def verify_slack_signature(headers, body, signing_secret):
    timestamp = headers.get('x-slack-request-timestamp', '')
    signature = headers.get('x-slack-signature', '')
    
    if not timestamp or not signature:
        return False
        
    if abs(time.time() - int(timestamp)) > 300: # 5 minutes
        return False
        
    sig_basestring = f"v0:{timestamp}:{body}"
    my_sig = 'v0=' + hmac.new(
        signing_secret.encode(),
        sig_basestring.encode(),
        hashlib.sha256
    ).hexdigest()
    
    return hmac.compare_digest(my_sig, signature)

def process_approved_selections(selected_options):
    cleaned_up = 0
    savings_realized = 0

    for opt in selected_options:
        val = opt.get('value', '')
        if not val or '|' not in val:
            continue
        if val == 'none':
            continue
            
        res_type, res_id = val.split('|', 1)
        
        try:
            if res_type == 'EBS':
                ec2_client.delete_volume(VolumeId=res_id)
                savings_realized += 2.0
            elif res_type == 'EC2':
                ec2_client.stop_instances(InstanceIds=[res_id])
                savings_realized += 10.0
            elif res_type == 'SNAPSHOT':
                ec2_client.delete_snapshot(SnapshotId=res_id)
                savings_realized += 1.0
            elif res_type == 'UNTAGGED':
                ec2_client.stop_instances(InstanceIds=[res_id])
                savings_realized += 10.0
                
            cleaned_up += 1
        except Exception as e:
            logger.error(f"Failed to process {val}: {e}")
            
    # Push to CloudWatch
    if cleaned_up > 0:
        try:
            cloudwatch_client = boto3.client('cloudwatch')
            cloudwatch_client.put_metric_data(
                Namespace='HygieneBot',
                MetricData=[
                    {'MetricName': 'ResourcesCleanedUp', 'Value': cleaned_up, 'Unit': 'Count'},
                    {'MetricName': 'SavingsRealized', 'Value': savings_realized, 'Unit': 'None'}
                ]
            )
        except Exception as e:
            logger.error(f"CW PutMetricData failed: {e}")
            
    return cleaned_up, savings_realized

def lambda_handler(event, context):
    headers = {k.lower(): v for k, v in event.get('headers', {}).items()}
    body = event.get('body', '')
    
    if event.get('isBase64Encoded'):
        import base64
        body = base64.b64decode(body).decode('utf-8')
        
    slack_signing_secret = get_slack_secret()
    if not verify_slack_signature(headers, body, slack_signing_secret):
        return {"statusCode": 401, "body": "Unauthorized - Signature Verification Failed"}

    parsed_body = urllib.parse.parse_qs(body)
    payload = json.loads(parsed_body.get('payload', [''])[0])
    
    action_info = payload['actions'][0]
    action_value = json.loads(action_info['value']) # Contains batch_id and action
    
    batch_id = action_value['batch_id']
    user_action = action_value['action']
    user_id = payload['user']['id']
    
    logger.info(f"User {user_id} trigged action {user_action} for batch {batch_id}")
    
    if user_action == 'approve':
        state_values = payload.get('state', {}).get('values', {})
        selections_block = state_values.get('selections_block', {})
        checkbox_selections = selections_block.get('checkbox_selections', {})
        selected_options = checkbox_selections.get('selected_options', [])
        
        cleaned, savings = process_approved_selections(selected_options)
        msg = f"Cleanup Approved! 🚀 Successfully processed {cleaned} resources. Realized Savings: ${savings:.2f}/mo."
    else:
        msg = "Cleanup Denied by user. Safely ignored. ❌"

    return {
        "statusCode": 200,
        "body": msg
    }
