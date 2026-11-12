# tender-radar infra runbook

Region `us-east-2`, profile `tender-radar`. Everything is tagged `Project=tender-radar`.

```
infra/bootstrap/   S3 bucket for Terraform state (local state, gitignored)
infra/prod/        ECR, IAM, 2 Lambdas, HTTP API, daily schedule (state in that bucket)
infra/scripts/     build_push.sh: build arm64 images, push api-<sha> / worker-<sha>
```

What gets created, and what doesn't: no VPC, NAT, ALB, RDS, or provisioned/reserved
concurrency. The database is Neon: the API and the daily run use the pooled URL, and only
`{"task":"migrate"}` uses the direct URL. Secrets live only in SSM SecureStrings that
you create by hand; Terraform knows their **names** and never reads their values, so the
values never reach state.

| Piece | Config |
|---|---|
| `tender-radar-api` | image `api-<sha>`, arm64, 512 MB, 30 s, Lambda Web Adapter → gunicorn :8080, readiness `/api/health` |
| `tender-radar-worker` | image `worker-<sha>`, arm64, 1024 MB, 900 s, 1024 MB `/tmp`, handler `tenders.worker.handler`, 0 async retries, `LLM_DAILY_BUDGET_USD=0.15` |
| HTTP API | `$default` stage, auto-deploy, `ANY /` + `ANY /{proxy+}`, throttle 10 rps / burst 20, JSON access logs |
| Scheduler | `tender-radar-daily`, 06:15 America/St_Johns, `{"task":"daily"}`, 0 retries, **DISABLED** until step (h) |
| Logs | `/aws/lambda/tender-radar-{api,worker}`, `/aws/apigateway/tender-radar`, 14-day retention |
| IAM | api-role, worker-role: own log group + `ssm:GetParameter` on `/tender-radar/prod/*`. scheduler-role: invoke the worker only |

All commands run from the repo root unless a `cd` says otherwise. Set these once per shell:

```sh
export AWS_PROFILE=tender-radar
export AWS_REGION=us-east-2
```

## First deploy

### (a) Bootstrap the state bucket

```sh
cd infra/bootstrap
terraform init
terraform plan -out=bootstrap.tfplan      # expect 6 to add, bucket tender-radar-tfstate-<account id>
terraform apply bootstrap.tfplan
terraform output -raw state_bucket
cd ../..
```

`infra/bootstrap/terraform.tfstate` is gitignored. Keep it; it's how Terraform remembers it
owns the bucket.

### (b) Prod init, then ECR only

The images must exist before the Lambdas can be created, so the repository goes first.
`build_push.sh` refuses a dirty tree, so commit everything before this step.

```sh
export TF_VAR_git_sha="$(git rev-parse HEAD)"
cd infra/prod
terraform init -backend-config="bucket=$(terraform -chdir=../bootstrap output -raw state_bucket)"
terraform plan -out=ecr.tfplan \
  -target=aws_ecr_repository.app \
  -target=aws_ecr_lifecycle_policy.app \
  -target=aws_ecr_repository_policy.app
terraform apply ecr.tfplan                # Terraform warns about -target; expected here
cd ../..
```

### (c) Create the four SecureStrings yourself

For each parameter: copy the value to the clipboard, run the command, then clear the
clipboard. `"$(pbpaste)"` means shell history holds only the literal text `$(pbpaste)`, and
the AWS CLI prints the version number, not the value.

```sh
# 1. DATABASE_URL: the Neon POOLED production URL (host contains -pooler, sslmode=require)
aws ssm put-parameter --no-cli-pager --type SecureString \
  --name /tender-radar/prod/DATABASE_URL --value "$(pbpaste)"
pbcopy < /dev/null

# 2. ANTHROPIC_API_KEY: a dedicated production key
aws ssm put-parameter --no-cli-pager --type SecureString \
  --name /tender-radar/prod/ANTHROPIC_API_KEY --value "$(pbpaste)"
pbcopy < /dev/null

# 3. DJANGO_SECRET_KEY: generate one straight into the clipboard, never displayed
python3 -c "import secrets; print(secrets.token_urlsafe(50), end='')" | pbcopy
aws ssm put-parameter --no-cli-pager --type SecureString \
  --name /tender-radar/prod/DJANGO_SECRET_KEY --value "$(pbpaste)"
pbcopy < /dev/null

# 4. database_url_direct: the Neon DIRECT production URL (same database, host WITHOUT
#    -pooler, sslmode=require). Lowercase on purpose: it isn't loaded at cold start like the
#    three above, only by the worker's "migrate" task, as DATABASE_URL_DIRECT. Migrations
#    get a real session connection instead of PgBouncer's transaction mode.
aws ssm put-parameter --no-cli-pager --type SecureString \
  --name /tender-radar/prod/database_url_direct --value "$(pbpaste)"
pbcopy < /dev/null

# Check the names (expect 4), without values:
aws ssm describe-parameters --no-cli-pager \
  --parameter-filters "Key=Name,Option=BeginsWith,Values=/tender-radar/prod/" \
  --query "Parameters[].[Name,Type]" --output table
```

No `--key-id`: the default AWS-managed key `aws/ssm` is free, and its key policy already
lets SSM decrypt for principals in this account. That's why the roles have no `kms:Decrypt`.
IAM already covers all four, because both roles may `ssm:GetParameter` on
`/tender-radar/prod/*`. (The API role could read `database_url_direct` too but never asks
for it. Splitting that off would mean listing parameter ARNs per role.)
To rotate a value later, add `--overwrite`; running Lambdas pick it up on their next cold
start (or after a redeploy).

### (d) Build and push the images

