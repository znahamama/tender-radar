variable "region" {
  type    = string
  default = "us-east-2"
}

variable "git_sha" {
  # Read only when a Lambda is CREATED (first deploy, or after a destroy): lambda.tf ignores
  # image_uri afterwards, and .github/workflows/deploy.yml owns the images. Every plan still
  # needs a valid value; any sha works then, e.g. TF_VAR_git_sha=$(git rev-parse HEAD).
  description = "Commit whose images a newly created Lambda starts with: ECR tags api-<sha> and worker-<sha>. Ignored for existing functions (CI deploys images)."
  type        = string
  validation {
    condition     = can(regex("^[0-9a-f]{40}$", var.git_sha))
    error_message = "git_sha must be a full 40-character lowercase commit sha (git rev-parse HEAD)."
  }
}

variable "ssm_prefix" {
  description = "Path holding the SecureStrings DATABASE_URL, ANTHROPIC_API_KEY, DJANGO_SECRET_KEY, database_url_direct."
  type        = string
  default     = "/tender-radar/prod/"
  validation {
    condition     = can(regex("^/([A-Za-z0-9_.-]+/)+$", var.ssm_prefix))
    error_message = "ssm_prefix must look like /a/b/ (leading and trailing slash)."
  }
}

variable "schedule_enabled" {
  description = "Turn the daily worker schedule on. Leave false until a manual daily run has succeeded."
  type        = bool
  default     = false
}

variable "alarm_email" {
  description = "Address the alarm emails go to. Never committed: set it in the gitignored infra/prod/alerts.secret.tfvars and pass -var-file=alerts.secret.tfvars."
  type        = string
  sensitive   = true # kept out of plan output; it is still stored in state
  validation {
    condition     = can(regex("^[^@[:space:]]+@[^@[:space:]]+\\.[^@[:space:]]+$", var.alarm_email))
    error_message = "alarm_email must be an email address."
  }
}
