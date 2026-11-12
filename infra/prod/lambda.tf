# Both functions: container images (arm64) from the one ECR repo, no VPC (Neon and
# Anthropic are public endpoints, and a VPC would need a NAT gateway), no reserved or
# provisioned concurrency.

resource "aws_cloudwatch_log_group" "lambda" {
  for_each          = local.lambdas
  name              = "/aws/lambda/${local.name}-${each.key}"
  retention_in_days = local.log_retention
}

resource "aws_lambda_function" "api" {
  function_name = "${local.name}-api"
  role          = aws_iam_role.lambda["api"].arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.app.repository_url}:api-${var.git_sha}"
  architectures = ["arm64"]
  memory_size   = 512
  timeout       = 30

  environment {
    variables = {
      DJANGO_DEBUG = "0"
      SSM_PREFIX   = var.ssm_prefix
      # 127.0.0.1 is the Web Adapter's readiness check (Host: 127.0.0.1:8080). Only the
      # adapter inside the sandbox can reach it; API Gateway always sends its own host.
      DJANGO_ALLOWED_HOSTS = ".execute-api.${var.region}.amazonaws.com,127.0.0.1"
      # The image sets these too; repeated so the function config documents itself.
      AWS_LWA_PORT                 = "8080"
      AWS_LWA_READINESS_CHECK_PATH = "/api/health"
      # Only a real 200 counts as ready (the default 100-499 would accept a 400 from a
      # host mismatch and route traffic to a broken app).
      AWS_LWA_READINESS_CHECK_HEALTHY_STATUS = "200-299"
    }
  }

  depends_on = [aws_cloudwatch_log_group.lambda, aws_iam_role_policy.lambda, aws_ecr_repository_policy.app]
}

resource "aws_lambda_function" "worker" {
  function_name = "${local.name}-worker"
  role          = aws_iam_role.lambda["worker"].arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.app.repository_url}:worker-${var.git_sha}"
  architectures = ["arm64"]
  memory_size   = 1024
  timeout       = 900

  ephemeral_storage {
    size = 1024 # MB of /tmp: the downloaded CanadaBuys CSVs
  }

  environment {
    variables = {
      DJANGO_DEBUG = "0"
      SSM_PREFIX   = var.ssm_prefix
      # Hard cap on Anthropic spend per day (~$4.50/month worst case). Local default: 0.50.
      LLM_DAILY_BUDGET_USD = "0.15"
    }
  }

  depends_on = [aws_cloudwatch_log_group.lambda, aws_iam_role_policy.lambda, aws_ecr_repository_policy.app]
}

# The scheduler invokes asynchronously, and async Lambda invokes retry twice on error by
# default. Zero here as well as on the schedule, so a failed run never repeats LLM calls.
resource "aws_lambda_function_event_invoke_config" "worker" {
  function_name                = aws_lambda_function.worker.function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = 3600
}
