#!/usr/bin/env bash
# Build the api and worker images for linux/arm64 and push them to ECR as
# api-<sha> and worker-<sha>.
#
#   infra/scripts/build_push.sh "$(git rev-parse HEAD)"
#
# Refuses a dirty working tree, and a sha that isn't HEAD, so every tag is exactly the
# commit it names. ECR tags are IMMUTABLE: an existing tag is skipped, never overwritten.
set -euo pipefail

PROFILE="${AWS_PROFILE:-tender-radar}"
REGION="us-east-2"
REPO_NAME="tender-radar"

sha="${1:-}"
if [[ ! "$sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo "usage: $0 <full 40-char git sha>   e.g. $0 \"\$(git rev-parse HEAD)\"" >&2
  exit 2
fi

root="$(git rev-parse --show-toplevel)"
cd "$root"

if [[ -n "$(git status --porcelain)" ]]; then
  echo "refusing: working tree is dirty (commit or stash first):" >&2
  git status --short >&2
  exit 1
fi
head="$(git rev-parse HEAD)"
if [[ "$sha" != "$head" ]]; then
  echo "refusing: $sha is not HEAD ($head); check out that commit first" >&2
  exit 1
fi

account="$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)"
registry="${account}.dkr.ecr.${REGION}.amazonaws.com"
repo="${registry}/${REPO_NAME}"

# The password goes straight from the AWS CLI into docker's stdin: never echoed or stored
# in shell history.
aws ecr get-login-password --profile "$PROFILE" --region "$REGION" \
  | docker login --username AWS --password-stdin "$registry"

for target in api worker; do
  tag="${target}-${sha}"
  if aws ecr describe-images --profile "$PROFILE" --region "$REGION" \
       --repository-name "$REPO_NAME" --image-ids "imageTag=${tag}" >/dev/null 2>&1; then
    echo "skip: ${repo}:${tag} already exists (tags are immutable)"
    continue
  fi
  echo "build+push: ${repo}:${tag}"
  # --provenance=false / --sbom=false: no attestation manifests. With them, buildx pushes
  # an image index, and Lambda rejects it.
  docker buildx build \
    --platform linux/arm64 \
    --provenance=false \
    --sbom=false \
    --target "$target" \
    --build-arg "GIT_SHA=${sha}" \
    --tag "${repo}:${tag}" \
    --push \
    .
done

echo "done. Next: cd infra/prod && terraform apply -var git_sha=${sha}"
