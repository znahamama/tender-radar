resource "aws_scheduler_schedule" "daily" {
  name        = "${local.name}-daily"
  description = "06:15 St. John's: ingest, extract Open tenders, assess"
  # Created DISABLED; enable with -var schedule_enabled=true after a good manual run.
  state = var.schedule_enabled ? "ENABLED" : "DISABLED"

  schedule_expression          = "cron(15 6 * * ? *)"
  schedule_expression_timezone = "America/St_Johns"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.worker.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ task = "daily" })

    retry_policy {
      maximum_retry_attempts       = 0
      maximum_event_age_in_seconds = 3600
    }
  }
}
