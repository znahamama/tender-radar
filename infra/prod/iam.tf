# Execution roles. Each may write only its own log group and read only the
# /tender-radar/prod/* parameters.
#
# No kms:Decrypt: the SecureStrings use the AWS-managed key aws/ssm. Its key policy
# (owned by AWS, not editable) already allows kms:Decrypt for any principal in this account
# when the call comes through SSM (kms:ViaService = ssm.<region>.amazonaws.com and
# kms:CallerAccount = this account). With GetParameter allowed in IAM, decryption works.
# A customer-managed key would need kms:Decrypt here AND in its key policy.
#
# No ECR permissions: the repository policy in ecr.tf grants Lambda the image pull.

locals {
  lambdas = toset(["api", "worker"])
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  for_each           = local.lambdas
  name               = "${local.name}-${each.key}-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "lambda" {
  for_each = local.lambdas

  statement {
    sid       = "OwnLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.lambda[each.key].arn}:*"]
    # No logs:CreateLogGroup: Terraform creates the group (with 14-day retention), and
    # without that permission Lambda can't recreate it without a retention policy.
  }

  statement {
    sid       = "ReadProdParameters"
    actions   = ["ssm:GetParameter"]
    resources = [local.ssm_params_arn]
  }
}

resource "aws_iam_role_policy" "lambda" {
  for_each = local.lambdas
  name     = "${local.name}-${each.key}"
  role     = aws_iam_role.lambda[each.key].id
  policy   = data.aws_iam_policy_document.lambda[each.key].json
}

# EventBridge Scheduler needs a role it assumes to invoke the worker.
data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${local.name}-scheduler-role"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

data "aws_iam_policy_document" "scheduler" {
  statement {
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.worker.arn, "${aws_lambda_function.worker.arn}:*"]
  }
}

resource "aws_iam_role_policy" "scheduler" {
  name   = "${local.name}-scheduler-invoke-worker"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler.json
}
