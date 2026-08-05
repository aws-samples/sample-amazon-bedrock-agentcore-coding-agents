---
name: configure-opencode-frontend
description: >-
  Configure opencode as the frontend builder in the three-agent AgentCore harness.
  Use for deploying the opencode Runtime, staging AGENTS.md and
  .config/opencode/opencode.json, or verifying that the generated interface resolves
  its service address at runtime and delegates work to the backend MCP server.
  opencode runs Bedrock-native (amazon-bedrock provider, claude-sonnet-4-6) with no
  API key.
---

# Configure the opencode frontend builder

opencode builds the frontend interface. Claude Code builds the backend and Kiro
(the validator) validates the composed result. There is no race and no winner.

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

The root `AGENTS.md` defines the frontend role. The hidden
`.config/opencode/opencode.json` selects the `amazon-bedrock` provider and
the `claude-sonnet-4-6` model.

## The thin-client rule (why it is a browser fact, not a preference)

Any interface the frontend builds must resolve its service address at RUNTIME,
never at build time. This is not a use-case preference; it is a browser constraint:

- `localhost` and `127.0.0.1` in a page mean the machine running the BROWSER.
  A URL baked at build time is dead the moment the page moves to any other host.
- A cross-origin JSON POST is preflighted by the browser. The service must answer
  `OPTIONS` with the correct `Access-Control-Allow-Origin` header or the browser
  will block the request before it reaches the server.

These constraints hold for every interface the frontend produces, regardless of the
task. The agent should read the service address from an env var or a config endpoint
at startup, not embed it in source.

## Verify

```bash
agentcore exec --it \
  --runtime "$(jq -r .runtime_arn coding-agents/opencode/runtime_config.json)" \
  --region us-west-2
```

Inside the Runtime, run `/app/run.sh` and give opencode the task. Verify that the
produced interface resolves the backend service address at runtime (not hardcoded),
delegates backend work to the MCP server via `tools/call`, and handles `OPTIONS`
preflight correctly if it runs cross-origin. Do not claim completion until the
interface loads and a round-trip through the backend succeeds.

Reference: <https://opencode.ai/docs/>
