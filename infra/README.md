# tender-radar infra runbook

Region `us-east-2`. Profiles: `tender-radar` for the AWS CLI and `tender-radar-tf` for
Terraform (see below). Everything is tagged `Project=tender-radar`.

```
infra/bootstrap/   S3 bucket for Terraform state (local state, gitignored)
infra/prod/        ECR, IAM, 2 Lambdas, HTTP API, daily schedule, alarms (state in that bucket)
infra/scripts/     build_push.sh: build arm64 images, push api-<sha> / worker-<sha> (first deploy, emergencies)
.github/workflows/deploy.yml   every push to main: CI, approval, build, worker -> migrate -> API -> health
```

What gets created, and what doesn't: no VPC, NAT, ALB, RDS, or provisioned/reserved
concurrency. The database is Neon: the API and the daily run use the pooled URL, and only
`{"task":"migrate"}` uses the direct URL. Secrets live only in SSM SecureStrings that
you create by hand; Terraform knows their **names** and never reads their values, so the
values never reach state.

| Piece | Config |
|---|---|
| `tender-radar-api` | image `api-<sha>`, arm64, 1024 MB, 30 s, Lambda Web Adapter → gunicorn :8080, readiness `/api/health`, `WORKER_FUNCTION_NAME` (reviewer uploads) |
| `tender-radar-worker` | image `worker-<sha>`, arm64, 1024 MB, 900 s, 1024 MB `/tmp`, handler `tenders.worker.handler`, 0 async retries, `LLM_DAILY_BUDGET_USD=0.15` |
| HTTP API | `$default` stage, auto-deploy, `ANY /` + `ANY /{proxy+}`, throttle 10 rps / burst 20, JSON access logs |
| Scheduler | `tender-radar-daily`, 06:15 America/St_Johns, `{"task":"daily"}`, 0 retries, **DISABLED** until step (h) |
| Logs | `/aws/lambda/tender-radar-{api,worker}`, `/aws/apigateway/tender-radar`, 30-day retention (`local.log_retention`) |
| IAM | api-role, worker-role: own log group + `ssm:GetParameters` (one call per cold start): worker-role on `/tender-radar/prod/*`, api-role only on `DATABASE_URL` and `DJANGO_SECRET_KEY` by exact ARN (no LLM key, direct URL or bootstrap password). api-role also: `lambda:InvokeFunction` on the worker only (`extract_one` after a reviewer upload or delete). scheduler-role: invoke the worker only. `tender-radar-deploy` (GitHub OIDC, environment `prod` only, 1 h): ECR push/pull on this repo, update code of the two functions, invoke the worker (see **Deploys from GitHub**) |
| Alerts | SNS `tender-radar-alerts` → one email; 5 CloudWatch alarms notify on ALARM and OK, one fed by a 5xx metric filter on the API access log (see **Alerts**) |

## AWS profiles: `tender-radar` and the `tender-radar-tf` bridge

`tender-radar` is an `aws login` profile, so its config holds a `login_session` and no
keys. The AWS CLI and `build_push.sh` read it directly. Terraform's AWS provider is built
on the Go SDK, which can't read `aws login` sessions and fails with "no valid credential
sources". So Terraform uses a second profile, `tender-radar-tf`, that has no credentials of
its own. Its `credential_process` asks the CLI to export the current login session's
short-lived credentials, and the Go SDK accepts those. Add this to `~/.aws/config`:

```ini
[profile tender-radar-tf]
credential_process = aws configure export-credentials --profile tender-radar --format process
region = us-east-2
```

Check it with `aws sts get-caller-identity --profile tender-radar-tf`: it should show the
same ARN as `--profile tender-radar`. Nothing is written to disk. The exported credentials
last only as long as the login session. When that session expires, run `aws login --profile
tender-radar` again; the bridge profile needs no changes. Don't put a profile in the
`provider` blocks: CI validates without one.

All commands run from the repo root unless a `cd` says otherwise. Set these once per shell:

