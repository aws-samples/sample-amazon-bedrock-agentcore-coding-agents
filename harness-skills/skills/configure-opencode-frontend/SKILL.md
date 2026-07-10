---
name: configure-opencode-frontend
description: >-
  Configure opencode as the frontend builder in the three-agent AgentCore harness.
  Use for deploying the opencode Runtime, staging AGENTS.md and
  .config/opencode/opencode.json, or verifying that the generated chatbot delegates
  pricing to the MCP server. opencode runs Bedrock-native (amazon-bedrock provider,
  claude-sonnet-4-6) with no API key.
---

# Configure the opencode frontend builder

opencode builds the thin chatbot UI. Claude Code builds the MCP backend and a
second Claude Code (the validator) validates the composed result. There is no race
and no winner.

## Prerequisites

- `coding-agents/infra.config` exists.
- The `amazon-bedrock` provider can reach `claude-sonnet-4-6` in `us-west-2`.
- The Runtime execution role can use the AWS SDK credential chain.
- Docker Buildx or Finch can build arm64 images.

opencode does not need an API key, AgentCore workload identity, or API-key
credential provider. The Runtime IAM role is the authentication path (the CLI
signs its Bedrock calls with the role's temporary credentials).

## Deploy

```bash
cd coding-agents/opencode
./setup.sh
python deploy.py
```

`runtime_config.json` must contain a Runtime ARN and the Runtime must reach
`READY` before continuing.

## Stage project guidance

Copy the whole project configuration so hidden settings are preserved:

```bash
cp -R orchestrator/harness/opencode/. /mnt/s3files/
test -s /mnt/s3files/AGENTS.md
test -s /mnt/s3files/.config/opencode/opencode.json
```

The root `AGENTS.md` defines the frontend role and the `harness:ui` contract. The
hidden `.config/opencode/opencode.json` selects the `amazon-bedrock` provider and
the `claude-sonnet-4-6` model.

## Verify

```bash
agentcore exec --it \
  --runtime "$(jq -r .runtime_arn coding-agents/opencode/runtime_config.json)" \
  --region us-west-2
```

Inside the Runtime, run `/app/run.sh` and ask opencode to build `chatbot.html`.
Verify that the page sends an MCP tools/call request and contains no local price
table or pricing arithmetic. Do not claim completion until the file exists and the
acceptance tests pass.

Reference: <https://opencode.ai/docs/>
