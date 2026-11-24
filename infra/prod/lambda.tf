# Both functions: container images (arm64) from the one ECR repo, no VPC (Neon and
# Anthropic are public endpoints, and a VPC would need a NAT gateway), no reserved or
# provisioned concurrency.
#
# Images: Terraform sets image_uri from var.git_sha only when it CREATES a function. After
# that, lifecycle.ignore_changes hands image_uri to the deploy workflow (deploy.tf role,
# aws lambda update-function-code), so a plan never shows or reverts an image change.

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
  # Lambda's CPU share scales with memory, and a cold start is CPU-bound Python imports.
  # 1024 vs 512 MB: ~half the import time in a local CPU-capped run, for ~$0 at this
  # traffic (~500 GB-s/month at most, inside the always-free 400k GB-s).
  memory_size = 1024
  timeout     = 30

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
      # Invoked asynchronously after a reviewer upload or delete ({"task": "extract_one"});
      # the API role may invoke this function and nothing else (iam.tf).
      WORKER_FUNCTION_NAME = aws_lambda_function.worker.function_name
    }
  }

  depends_on = [aws_cloudwatch_log_group.lambda, aws_iam_role_policy.lambda, aws_ecr_repository_policy.app]

  lifecycle {
    # CI owns the image (.github/workflows/deploy.yml); var.git_sha only picks it at create.
    ignore_changes = [image_uri]
  }
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

  lifecycle {
    # CI owns the image (.github/workflows/deploy.yml); var.git_sha only picks it at create.
    ignore_changes = [image_uri]
  }
}

# The scheduler and the API (extract_one) invoke asynchronously, and async Lambda invokes
# retry twice on error by default. Zero here as well as on the schedule, so a failed run
# never repeats LLM calls.
resource "aws_lambda_function_event_invoke_config" "worker" {
  function_name                = aws_lambda_function.worker.function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = 3600
}
