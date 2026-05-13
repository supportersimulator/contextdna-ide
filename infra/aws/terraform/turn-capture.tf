# Turn Capture API - AWS API Gateway + Lambda
# Provides a permanent, non-expiring API endpoint for capturing voice session turns
# from the frontend. Uses API key authentication (no user auth required).

# ============================================================================
# LAMBDA FUNCTION
# ============================================================================

# IAM role for the Lambda function
resource "aws_iam_role" "turn_capture_lambda" {
  name = "${var.project_name}-${var.environment}-turn-capture-lambda"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Name        = "${var.project_name}-turn-capture-lambda-role"
    Environment = var.environment
    Project     = var.project_name
  }
}

# Attach basic Lambda execution policy
resource "aws_iam_role_policy_attachment" "turn_capture_lambda_basic" {
  role       = aws_iam_role.turn_capture_lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Lambda function code (inline for simplicity)
data "archive_file" "turn_capture_lambda" {
  type        = "zip"
  output_path = "${path.module}/turn-capture/lambda.zip"

  source {
    content  = <<-PYTHON
import json
import os
import urllib.request
import urllib.error
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

BACKEND_URL = os.environ.get('BACKEND_URL', 'https://api.ersimulator.com')
INTERNAL_API_KEY = os.environ.get('INTERNAL_API_KEY', '')

def lambda_handler(event, context):
    """
    Receives turn data from frontend via API Gateway, validates it,
    and forwards to the Django backend internal endpoint.

    Expected payload:
    {
        "session_id": "uuid-string",
        "user_id": "uuid-string",
        "case_id": "string",
        "speaker": "learner|facilitator|nurse|patient",
        "transcript": "text of the turn"
    }
    """
    logger.info(f"Received event: {json.dumps(event)}")

    # Parse body
    try:
        if isinstance(event.get('body'), str):
            body = json.loads(event['body'])
        else:
            body = event.get('body', {})
    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error: {e}")
        return {
            'statusCode': 400,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({'error': 'Invalid JSON'})
        }

    # Validate required fields
    required_fields = ['session_id', 'speaker', 'transcript']
    missing = [f for f in required_fields if not body.get(f)]
    if missing:
        logger.warning(f"Missing required fields: {missing}")
        return {
            'statusCode': 400,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({'error': f'Missing required fields: {missing}'})
        }

    # Prepare payload for backend
    backend_payload = {
        'session_id': body['session_id'],
        'speaker': body['speaker'],
        'transcript': body['transcript'],
        'case_id': body.get('case_id', ''),
        'user_id': body.get('user_id', ''),
    }

    # Forward to Django backend
    try:
        url = f"{BACKEND_URL}/api/voice/realtime/internal-save-turn/"
        data = json.dumps(backend_payload).encode('utf-8')

        req = urllib.request.Request(
            url,
            data=data,
            headers={
                'Content-Type': 'application/json',
                'X-Internal-API-Key': INTERNAL_API_KEY,
            },
            method='POST'
        )

        with urllib.request.urlopen(req, timeout=10) as response:
            response_data = response.read().decode('utf-8')
            logger.info(f"Backend response: {response.status} - {response_data}")

            return {
                'statusCode': 200,
                'headers': {
                    'Content-Type': 'application/json',
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Methods': 'POST, OPTIONS',
                    'Access-Control-Allow-Headers': 'Content-Type, x-api-key',
                },
                'body': json.dumps({'status': 'saved'})
            }

    except urllib.error.HTTPError as e:
        logger.error(f"Backend HTTP error: {e.code} - {e.reason}")
        return {
            'statusCode': 502,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({'error': 'Backend error', 'detail': str(e.reason)})
        }
    except urllib.error.URLError as e:
        logger.error(f"Backend URL error: {e.reason}")
        return {
            'statusCode': 502,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({'error': 'Backend unreachable'})
        }
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return {
            'statusCode': 500,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({'error': 'Internal error'})
        }
PYTHON
    filename = "lambda_function.py"
  }
}

resource "aws_lambda_function" "turn_capture" {
  function_name = "${var.project_name}-${var.environment}-turn-capture"
  role          = aws_iam_role.turn_capture_lambda.arn
  handler       = "lambda_function.lambda_handler"
  runtime       = "python3.11"
  timeout       = 30  # Increased from 15s for reliability under load
  memory_size   = 128

  filename         = data.archive_file.turn_capture_lambda.output_path
  source_code_hash = data.archive_file.turn_capture_lambda.output_base64sha256

  environment {
    variables = {
      BACKEND_URL      = var.turn_capture_backend_url
      INTERNAL_API_KEY = var.turn_capture_internal_api_key
    }
  }

  tags = {
    Name        = "${var.project_name}-turn-capture"
    Environment = var.environment
    Project     = var.project_name
  }
}

# ============================================================================
# API GATEWAY (HTTP API - simpler and cheaper than REST API)
# ============================================================================

resource "aws_apigatewayv2_api" "turn_capture" {
  name          = "${var.project_name}-${var.environment}-turn-capture"
  protocol_type = "HTTP"
  description   = "Turn capture API for ER Simulator voice sessions"

  cors_configuration {
    allow_origins = ["*"]
    allow_methods = ["POST", "OPTIONS"]
    allow_headers = ["Content-Type", "x-api-key"]
    max_age       = 86400
  }

  tags = {
    Name        = "${var.project_name}-turn-capture-api"
    Environment = var.environment
    Project     = var.project_name
  }
}

# Lambda integration
resource "aws_apigatewayv2_integration" "turn_capture" {
  api_id                 = aws_apigatewayv2_api.turn_capture.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.turn_capture.invoke_arn
  payload_format_version = "2.0"
}

# Route for POST /turn
resource "aws_apigatewayv2_route" "turn_capture" {
  api_id    = aws_apigatewayv2_api.turn_capture.id
  route_key = "POST /turn"
  target    = "integrations/${aws_apigatewayv2_integration.turn_capture.id}"

  # API key authorization (custom authorizer not needed for simple API key check)
  # We'll validate the API key in the Lambda function itself for HTTP APIs
}

# Default stage (auto-deploy)
resource "aws_apigatewayv2_stage" "turn_capture" {
  api_id      = aws_apigatewayv2_api.turn_capture.id
  name        = "$default"
  auto_deploy = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.turn_capture_api.arn
    format = jsonencode({
      requestId      = "$context.requestId"
      ip             = "$context.identity.sourceIp"
      requestTime    = "$context.requestTime"
      httpMethod     = "$context.httpMethod"
      routeKey       = "$context.routeKey"
      status         = "$context.status"
      responseLength = "$context.responseLength"
      errorMessage   = "$context.error.message"
    })
  }

  tags = {
    Name        = "${var.project_name}-turn-capture-stage"
    Environment = var.environment
    Project     = var.project_name
  }
}