```sh
export AWS_PROFILE=tender-radar      # aws CLI and build_push.sh
export AWS_REGION=us-east-2
# Every terraform command (including the ones inside $(...)) goes through the bridge:
terraform() { AWS_PROFILE=tender-radar-tf command terraform "$@"; }
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

First create `infra/prod/alerts.secret.tfvars` (gitignored by `*.secret.tfvars`) in an
editor, with one line: `alarm_email = "<the address for alarm emails>"`. `alarm_email` has
no default, so **every** prod plan, including `-target` and `-destroy` plans, needs
`-var-file=alerts.secret.tfvars`; without it Terraform prompts or fails.

```sh
export TF_VAR_git_sha="$(git rev-parse HEAD)"
cd infra/prod
terraform init -backend-config="bucket=$(terraform -chdir=../bootstrap output -raw state_bucket)"
terraform plan -var-file=alerts.secret.tfvars -out=ecr.tfplan \
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
IAM already covers all four, because both roles may `ssm:GetParameters` on
`/tender-radar/prod/*`. (The API role could read `database_url_direct` too but never asks
for it. Splitting that off would mean listing parameter ARNs per role.)
To rotate a value later, add `--overwrite`; running Lambdas pick it up on their next cold
start (or after a redeploy).

### (d) Build and push the images

```sh
infra/scripts/build_push.sh "$(git rev-parse HEAD)"
```

The images install `requirements.txt` with `-c requirements.lock`, so a rebuild of the same
commit gets the same package versions. To change them, see "Updating dependencies" in the
root README (`infra/scripts/lock_requirements.sh`).

### (e) Full apply

```sh
cd infra/prod
terraform plan -var-file=alerts.secret.tfvars -out=prod.tfplan   # TF_VAR_git_sha from (b), same commit as (d)
terraform apply prod.tfplan
terraform output api_url
cd ../..
```

This is the only time `git_sha` matters: it picks the images the two functions are
**created** with. Both functions have `lifecycle { ignore_changes = [image_uri] }`, so from
here on the deploy workflow owns the images and Terraform never shows or reverts an image
change. Later plans still need some valid `TF_VAR_git_sha` (the variable has no default);
any full sha, e.g. the current `HEAD`, will do. This apply also creates the GitHub OIDC
provider and the deploy role; set up GitHub next (**Deploys from GitHub**, steps 2-3).

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

The first call after a deploy is the slowest cold start there is (5-7 s measured):
Lambda is still pulling the new image's layers into its cache. Later cold starts, in any
sandbox, are ~1-2 s. `/api/health/db` can take ~0.7 s longer if Neon was suspended.

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
terraform plan -var-file=alerts.secret.tfvars -out=prod.tfplan   # expect exactly 1 change: the schedule state
terraform apply prod.tfplan
cd ../..
aws scheduler get-schedule --no-cli-pager --name tender-radar-daily --query State
```

## Admin user (one-time bootstrap)

The admin password never passes through Claude Code, a shell argument, the logs or the
chat. You put it into a one-time SecureString; the worker's `create_admin` task reads it,
creates or updates the superuser (Django's password validators apply: 12+ characters, not
common, not all digits, not like the username), and logs only `admin created: <username>`
or `admin updated: <username>`. Then you delete the parameter. The same steps reset a
forgotten password; they also clear that username's failed-login lockout.

1. **You:** copy a new password (from your password manager) to the clipboard, then:

   ```sh
   aws ssm put-parameter --no-cli-pager --type SecureString \
     --name /tender-radar/prod/admin_bootstrap_password --value "$(pbpaste)"
   pbcopy < /dev/null
   ```

   (Retrying after a refused password: add `--overwrite`.)

2. **Claude Code (or you):** invoke the task, with your admin username in the payload:

   ```sh
   AWS_MAX_ATTEMPTS=1 aws lambda invoke --no-cli-pager \
     --function-name tender-radar-worker \
     --cli-binary-format raw-in-base64-out \
     --payload '{"task":"create_admin","username":"<username>"}' \
     --cli-read-timeout 910 /tmp/tr-admin.json
   cat /tmp/tr-admin.json; echo
   aws logs tail /aws/lambda/tender-radar-worker --since 5m | grep -E "admin (created|updated)|create_admin"
   ```

   Success: `"ok": true` and one `admin created: <username>` line. A refused password fails
   the invocation with the validator's reason, e.g. `BootstrapError: the bootstrap password
   was refused by Django's password validators: This password is too short...`; nothing is
   written then. A missing parameter fails with `SSM parameter
   /tender-radar/prod/admin_bootstrap_password: ParameterNotFound`.

