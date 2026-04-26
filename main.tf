provider "aws" {
  region = "us-east-1"
}

# -------------------------------------------------------------
# SQS Queue
# -------------------------------------------------------------
resource "aws_sqs_queue" "zombie_queue" {
  name                       = "hygienebot-pending-deletions"
  visibility_timeout_seconds = 300
}

# -------------------------------------------------------------
# IAM Configurations
# -------------------------------------------------------------
resource "aws_iam_role" "scanner_lambda_role" {
  name = "HygieneBotScannerRole"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "scanner_policy" {
  name = "HygieneBotScannerPolicy"
  role = aws_iam_role.scanner_lambda_role.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = [
          "ec2:DescribeInstances", "ec2:DescribeVolumes", "ec2:DescribeSnapshots", "ec2:DescribeTags",
          "cloudwatch:GetMetricStatistics", "cloudwatch:PutMetricData", "secretsmanager:GetSecretValue",
          "sqs:SendMessage"
        ]
        Effect   = "Allow"
        Resource = "*"
      },
      {
        Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Effect = "Allow"
        Resource = "*"
      }
    ]
  })
}

resource "aws_iam_role" "deleter_lambda_role" {
  name = "HygieneBotDeleterRole"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "deleter_policy" {
  name = "HygieneBotDeleterPolicy"
  role = aws_iam_role.deleter_lambda_role.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = [
          "ec2:TerminateInstances", "ec2:StopInstances", "ec2:DeleteVolume", "ec2:DeleteSnapshot", 
          "secretsmanager:GetSecretValue",
          "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes",
          "cloudwatch:PutMetricData"
        ]
        Effect   = "Allow"
        Resource = "*"
      },
      {
        Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Effect = "Allow"
        Resource = "*"
      }
    ]
  })
}

# -------------------------------------------------------------
# Lambda Functions
# -------------------------------------------------------------
data "archive_file" "lambda_zip" {
  type        = "zip"
  source_file = "scanner_lambda.py"
  output_path = "scanner_lambda.zip"
}

resource "aws_lambda_function" "scanner_lambda" {
  filename         = data.archive_file.lambda_zip.output_path
  function_name    = "HygieneBotScanner"
  role             = aws_iam_role.scanner_lambda_role.arn
  handler          = "scanner_lambda.lambda_handler"
  runtime          = "python3.11"
  timeout          = 300

  environment {
    variables = {
      SQS_QUEUE_URL   = aws_sqs_queue.zombie_queue.url
      SLACK_SECRET_ID = "hygienebot/slack"
    }
  }
}

resource "aws_lambda_function" "deleter_lambda" {
  filename         = "deleter_lambda.zip" # Assuming code exists
  function_name    = "HygieneBotDeleter"
  role             = aws_iam_role.deleter_lambda_role.arn
  handler          = "deleter_lambda.lambda_handler"
  runtime          = "python3.11"
  timeout          = 60

  environment {
    variables = {
      SQS_QUEUE_URL   = aws_sqs_queue.zombie_queue.url
      SLACK_SECRET_ID = "hygienebot/slack"
    }
  }
}

# -------------------------------------------------------------
# EventBridge Schedule (Every Friday)
# -------------------------------------------------------------
resource "aws_cloudwatch_event_rule" "weekly_scan" {
  name                = "HygieneBotWeeklyScan"
  description         = "Triggers HygieneBot Scanner every Friday"
  schedule_expression = "cron(0 12 ? * FRI *)"
}

resource "aws_cloudwatch_event_target" "scan_target" {
  rule      = aws_cloudwatch_event_rule.weekly_scan.name
  target_id = "TriggerScanner"
  arn       = aws_lambda_function.scanner_lambda.arn
}

resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.scanner_lambda.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.weekly_scan.arn
}

# -------------------------------------------------------------
# API Gateway for Slack Webhooks
# -------------------------------------------------------------
resource "aws_apigatewayv2_api" "slack_webhook_api" {
  name          = "HygieneBotSlackAPI"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "lambda_integration" {
  api_id           = aws_apigatewayv2_api.slack_webhook_api.id
  integration_type = "AWS_PROXY"
  integration_uri  = aws_lambda_function.deleter_lambda.invoke_arn
}

resource "aws_apigatewayv2_route" "slack_route" {
  api_id    = aws_apigatewayv2_api.slack_webhook_api.id
  route_key = "POST /slack/action"
  target    = "integrations/${aws_apigatewayv2_integration.lambda_integration.id}"
}

resource "aws_apigatewayv2_stage" "default_stage" {
  api_id      = aws_apigatewayv2_api.slack_webhook_api.id
  name        = "$default"
  auto_deploy = true
}

resource "aws_lambda_permission" "allow_apigateway" {
  statement_id  = "AllowExecutionFromAPIGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.deleter_lambda.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.slack_webhook_api.execution_arn}/*/*"
}

output "api_gateway_endpoint" {
  value = "${aws_apigatewayv2_api.slack_webhook_api.api_endpoint}/slack/action"
}
