#!/usr/bin/env bash
# Deploy all coding agents in sequence.
# Prerequisites: infra/setup.sh already ran (infra.config exists).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# The SERVED roster (orchestrator/roles.py): backend, frontend, validator. Kept-but-
# hidden restore paths (claude-code-validator, codex) are NOT deployed by default;
# deploy one explicitly with `./deploy-prebuilt.sh <role>` when you restore it.
# This list previously named `cursor`, `hermes`, and `open-code`, none of which are
# directories in this repo, and OMITTED the real `opencode`: the loop skips missing
# dirs, so it silently deployed two of the three served roles.
AGENTS=(claude-code opencode kiro)

echo "=============================================="
echo "  Deploying all coding agents"
echo "=============================================="

for agent in "${AGENTS[@]}"; do
  AGENT_DIR="${SCRIPT_DIR}/${agent}"
  if [ ! -d "$AGENT_DIR" ]; then
    echo "  SKIP: ${agent}/ not found"
    continue
  fi

  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  ${agent}: setup.sh"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  # Kiro is the one role with a credential step, and at image-build time the
  # attendee may not have minted their ksk_ key yet (the event provisions the
  # subscription; the key is per-attendee and comes after). Without a key its
  # setup.sh prompts on a TTY and FAILS LOUD without one, which would abort this
  # whole batch. So build it keyless and let the attendee add the key later on the
  # wired instance in console Settings; run.sh reads it from the Token Vault at
  # session start. Pass KIRO_API_KEY to provision the vault here instead.
  if [ "$agent" = "kiro" ] && [ -z "${KIRO_API_KEY:-}" ]; then
    echo "  No KIRO_API_KEY set; building kiro WITHOUT its Token Vault identity"
    echo "  (--skip-identity). Add your ksk_ key on the wired Kiro instance in"
    echo "  console Settings after it deploys."
    (cd "$AGENT_DIR" && ./setup.sh --skip-identity)
  else
    (cd "$AGENT_DIR" && ./setup.sh)
  fi

  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  ${agent}: deploy.py"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  (cd "$AGENT_DIR" && python deploy.py)
done

echo ""
echo "=============================================="
echo "  All agents deployed."
echo "  Test: python claude-code/connect.py"
echo "=============================================="