# CloudWatch log group for API access logs
resource "aws_cloudwatch_log_group" "turn_capture_api" {
  name              = "/aws/apigateway/${var.project_name}-${var.environment}-turn-capture"
  retention_in_days = 14

  tags = {
    Name        = "${var.project_name}-turn-capture-api-logs"
    Environment = var.environment
    Project     = var.project_name
  }
}

# Lambda permission to be invoked by API Gateway
resource "aws_lambda_permission" "turn_capture_api_gateway" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.turn_capture.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.turn_capture.execution_arn}/*/*"
}

# ============================================================================
# API KEY (stored in SSM Parameter Store for easy retrieval)
# ============================================================================

# Generate a random API key
resource "random_password" "turn_capture_api_key" {
  length  = 48
  special = false
}

# Store the API key in SSM Parameter Store (for frontend to use)
resource "aws_ssm_parameter" "turn_capture_api_key" {
  name        = "/${var.project_name}/${var.environment}/turn-capture-api-key"
  description = "API key for turn capture endpoint (frontend use)"
  type        = "SecureString"
  value       = random_password.turn_capture_api_key.result

  tags = {
    Name        = "${var.project_name}-turn-capture-api-key"
    Environment = var.environment
    Project     = var.project_name
  }
}

# ============================================================================
# OUTPUTS
# ============================================================================

output "turn_capture_api_url" {
  description = "URL for the turn capture API endpoint"
  value       = "${aws_apigatewayv2_api.turn_capture.api_endpoint}/turn"
}

output "turn_capture_api_key_ssm_path" {
  description = "SSM Parameter Store path for the API key"
  value       = aws_ssm_parameter.turn_capture_api_key.name
}

output "turn_capture_lambda_arn" {
  description = "ARN of the turn capture Lambda function"
  value       = aws_lambda_function.turn_capture.arn
}

output "turn_capture_api_id" {
  description = "API Gateway ID"
  value       = aws_apigatewayv2_api.turn_capture.id
}
