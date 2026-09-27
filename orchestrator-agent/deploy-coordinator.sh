#!/usr/bin/env bash
# Package and deploy the coordinator to AgentCore Runtime, in one command.
#
# WHY THIS EXISTS. The five steps below are the same five every time: stage the engine
# into the build context, create the AgentCore CLI project, add the agent, write this
# account's values into the generated project, and deploy. None of them is a decision,
# and typing them one at a time in a room buys nothing but chances to paste half a
# line. What IS worth understanding is what the coordinator is wired to (selected role
# ARNs, the Gateway, the repository, the merge policy), and this script prints exactly
# that before it deploys.
#
# It is idempotent: an existing project is reused rather than recreated, and a re-run
# after a failed deploy picks up where it stopped. Every prerequisite is checked BEFORE
# the slow container build, because finding out after six minutes that GITHUB_REPO was
# unset is the failure this script exists to prevent.
#
# Usage (from anywhere):
#   # Saved GitHub settings work in a new terminal; explicit exports take priority.
#   export AWS_REGION=... [GITHUB_GATEWAY_URL=... GITHUB_REPO=owner/repo]
#   ./deploy-coordinator.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
PROJECT_DIR="$REPO_ROOT/CodingAgents"
PROJECT_JSON="$PROJECT_DIR/agentcore/agentcore.json"
: "${WORKSHOP_MERGE_POLICY:=human_review}"

die() { echo "ERROR: $*" >&2; exit 1; }

# ── 1. Refuse to start without what the deploy needs ─────────────────────────
# Match github.py doctor: env, saved Settings, then the Gateway deployment state.
# Fill missing exports only. Resolve as data, never evaluate saved shell text.
case "${GITHUB_GATEWAY_URL:-}" in https://*) ;; *) GITHUB_GATEWAY_URL="" ;; esac   # e.g. a saved "null"
if [ -z "${GITHUB_GATEWAY_URL:-}" ] || [ -z "${GITHUB_REPO:-}" ]; then
  # Resolve the two values independently: _gateway_config() returns nothing when the
  # repository is malformed, which then blamed the Gateway for a bad GITHUB_REPO.
  gateway_config=$(PYTHONPATH="$REPO_ROOT/orchestrator${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -c 'import json, os; import github as g
f = g._load_config_file()
url = next((str(v).strip() for v in (os.environ.get("GITHUB_GATEWAY_URL"), f.get("gateway_url"))
            if v and str(v).strip().startswith("https://")), "") or (g._discover_gateway_url() or "")
print(json.dumps({"gateway_url": url, "repo": os.environ.get("GITHUB_REPO") or f.get("repo") or ""}))')
  GITHUB_GATEWAY_URL="${GITHUB_GATEWAY_URL:-$(printf '%s' "$gateway_config" | jq -r '.gateway_url // empty')}"
  GITHUB_REPO="${GITHUB_REPO:-$(printf '%s' "$gateway_config" | jq -r '.repo // empty')}"
fi
[ -n "${GITHUB_GATEWAY_URL:-}" ] || die "GITHUB_GATEWAY_URL is not configured. Complete Connect GitHub and run python3 orchestrator/github.py doctor from the repository root."
[ -n "${GITHUB_REPO:-}" ] || die "GITHUB_REPO is not configured. Save owner/repository in GitHub Settings or export GITHUB_REPO."
# Accept what attendees paste (a URL, an ssh remote, a trailing slash, .git) with the
# SAME normalization doctor uses; a second shell copy accepted different shapes.
repo_check=$(PYTHONPATH="$REPO_ROOT/orchestrator${PYTHONPATH:+:$PYTHONPATH}" GITHUB_REPO_RAW="$GITHUB_REPO" \
  python3 -c 'import os; from github import normalize_repo, repo_problem, _REPO_RE
repo = normalize_repo(os.environ["GITHUB_REPO_RAW"])
print(("OK " + repo) if _REPO_RE.match(repo) else ("BAD GITHUB_REPO " + repo_problem(repo)))')
case "$repo_check" in
  "OK "*) GITHUB_REPO="${repo_check#OK }" ;;
  *) die "${repo_check#BAD }" ;;
esac
export GITHUB_GATEWAY_URL GITHUB_REPO
AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null || true)}}"
[ -n "$AWS_REGION" ] || die "No AWS region. Export AWS_REGION."
# Export it: configure_deploy.py and the CDK deploy read it, and a non-interactive
# shell has no .bashrc exports (it fell back to a literal us-west-2 in us-east-1).
export AWS_REGION
command -v agentcore >/dev/null || die "The agentcore CLI is missing. Install the workshop's pinned @aws/agentcore version."
WORKSHOP_RUNTIME_PLATFORM_VERSION=$(python3 "$REPO_ROOT/coding-agents/runtime_deploy.py" --print-platform)
export WORKSHOP_RUNTIME_PLATFORM_VERSION

echo "==> The coordinator will be wired to:"
role_names=$(PYTHONPATH="$REPO_ROOT/orchestrator${PYTHONPATH:+:$PYTHONPATH}" \
  python3 -c 'from roles import roster_ids; print(" ".join(roster_ids()))')
for role in $role_names; do
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
echo "==> Deploying (container build + push + CreateAgentRuntime; usually 5 to 10 minutes, up to 15)"
( cd "$PROJECT_DIR" && agentcore deploy --yes --json )
echo "==> Verifying platform $WORKSHOP_RUNTIME_PLATFORM_VERSION READY (preparation, if needed, can take several minutes)"
python3 "$HERE/promote_runtime.py" --project "$PROJECT_DIR"
( cd "$PROJECT_DIR" && agentcore status --runtime orchestrator --json )

echo
echo "==> Read-only probe: asking the DEPLOYED coordinator which roles a preset routes to"
python3 "$HERE/probe_coordinator.py" --project "$PROJECT_DIR"

echo
# What was created and how to see it without this script (local state only, no AWS call).
python3 "$HERE/promote_runtime.py" --project "$PROJECT_DIR" --receipt || true

echo
# The preset here MUST match the one the Run-a-Build page tells the room to submit.
# It used to say project-from-scratch, which is the take-home version: measured at 82
# minutes because each pull request is gated in turn, so an attendee who followed the
# terminal instead of the page started a build the session cannot wait for. The room's
# build is the one-service browser game the whole room plays at the end of Lab 2.
# Point at the page's own block instead of printing a second, different command: the
# page's version prints the session ID the attendee needs later (clarifying answers,
# status, and Clean Up), and a terminal command here invited a second submission.
echo "Coordinator deployed. Next: submit your build with step 1 of Run a Build and Follow It."
echo "  It creates and prints the session ID you will reuse; submit only once."
