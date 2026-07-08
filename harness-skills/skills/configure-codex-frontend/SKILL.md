---
name: configure-codex-frontend
description: >-
  Configure Codex as the frontend builder in the three-agent AgentCore harness.
  Use for deploying the Codex Runtime, staging AGENTS.md and .codex/config.toml,
  or verifying that the generated chatbot delegates pricing to the MCP server.
---

# Configure the Codex frontend builder

Codex builds the thin chatbot UI. Claude Code builds the MCP backend and Kiro
validates the composed result. There is no race and no winner.

## Prerequisites

- `coding-agents/infra.config` exists.
- `openai.gpt-5.5` is enabled for Bedrock Mantle in `us-east-2`.
- The Runtime execution role can use the AWS SDK credential chain.
- Docker Buildx or Finch can build arm64 images.

Codex does not need an OpenAI key, AgentCore workload identity, or API-key
credential provider. The Runtime IAM role is the authentication path.

## Deploy

```bash
cd coding-agents/codex
./setup.sh
python deploy.py
```

`runtime_config.json` must contain a Runtime ARN and the Runtime must reach
`READY` before continuing.

## Stage project guidance

Copy the whole project configuration so hidden settings are preserved:

```bash
cp -R orchestrator/harness/codex/. /mnt/s3files/
test -s /mnt/s3files/AGENTS.md
test -s /mnt/s3files/.codex/config.toml
```

The root `AGENTS.md` defines the frontend role and UI contract. The hidden
`.codex/config.toml` selects the `amazon-bedrock` provider and model.

## Verify

```bash
agentcore exec --it \
  --runtime "$(jq -r .runtime_arn coding-agents/codex/runtime_config.json)" \
  --region us-west-2
```

Inside the Runtime, run `/app/run.sh` and ask Codex to build `chatbot.html`.
Verify that the page sends an MCP tools/call request and contains no local price
table or pricing arithmetic. Do not claim completion until the file exists and
the acceptance tests pass.

Reference: <https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-55.html>
