output "api_url" {
  description = "Base URL of the HTTP API ($default stage)."
  value       = aws_apigatewayv2_stage.default.invoke_url
}

output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}

output "api_function_name" {
  value = aws_lambda_function.api.function_name
}

output "worker_function_name" {
  value = aws_lambda_function.worker.function_name
}

output "schedule_state" {
  value = aws_scheduler_schedule.daily.state
}

output "alerts_topic_arn" {
  value = aws_sns_topic.alerts.arn
}

output "deploy_role_arn" {
  description = "Role the GitHub deploy workflow assumes via OIDC: set it as the prod environment variable AWS_DEPLOY_ROLE_ARN."
  value       = aws_iam_role.deploy.arn
}
