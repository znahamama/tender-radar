terraform {
  required_version = ">= 1.10" # S3-native locking (use_lockfile)

  # Partial config: the bucket name includes the account id, so it's passed at init:
  #   terraform init -backend-config="bucket=$(terraform -chdir=../bootstrap output -raw state_bucket)"
  backend "s3" {
    key          = "prod/terraform.tfstate"
    region       = "us-east-2"
    encrypt      = true
    use_lockfile = true # lock object in the same bucket; no DynamoDB table
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.region
  # Credentials: AWS_PROFILE=tender-radar. No profile here so CI can validate without one.
  default_tags {
    tags = { Project = "tender-radar" }
  }
}

data "aws_caller_identity" "current" {}

locals {
  name       = "tender-radar"
  account_id = data.aws_caller_identity.current.account_id
  # SSM parameter ARNs drop the name's leading slash: /a/b/ -> parameter/a/b/*
  ssm_params_arn = "arn:aws:ssm:${var.region}:${local.account_id}:parameter${var.ssm_prefix}*"
  log_retention  = 14
}
