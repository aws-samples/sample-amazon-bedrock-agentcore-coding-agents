#!/usr/bin/env bash
# Package and deploy the coordinator to AgentCore Runtime, in one command.
#
# WHY THIS EXISTS. The five steps below are the same five every time: stage the engine
# into the build context, create the AgentCore CLI project, add the agent, write this
# account's values into the generated project, and deploy. None of them is a decision,
# and typing them one at a time in a room buys nothing but chances to paste half a
# line. What IS worth understanding is what the coordinator is wired to (three role
# ARNs, the Gateway, the repository, the merge policy), and this script prints exactly
# that before it deploys.
#
# It is idempotent: an existing project is reused rather than recreated, and a re-run
# after a failed deploy picks up where it stopped. Every prerequisite is checked BEFORE
# the slow container build, because finding out after six minutes that GITHUB_REPO was
# unset is the failure this script exists to prevent.
#
# Usage (from anywhere):
#   export GITHUB_GATEWAY_URL=... GITHUB_REPO=owner/repo [WORKSHOP_MERGE_POLICY=...]
#   ./deploy-coordinator.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
PROJECT_DIR="$REPO_ROOT/CodingAgents"
PROJECT_JSON="$PROJECT_DIR/agentcore/agentcore.json"
: "${WORKSHOP_MERGE_POLICY:=human_review}"

die() { echo "ERROR: $*" >&2; exit 1; }

# ── 1. Refuse to start without what the deploy needs ─────────────────────────
[ -n "${GITHUB_GATEWAY_URL:-}" ] || die "GITHUB_GATEWAY_URL is not set. Deploy the GitHub MCP Gateway first (Connect GitHub, step 3), then export it (step 4)."
[ -n "${GITHUB_REPO:-}" ] || die "GITHUB_REPO is not set. Export it as owner/repository (Connect GitHub, step 1)."
case "$GITHUB_REPO" in */*) ;; *) die "GITHUB_REPO must look like owner/repository, not '$GITHUB_REPO'." ;; esac
AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null || true)}}"
[ -n "$AWS_REGION" ] || die "No AWS region. Export AWS_REGION."
command -v agentcore >/dev/null || die "The agentcore CLI is missing. Install it with: sudo npm install -g @aws/agentcore@latest"

echo "==> The coordinator will be wired to:"
for role in claude-code opencode kiro; do
  cfg="$REPO_ROOT/coding-agents/$role/runtime_config.json"
  arn=$(jq -r '.runtime_arn // empty' "$cfg" 2>/dev/null || true)
  [ -n "$arn" ] || die "No Runtime ARN for $role ($cfg). Finish Lab 1 before deploying the coordinator: the coordinator must not be created before its workers exist."
  printf '      %-12s %s\n' "$role" "$arn"
done
printf '      %-12s %s\n' "gateway" "$GITHUB_GATEWAY_URL"
printf '      %-12s %s\n' "repository" "$GITHUB_REPO"
printf '      %-12s %s\n' "merge" "$WORKSHOP_MERGE_POLICY"
echo

# ── 2. Stage the engine into the container build context ─────────────────────
echo "==> Staging the engine (orchestrator/ -> the coordinator image)"
( cd "$HERE" && python3 stage_engine.py )

# ── 3. Create the AgentCore CLI project, once ────────────────────────────────
# --skip-git because this lives inside the workshop clone; --no-agent because the
# agent is added on the next line with the byo/container shape this engine needs.
if [ -f "$PROJECT_JSON" ]; then
  echo "==> Reusing the existing AgentCore project ($PROJECT_JSON)"
else
  echo "==> Creating the AgentCore project"
  ( cd "$REPO_ROOT" && agentcore create --name CodingAgents --no-agent --skip-git )
  # --region on a byo agent is the CLI's import path only and sets no deploy target;
  # configure_deploy.py below is what pins account + region, because agentcore deploy
  # otherwise invents a us-east-1 target where this workshop's IAM does not exist.
  ( cd "$PROJECT_DIR" && agentcore add agent --name orchestrator --type byo \
      --build Container --language Python --framework Strands --model-provider Bedrock \
      --code-location ../orchestrator-agent --entrypoint main.py \
      --protocol HTTP --region "$AWS_REGION" --json )
fi

# ── 4. Write this account's values into the generated project ────────────────
echo "==> Injecting the role ARNs, GitHub configuration, and deploy target"
( cd "$HERE" && python3 configure_deploy.py --project "$PROJECT_JSON" )
( cd "$PROJECT_DIR" && agentcore validate )

# ── 5. Deploy, then prove the deployed thing answers ────────────────────────
echo "==> Deploying (container build + push + CreateAgentRuntime; 3 to 6 minutes)"
( cd "$PROJECT_DIR" && agentcore deploy --yes --json )
( cd "$PROJECT_DIR" && agentcore status --runtime orchestrator --json )

echo
echo "==> Read-only probe: asking the DEPLOYED coordinator which roles a preset routes to"
( cd "$PROJECT_DIR" && agentcore invoke --stream \
    "Call list_presets and tell me which roles the add-a-feature preset routes to. Do not dispatch anything." )

echo
# The preset here MUST match the one the Run-a-Build page tells the room to submit.
# It used to say project-from-scratch, which is the take-home version: measured at 82
# minutes because each pull request is gated in turn, so an attendee who followed the
# terminal instead of the page started a build the session cannot wait for. The room's
# build is the one-service browser game the whole room plays at the end of Lab 2.
echo "Coordinator deployed. Submit your build from $PROJECT_DIR with:"
echo "    agentcore invoke --session-id \"\$(python3 -c 'import uuid;print(uuid.uuid4())')\" --stream \"preset=game-from-scratch\""