```sh
infra/scripts/build_push.sh "$(git rev-parse HEAD)"
```

### (e) Full apply

```sh
cd infra/prod
terraform plan -out=prod.tfplan           # TF_VAR_git_sha from step (b), same commit as (d)
terraform apply prod.tfplan
terraform output api_url
cd ../..
```

### (f) Run migrations

`AWS_MAX_ATTEMPTS=1` and `--cli-read-timeout` matter. By default the CLI gives up reading
after 60 s and *re-sends* the invoke, which would start a second run.

```sh
AWS_MAX_ATTEMPTS=1 aws lambda invoke --no-cli-pager \
  --function-name tender-radar-worker \
  --cli-binary-format raw-in-base64-out --payload '{"task":"migrate"}' \
  --cli-read-timeout 910 /tmp/tr-migrate.json
cat /tmp/tr-migrate.json; echo
```

Success: the CLI output has no `"FunctionError"`, and the file shows `"ok": true`. Logs:
`aws logs tail /aws/lambda/tender-radar-worker --since 15m`.

### (g) Health checks

```sh
API="$(terraform -chdir=infra/prod output -raw api_url)"
curl -sS "${API%/}/api/health";    echo   # {"status": "ok", "version": "<sha>"}; never touches Neon
curl -sS "${API%/}/api/health/db"; echo   # {"status": "ok", "db": "ok"}; runs SELECT 1
```

The first call after a deploy is a cold start (a few seconds). `/api/health/db` can take
about a second longer if Neon was suspended.

### (h) One manual daily run, then enable the schedule

```sh
AWS_MAX_ATTEMPTS=1 aws lambda invoke --no-cli-pager \
  --function-name tender-radar-worker \
  --cli-binary-format raw-in-base64-out --payload '{"task":"daily"}' \
  --cli-read-timeout 910 /tmp/tr-daily.json
cat /tmp/tr-daily.json; echo
aws logs tail /aws/lambda/tender-radar-worker --since 30m   # ingest/extract/assess output + JSON summary
```

If it succeeded (`"ok": true`, every step ok, LLM cost as expected in the extract totals),
set `schedule_enabled = true` in `infra/prod/terraform.tfvars`, commit, then:

```sh
cd infra/prod
terraform plan -out=prod.tfplan           # expect exactly 1 change: the schedule state
terraform apply prod.tfplan
cd ../..
aws scheduler get-schedule --no-cli-pager --name tender-radar-daily --query State
```

## Later deploys

```sh
git status                                  # clean, committed
export TF_VAR_git_sha="$(git rev-parse HEAD)"
infra/scripts/build_push.sh "$TF_VAR_git_sha"
cd infra/prod && terraform plan -out=prod.tfplan && terraform apply prod.tfplan && cd ../..
# with new migrations: step (f) again
```

ECR keeps the last 5 `api-*` and the last 5 `worker-*` images. To roll back, set
`TF_VAR_git_sha` to an older sha whose images still exist, then plan and apply.

## Monthly cost estimate: about $0–1

| Service | Usage | Cost |
|---|---|---|
| Lambda | 1 worker run/day (a few minutes at 1 GB) + light API traffic, well inside the always-free 1M requests / 400k GB-s | $0 |
| API Gateway HTTP API | under 1M requests: free for the first 12 months, then $1.00 per million | $0–0.01 |
| ECR | api and worker share base and dependency layers; roughly 0.3–0.7 GB with 5 deploys kept. 500 MB free for 12 months, then $0.10/GB-month | $0–0.10 |
| CloudWatch Logs | MBs per month, 14-day retention; 5 GB ingestion free | $0 |
| SSM Parameter Store | 4 standard SecureStrings with `aws/ssm`: free. KMS decrypts at cold start, inside the free 20k requests/month | $0 |
| EventBridge Scheduler | 30 invocations/month, 14M free | $0 |
| S3 state bucket | KBs, a few hundred requests | <$0.01 |
| ECR basic scan on push | free | $0 |
| Data transfer out | CSV downloads come *in* (free); calls to Neon/Anthropic are tiny | ~$0 |

Not AWS, but these are the real spend: Anthropic usage is capped per day by
`LLM_DAILY_BUDGET_USD`, set to $0.15 on the worker (a worst case of ~$4.50/month; the
local default stays $0.50), and Neon is on
its free tier. Optional: an AWS Budget with a $1 alert (the first two budgets are free).

## Tear it all down

```sh
# 1. Prod: everything, including ECR images (force_delete) and log groups
cd infra/prod
terraform plan -destroy -out=destroy.tfplan
terraform apply destroy.tfplan
cd ../..

# 2. The secrets (Terraform never managed them)
aws ssm delete-parameters --no-cli-pager --names \
  /tender-radar/prod/DATABASE_URL /tender-radar/prod/ANTHROPIC_API_KEY \
  /tender-radar/prod/DJANGO_SECRET_KEY /tender-radar/prod/database_url_direct

# 3. The state bucket (only after step 1: the prod state lives in it)
#    In infra/bootstrap/main.tf, delete the `lifecycle { prevent_destroy = true }` block and
#    add `force_destroy = true` to aws_s3_bucket.state (the bucket is versioned, so it can't
#    be deleted while any object version remains). Then:
cd infra/bootstrap
terraform apply                           # records force_destroy
terraform plan -destroy -out=destroy.tfplan
terraform apply destroy.tfplan
cd ../..
```

Check that nothing is left: `aws resourcegroupstaggingapi get-resources --no-cli-pager
--tag-filters Key=Project,Values=tender-radar --query "ResourceTagMappingList[].ResourceARN"`.
The tag index can lag deleted resources by a while.