3. **You:** log in at `${API%/}/admin/` (the URL from `terraform output api_url`).

4. **You:** delete the parameter straight away (the worker role can only read parameters,
   so the task can't delete it itself):

   ```sh
   aws ssm delete-parameter --no-cli-pager --name /tender-radar/prod/admin_bootstrap_password
   ```

Lockout: 5 failed logins for one username from one address lock that pair for an hour,
then it unlocks by itself. While logged in, see **Axes → Access attempts** in the admin; to
unlock early, delete the attempt there or run this bootstrap again for that username.

## Deploys from GitHub

`.github/workflows/deploy.yml` deploys every push to `main`. No AWS keys are stored in
GitHub: the deploy job gets a GitHub OIDC token and exchanges it for the
`tender-radar-deploy` role (`infra/prod/deploy.tf`). That role's trust policy accepts
exactly one subject, `repo:znahamama/tender-radar:environment:prod`, so only a job running
in the GitHub environment `prod` of this repo can assume it, for at most 1 h. What it may
do: push/pull images in the `tender-radar` ECR repo, `GetFunction`,
`GetFunctionConfiguration` and `UpdateFunctionCode` on the two functions, and invoke the
worker. Nothing else: no IAM, no function configuration, no environment variables, no SSM.

What one run does:

1. **resolve**: the sha to deploy: the pushed commit, or the `sha` input of a manual run
   (it must be a commit on `main`).
2. **ci**: the existing `ci.yml` jobs (gitleaks, tests, terraform fmt/validate) on that sha.
   `ci.yml` no longer runs on pushes to `main` by itself, since this is the same check.
3. **deploy** (environment `prod`, so it waits for your approval; runs on `ubuntu-24.04-arm`):
   - builds `api-<sha>` and `worker-<sha>` natively on arm64 with the same Dockerfile,
     targets, flags and `requirements.lock` as `build_push.sh`. A tag that already exists
     is not rebuilt (tags are immutable; this is what makes a rollback fast);
   - points the **worker** at `worker-<sha>` and waits until the update is done;
   - invokes `{"task":"migrate"}` synchronously; fails on a `FunctionError`, on
     `"ok": false`, or when the summary's `version` isn't the sha (an old image ran it);
   - points the **API** at `api-<sha>` and waits;
   - polls `/api/health` until it reports the sha (up to 2 minutes), else fails;
   - writes a summary (sha, images built or reused, migrate result, health) to the run page.

   A failed step stops the run: nothing after it happens. A failure after the worker
   update leaves the worker on the new code and the API on the old one, which is safe
   because migrations are backward compatible (below); fix forward or roll back.

Only one deploy runs at a time (concurrency group `deploy-prod`, never cancelled
mid-run). If several runs pile up, GitHub keeps only the newest waiting one and cancels
the older waiting ones; that's fine, the newest `main` contains them.

The full migrate error stays in CloudWatch (`aws logs tail /aws/lambda/tender-radar-worker
--since 30m`). The workflow prints only the error type: this repo is public, and so are its
Actions logs.

### One-time setup

1. **Terraform** (you): plan and apply. On an existing deployment expect 3 to add
   (`aws_iam_openid_connect_provider.github`, `aws_iam_role.deploy`,
   `aws_iam_role_policy.deploy`), nothing to change or destroy, and the new output
   `deploy_role_arn`.

   ```sh
   export TF_VAR_git_sha="$(git rev-parse HEAD)"   # any valid sha; ignored for existing functions
   cd infra/prod
   terraform plan -var-file=alerts.secret.tfvars -out=prod.tfplan
   terraform apply prod.tfplan
   terraform output -raw deploy_role_arn; echo
   terraform output -raw api_url; echo
   cd ../..
   ```

   An account has at most one OIDC provider per URL. If the apply fails with
   `EntityAlreadyExists` for `token.actions.githubusercontent.com` (another project created
   it), import it instead, then plan again:
   `terraform import aws_iam_openid_connect_provider.github
   arn:aws:iam::<account id>:oidc-provider/token.actions.githubusercontent.com`.
   Its audience list must include `sts.amazonaws.com`. A shared provider would also be
   deleted by **Tear it all down**; `terraform state rm` it before that.

2. **GitHub environment** (you): repo **Settings → Environments → New environment** `prod`:
   - **Required reviewers**: yourself. Leave "Prevent self-review" off (you're the only
     reviewer, so it would block every deploy).
   - **Deployment branches and tags**: "Selected branches and tags", add the branch rule
     `main`. The role trusts any job in `prod`, so this is what keeps other branches out.
   - **Environment variables** (variables, not secrets; neither value is secret):
     `AWS_DEPLOY_ROLE_ARN` = the `deploy_role_arn` output, `API_URL` = the `api_url` output.

3. **Optional hardening** (Settings → Actions → General): "Require actions to be pinned to a
   full-length commit SHA" (the workflows already are; a test checks it too).

4. **First run**: push to `main`, or **Actions → deploy → Run workflow** on `main` with
   an empty sha.

### Approving a deploy

After CI passes, the run shows **Waiting for review** on the `deploy` job (and GitHub
emails the reviewers). Open the run, **Review deployments**, tick `prod`, **Approve and
deploy**. **Reject** deploys nothing. An unanswered request expires after 30 days. Follow
along with `gh run watch`, or read the summary on the run page when it's done.

### Rollback

Re-deploy an older commit of `main`:

```sh
gh workflow run deploy.yml --ref main -f sha=<full 40-char sha>
```

(or **Actions → deploy → Run workflow**, branch `main`, field `sha`). The run is the same
as a normal deploy, including CI on that sha and your approval. ECR keeps the last 5
`api-*` and `worker-*` images, so a recent sha reuses its images and goes straight to the
worker update; an older one is rebuilt from that commit and its own `requirements.lock`.
Its migrate step finds nothing to apply: Django doesn't unapply migrations, so the
database stays on the newest schema and the old code runs against it.

The next push to `main` deploys `main`'s tip again. To stay rolled back, revert the bad
commit on `main` (`git revert`), which is also a normal forward deploy and usually the
better fix.

### Migrations must be backward compatible

Migrations run before the API switches, so for a minute or two the **old** API serves on
the **new** schema, and a rollback runs old code on it indefinitely. So every migration
has to work with the release before it (expand, then contract):

- Add, don't change: new tables, and new columns that are nullable or have a database
  default (`db_default=`; a plain `default=` is Python-side only, and the old code's
  INSERTs don't know the column).
- Never drop or rename a column or table the previous release still reads. Stop using it
  in one release; remove it (`RemoveField`, `DeleteModel`) in a later one.
- A rename is add new + backfill + switch reads + (later) drop old.
- Data migrations must be safe to run twice, and fast: the worker's limit is 900 s, and a
  long lock blocks the live API.

### Emergency deploy (GitHub Actions unavailable)

`infra/scripts/build_push.sh` is kept for this (and for the first deploy). It uses your
own `tender-radar` profile, not the deploy role. Same order as the workflow:

```sh
git status                                   # clean, on main, at the commit to deploy
sha="$(git rev-parse HEAD)"
infra/scripts/build_push.sh "$sha"
repo="$(terraform -chdir=infra/prod output -raw ecr_repository_url)"

aws lambda update-function-code --no-cli-pager --function-name tender-radar-worker \
  --image-uri "$repo:worker-$sha" --query LastUpdateStatus
aws lambda wait function-updated-v2 --function-name tender-radar-worker
# migrate: step (f). Continue only on "ok": true and no "FunctionError".
aws lambda update-function-code --no-cli-pager --function-name tender-radar-api \
  --image-uri "$repo:api-$sha" --query LastUpdateStatus
aws lambda wait function-updated-v2 --function-name tender-radar-api
API="$(terraform -chdir=infra/prod output -raw api_url)"; curl -sS "${API%/}/api/health"; echo
```

`terraform apply -var git_sha=...` does **not** change the images any more (they're
ignored after creation).

### Infrastructure changes

Terraform still owns everything else: plan and apply as before. Any valid
`TF_VAR_git_sha` works; image changes never appear in a plan.

```sh
export TF_VAR_git_sha="$(git rev-parse HEAD)"
cd infra/prod && terraform plan -var-file=alerts.secret.tfvars -out=prod.tfplan && terraform apply prod.tfplan && cd ../..
```

## Worker tasks by hand

### Re-assess only (after a rules or profile change)

`{"task":"assess"}` runs only `assess_tenders`: it recomputes eligibility from the stored
extractions with the deployed rules and the default supplier profile. No ingest, no LLM
calls (no Anthropic spend), no document fetch, so it is safe to run at any time, any number
of times. Use it after a deploy that changes the eligibility or scope rules, so the site
shows the new verdicts without waiting for the next 06:15 run. (Saving the profile in the
admin already re-assesses for that profile; this task is for code changes.)

```sh
AWS_MAX_ATTEMPTS=1 aws lambda invoke --no-cli-pager \
  --function-name tender-radar-worker \
  --cli-binary-format raw-in-base64-out --payload '{"task":"assess"}' \
  --cli-read-timeout 910 /tmp/tr-assess.json
cat /tmp/tr-assess.json; echo   # "ok": true, steps: [assess_tenders]
aws logs tail /aws/lambda/tender-radar-worker --since 10m | grep -E 'assessed [0-9]|"task":"assess"'
```

The log shows one line per extractor model (`<model>: eligible N, ...`) and `assessed N`.

### Re-read one notice (`extract_one`)

`{"task":"extract_one","reference":"<ref>"}` runs `extract_tenders --reference <ref>`: the
LLM extraction of that one notice (prompt v5 when it has stored documents, else v3; a cache
hit costs nothing) and its assessment, under the usual per-run and daily caps. The API
sends it asynchronously (0 retries, `aws_lambda_function_event_invoke_config.worker`) after
a reviewer uploads or deletes a document on the admin review page; an unknown reference is
rejected before anything runs. By hand:

```sh
AWS_MAX_ATTEMPTS=1 aws lambda invoke --no-cli-pager \
  --function-name tender-radar-worker \
  --cli-binary-format raw-in-base64-out --payload '{"task":"extract_one","reference":"<ref>"}' \
  --cli-read-timeout 910 /tmp/tr-one.json
cat /tmp/tr-one.json; echo   # "ok": true, "reference": "<ref>", steps: [extract_one]
```

## Alerts

One SNS topic, `tender-radar-alerts`, with one email subscription, and five alarms
(`infra/prod/alerts.tf`). Each emails on ALARM and again on OK.

| Alarm | Fires when | Missing data |
|---|---|---|
| `tender-radar-worker-errors` | worker `Errors` ≥ 1 in an hour: a step raised (incl. a bad Anthropic key/model), or the 900 s timeout | OK |
| `tender-radar-worker-not-run` | worker `Invocations` < 1 in the last 26 h (sliding; 26 not 24 so metric lag and the 25 h autumn DST gap don't false-alarm) | ALARM |
| `tender-radar-api-errors` | API `Errors` ≥ 5 in 5 minutes | OK |
| `tender-radar-api-5xx` | 5xx responses ≥ 3 in 5 minutes, from the metric filter `tender-radar-api-5xx` on the API access log (custom metric `TenderRadar/Api5xx`). Catches Django 500s, which `api-errors` can't see; the traceback is in `/aws/lambda/tender-radar-api` | OK |
| `tender-radar-scheduler-target-errors` | Scheduler `TargetErrorCount` ≥ 1 in an hour, schedule group `default` | OK |

A budget cap is not an error: `extract_tenders` stops cleanly, the run is `"ok": true`
(`extract_one` adds `"capped": true`, and the review page says the upload waits for the
next run).

`worker-not-run` is slow by design: a window over one day is checked hourly, on data up to
the top of the hour, so a missed 06:15 run alarms around 09:00 St. John's time that morning
(the day after the last good run). A new alarm shows INSUFFICIENT_DATA until its first
hourly check.

Add or change them:

```sh
cd infra/prod
terraform plan -var-file=alerts.secret.tfvars -out=alerts.tfplan
terraform apply alerts.tfplan
cd ../..
```

After the first apply, **confirm the subscription** from the email AWS sends ("AWS
Notification - Subscription Confirmation"). Until then nothing is delivered, including the
OK notification each alarm sends when it first leaves INSUFFICIENT_DATA. Check:

```sh
aws sns list-subscriptions-by-topic --no-cli-pager \
  --topic-arn "$(terraform -chdir=infra/prod output -raw alerts_topic_arn)" \
  --query "Subscriptions[].SubscriptionArn"         # an ARN, not PendingConfirmation
aws cloudwatch describe-alarms --no-cli-pager --alarm-name-prefix tender-radar- \
  --query "MetricAlarms[].[AlarmName,StateValue]" --output table
```

Test delivery end to end without breaking anything (it goes back to its real state at the
next evaluation, within a minute or so, and sends an OK):

```sh
aws cloudwatch set-alarm-state --no-cli-pager --alarm-name tender-radar-worker-errors \
  --state-value ALARM --state-reason "manual test"
```

To change the address, edit `alerts.secret.tfvars`, then plan and apply (the old
subscription is replaced; confirm the new one).

## Monthly cost estimate: about $0–1

| Service | Usage | Cost |
|---|---|---|
| Lambda | 1 worker run/day (a few minutes at 1 GB) + light API traffic, well inside the always-free 1M requests / 400k GB-s | $0 |
| API Gateway HTTP API | under 1M requests: free for the first 12 months, then $1.00 per million | $0–0.01 |
| ECR | api and worker share base and dependency layers; roughly 0.3–0.7 GB with 5 deploys kept. 500 MB free for 12 months, then $0.10/GB-month | $0–0.10 |
| CloudWatch Logs | MBs per month, 30-day retention (well under 1 GB stored); 5 GB ingestion free | $0 |
| SSM Parameter Store | 4 standard SecureStrings with `aws/ssm`: free. KMS decrypts at cold start, inside the free 20k requests/month | $0 |
| EventBridge Scheduler | 30 invocations/month, 14M free | $0 |
| CloudWatch alarms | 5 standard-resolution metric alarms: 10 free (always-free tier), then $0.10/alarm-month | $0 (list price $0.50) |
| CloudWatch metric filter | 1 filter (free) publishing 1 custom metric, `TenderRadar/Api5xx`, only when a 5xx happens: 10 custom metrics free, then $0.30/metric-month | $0 |
| SNS | 1 topic; email deliveries: 1,000/month free, a few per month used | $0 |
| S3 state bucket | KBs, a few hundred requests | <$0.01 |
| ECR basic scan on push | free | $0 |
| GitHub Actions | public repo: standard runners, including `ubuntu-24.04-arm`, are free | $0 |
| Data transfer out | CSV downloads come *in* (free); calls to Neon/Anthropic are tiny | ~$0 |

Not AWS, but these are the real spend: Anthropic usage is capped per day by
`LLM_DAILY_BUDGET_USD`, set to $0.15 on the worker (a worst case of ~$4.50/month; the
local default stays $0.50), and Neon is on
its free tier. Optional: an AWS Budget with a $1 alert (the first two budgets are free).

## Tear it all down

```sh
# 1. Prod: everything, including ECR images (force_delete) and log groups
cd infra/prod
terraform plan -destroy -var-file=alerts.secret.tfvars -out=destroy.tfplan
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
