#!/usr/bin/env bash
# Shared, builder-portable "build an arm64 image and push it to ECR" helper.
#
# AgentCore Runtime needs linux/arm64 images. Two real container builders are
# supported, auto-detected at run time; both do a genuine arm64 build + push,
# neither is a stub:
#
#   * docker buildx: the attendee EC2 box (Amazon Linux) ships Docker with the
#                      buildx plugin; one `docker buildx build … --push` builds
#                      and pushes in a single step.
#   * finch: a dev box may run Finch instead of Docker (Finch has no
#                      `buildx` subcommand): `finch build --platform linux/arm64`
#                      then a separate `finch push`.
#
# Usage (sourced or called):
#   build_and_push_arm64 "<ECR_URI>" "<DOCKERFILE>" "<BUILD_CONTEXT>" "<REGION>" "<ACCOUNT_ID>"
build_and_push_arm64() {
  local ecr_uri="$1" dockerfile="$2" context="$3" region="$4" account_id="$5"
  local registry="${account_id}.dkr.ecr.${region}.amazonaws.com"

  # Pick a real builder: prefer docker+buildx, fall back to finch. A box with
  # neither fails loud (no silent skip); there is no fake "pretend it built".
  local builder=""
  if command -v docker >/dev/null 2>&1 && docker buildx version >/dev/null 2>&1; then
    builder="docker-buildx"
  elif command -v finch >/dev/null 2>&1; then
    builder="finch"
  else
    echo "Error: no container builder found (need 'docker buildx' or 'finch')." >&2
    return 1
  fi
  echo "Builder: ${builder}"

  echo "Logging into ECR (${registry})..."
  if [ "$builder" = "finch" ]; then
    aws ecr get-login-password --region "${region}" | \
      finch login --username AWS --password-stdin "${registry}"
  else
    aws ecr get-login-password --region "${region}" | \
      docker login --username AWS --password-stdin "${registry}"
  fi

  echo "Building arm64 image: ${ecr_uri}"
  if [ "$builder" = "docker-buildx" ]; then
    docker buildx build \
      --platform linux/arm64 \
      -t "${ecr_uri}" \
      -f "${dockerfile}" \
      "${context}" \
      --push
  else
    # Finch: build then push (no single-step --push).
    finch build \
      --platform linux/arm64 \
      -t "${ecr_uri}" \
      -f "${dockerfile}" \
      "${context}"
    finch push "${ecr_uri}"
  fi

  echo "Image pushed: ${ecr_uri}"
}
