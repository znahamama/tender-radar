resource "aws_ecr_repository" "app" {
  name                 = local.name
  image_tag_mutability = "IMMUTABLE"
  # Images are rebuildable from git, so destroy may delete them with the repo.
  force_delete = true

  image_scanning_configuration {
    scan_on_push = true # basic scanning: free
  }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name
  # Last 5 of EACH image kind (5 deploys to roll back to), not 5 in total: a single
  # "5 images" rule would mix api and worker tags and keep only 2-3 deploys.
  policy = jsonencode({
    rules = concat([
      for i, prefix in ["api-", "worker-"] : {
        rulePriority = i + 1
        description  = "keep the last 5 ${prefix}* images"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = [prefix]
          countType     = "imageCountMoreThan"
          countNumber   = 5
        }
        action = { type = "expire" }
      }
      ], [{
        rulePriority = 3
        description  = "drop untagged images after a day"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 1
        }
        action = { type = "expire" }
    }])
  })
}

# Lambda pulls images as its service principal. Granting it here (only for this account's
# tender-radar-* functions) means the execution roles need no ECR permissions, and Lambda
# doesn't edit the repository policy behind Terraform's back.
data "aws_iam_policy_document" "ecr_lambda_pull" {
  statement {
    sid     = "LambdaECRImageRetrievalPolicy"
    actions = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
    condition {
      test     = "StringLike"
      variable = "aws:sourceArn"
      values   = ["arn:aws:lambda:${var.region}:${local.account_id}:function:${local.name}-*"]
    }
  }
}

resource "aws_ecr_repository_policy" "app" {
  repository = aws_ecr_repository.app.name
  policy     = data.aws_iam_policy_document.ecr_lambda_pull.json
}
