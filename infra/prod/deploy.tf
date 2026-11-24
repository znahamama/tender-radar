# GitHub Actions deploys (.github/workflows/deploy.yml) through OIDC: no AWS keys in GitHub.
#
# The workflow's deploy job runs in the GitHub environment "prod" and exchanges its OIDC
# token for this role's credentials (1 h at most). The trust policy accepts exactly one
# subject: jobs of znahamama/tender-radar that run in environment prod. No branch, tag or
# pull_request subject matches. Which branches may use "prod", and who approves a run, are
# environment settings in GitHub (infra/README.md, "Deploys from GitHub").
#
# The role can push images to this repository's ECR repo, point the two Lambdas at a new
# image and invoke the worker ({"task":"migrate"}). It can't change configuration, IAM,
# environment variables or anything Terraform owns besides the image (lambda.tf ignores
# image_uri so the two don't fight).

locals {
  github_oidc_host = "token.actions.githubusercontent.com"
  # Exact match, StringEquals. Changing the repo or environment name means changing this.
  github_deploy_subject = "repo:znahamama/tender-radar:environment:prod"
}

# One per account per URL. If the account already has one (another project), import it
# instead of creating a second: infra/README.md.
resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://${local.github_oidc_host}"
  client_id_list = ["sts.amazonaws.com"]
  # No thumbprint_list: AWS verifies GitHub's certificate against its own trusted CAs for
  # this provider, and provider v6 leaves the attribute optional.
}

data "aws_iam_policy_document" "deploy_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.github_oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.github_oidc_host}:sub"
      values   = [local.github_deploy_subject]
    }
  }
}

resource "aws_iam_role" "deploy" {
  name                 = "${local.name}-deploy"
  description          = "GitHub Actions deploys (environment prod) via OIDC"
  assume_role_policy   = data.aws_iam_policy_document.deploy_assume.json
  max_session_duration = 3600 # 1 h; the workflow asks for the same
}

data "aws_iam_policy_document" "deploy" {
  statement {
    sid = "EcrLogin"
    # GetAuthorizationToken has no resource-level permissions: "*" is the only option.
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid = "EcrPushPullThisRepo"
    # Push: the layer upload and PutImage. Pull: BatchGetImage (also how the workflow
    # checks whether api-<sha>/worker-<sha> already exist, so no DescribeImages) and
    # GetDownloadUrlForLayer. Tags are IMMUTABLE (ecr.tf): an existing tag can't be
    # overwritten, and there is no BatchDeleteImage here.
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = [aws_ecr_repository.app.arn]
  }

  statement {
    sid = "UpdateCode"
    # GetFunction backs `aws lambda wait function-updated-v2`. No UpdateFunctionConfiguration,
    # PublishVersion or alias actions.
    actions = [
      "lambda:GetFunction",
      "lambda:GetFunctionConfiguration",
      "lambda:UpdateFunctionCode",
    ]
    resources = [aws_lambda_function.api.arn, aws_lambda_function.worker.arn]
  }

  statement {
    sid       = "InvokeWorkerForMigrate"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.worker.arn]
  }
}

resource "aws_iam_role_policy" "deploy" {
  name   = "${local.name}-deploy"
  role   = aws_iam_role.deploy.id
  policy = data.aws_iam_policy_document.deploy.json
}
