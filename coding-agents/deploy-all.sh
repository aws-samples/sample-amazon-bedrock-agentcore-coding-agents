#!/usr/bin/env bash
# Deploy all coding agents in sequence.
# Prerequisites: infra/setup.sh already ran (infra.config exists).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Read the served roster, including WORKSHOP_ROLES restores, from its source of
# truth. Do not silently omit a declared role because its build directory is gone.
role_names=$(PYTHONPATH="$SCRIPT_DIR/../orchestrator${PYTHONPATH:+:$PYTHONPATH}" \
  python3 -c 'from roles import roster_ids; print(" ".join(roster_ids()))')
read -r -a AGENTS <<< "$role_names"

echo "=============================================="
echo "  Deploying all coding agents"
echo "=============================================="

for agent in "${AGENTS[@]}"; do
  AGENT_DIR="${SCRIPT_DIR}/${agent}"
  if [ ! -d "$AGENT_DIR" ]; then
    echo "ERROR: configured role ${agent}/ not found" >&2
    exit 1
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
  (cd "$AGENT_DIR" && python3 deploy.py)
done

echo ""
echo "=============================================="
echo "  All agents deployed."
echo "  Test: python claude-code/connect.py"
echo "=============================================="
