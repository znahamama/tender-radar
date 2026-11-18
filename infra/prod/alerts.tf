# Failure alerting: four CloudWatch alarms -> one SNS topic -> one email address.
# Every alarm notifies on ALARM and on OK, so a recovery is visible too.
#
# The address is var.alarm_email (no default, sensitive), read from the gitignored
# alerts.secret.tfvars: terraform plan -var-file=alerts.secret.tfvars. It still ends up in
# the state file (encrypted S3 bucket), as every resource argument does.
#
# Email subscriptions start as PendingConfirmation: click the link AWS sends after apply,
# or nothing is delivered. Notifications published before that are dropped.

resource "aws_sns_topic" "alerts" {
  name = "${local.name}-alerts"
  # No KMS key: alarm messages hold metric names and counts, not secrets. (A CMK would also
  # need a key policy letting cloudwatch.amazonaws.com use it.)
}

resource "aws_sns_topic_subscription" "alerts_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

locals {
  alarms = {
    # (a) The run failed. worker.handler re-raises a failed step, and extract_tenders fails
    # on a bad key/model or a 400 on the first call; a timeout (900 s) counts too. A budget
    # cap is a clean exit and doesn't count. 0 async retries, so one failure = one Error.
    worker-errors = {
      description = "Worker invocation failed (a daily/migrate step raised, or the 900 s timeout hit). Logs: aws logs tail /aws/lambda/${local.name}-worker --since 2h"
      namespace   = "AWS/Lambda"
      metric      = "Errors"
      dimensions  = { FunctionName = aws_lambda_function.worker.function_name }
      period      = 3600
      operator    = "GreaterThanOrEqualToThreshold"
      threshold   = 1
      missing     = "notBreaching" # no invocation this hour = no error
    }

    # (b) The run never started. Invocations counts every start (scheduled or manual),
    # whatever the outcome; a failed run still counts here and trips (a) instead.
    # Sliding 26 h window, not 24: runs are 24 h apart, so a 24 h window could drop
    # yesterday's run just before today's datapoint arrives (Lambda metrics lag a minute or
    # so), and the autumn clock change in America/St_Johns makes one gap 25 h. The first
    # evaluation after apply looks back 26 h, so it's OK if anything ran in that time.
    # (Period x evaluation periods may be up to 7 days for periods of 1 h or more.)
    worker-not-run = {
      description = "No worker invocation in the last 26 h: the daily schedule didn't fire (disabled, deleted, or Scheduler couldn't invoke)."
      namespace   = "AWS/Lambda"
      metric      = "Invocations"
      dimensions  = { FunctionName = aws_lambda_function.worker.function_name }
      period      = 93600 # 26 h
      operator    = "LessThanThreshold"
      threshold   = 1
      missing     = "breaching" # Lambda publishes nothing at all when nothing ran
    }

    # (c) The site is failing. Errors = unhandled errors/timeouts of the function (the Web
    # Adapter returning a 5xx from Django is a normal response, not a Lambda Error).
    api-errors = {
      description = "API Lambda: 5 or more errors in 5 minutes. Logs: aws logs tail /aws/lambda/${local.name}-api --since 30m"
      namespace   = "AWS/Lambda"
      metric      = "Errors"
      dimensions  = { FunctionName = aws_lambda_function.api.function_name }
      period      = 300
      operator    = "GreaterThanOrEqualToThreshold"
      threshold   = 5
      missing     = "notBreaching"
    }

    # (d) Scheduler called Lambda and the Invoke API call itself failed (permissions, missing
    # function, ...). The async invoke returns 202 before the code runs, so a failing run
    # shows up in (a), not here. Scheduler has no per-schedule dimension, only ScheduleGroup;
    # tender-radar-daily is the only schedule in its group (default).
    scheduler-target-errors = {
      description = "EventBridge Scheduler failed to invoke the worker (TargetErrorCount, schedule group ${aws_scheduler_schedule.daily.group_name})."
      namespace   = "AWS/Scheduler"
      metric      = "TargetErrorCount"
      dimensions  = { ScheduleGroup = aws_scheduler_schedule.daily.group_name }
      period      = 3600
      operator    = "GreaterThanOrEqualToThreshold"
      threshold   = 1
      missing     = "notBreaching"
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "alerts" {
  for_each = local.alarms

  alarm_name        = "${local.name}-${each.key}"
  alarm_description = each.value.description

  namespace   = each.value.namespace
  metric_name = each.value.metric
  dimensions  = each.value.dimensions
  statistic   = "Sum"

  period              = each.value.period
  evaluation_periods  = 1
  comparison_operator = each.value.operator
  threshold           = each.value.threshold
  treat_missing_data  = each.value.missing

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}
