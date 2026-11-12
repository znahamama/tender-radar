# Non-secret, committed. git_sha is passed per deploy: export TF_VAR_git_sha=$(git rev-parse HEAD)

# false until one manual {"task":"daily"} run succeeds (runbook step h). Every apply uses
# this value, so flip it here, not with -var, or the next deploy turns the schedule off.
schedule_enabled = false
