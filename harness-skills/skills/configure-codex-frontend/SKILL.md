---
name: configure-codex-frontend
description: >-
  Configure Codex as the frontend builder in the three-agent AgentCore harness.
  Use for deploying the Codex Runtime, staging AGENTS.md and .codex/config.toml,
  or verifying that the generated interface resolves its service address at runtime
  and delegates work to the MCP server.
---

> **RESTORE PATH - NOT THE SERVED ROLE**
>
> Codex requires GPT-5.x (OpenAI) via Bedrock Mantle. Workshop Studio accounts
> cannot be granted the `openai.gpt-5.5` Mantle entitlement (org SCP blocks the
> grant; see EE-14394). Any attempt to run Codex on a Workshop Studio account
> returns a 401. The served frontend in the workshop is opencode (Bedrock-native
> `claude-sonnet-4-6`, no Mantle entitlement needed; see
> `configure-opencode-frontend`).
>
> Use this skill only if you are restoring the Codex path in a context where the
> Mantle entitlement is available (non-Workshop Studio account with GPT-5.x
> enabled). All Codex assets (`coding-agents/codex/`) remain in the repository as
> the restore path.

# Configure the Codex frontend builder

Codex builds the frontend interface. Claude Code builds the backend and a second
Claude Code (the validator) validates the composed result. There is no race and no
winner.

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

The root `AGENTS.md` defines the frontend role. The hidden
`.codex/config.toml` selects the `amazon-bedrock` provider and model.

## The thin-client rule (why it is a browser fact, not a preference)

Any interface the frontend builds must resolve its service address at RUNTIME,
never at build time. This is not a use-case preference; it is a browser constraint:

- `localhost` and `127.0.0.1` in a page mean the machine running the BROWSER.
  A URL baked at build time is dead the moment the page moves to any other host.
- A cross-origin JSON POST is preflighted by the browser. The service must answer
  `OPTIONS` with the correct `Access-Control-Allow-Origin` header or the browser
  will block the request before it reaches the server.

These constraints hold for every interface the frontend produces, regardless of the
task.

## Verify

```bash
agentcore exec --it \
  --runtime "$(jq -r .runtime_arn coding-agents/codex/runtime_config.json)" \
  --region us-west-2
```

Inside the Runtime, run `/app/run.sh` and give Codex the task. Verify that the
produced interface resolves the backend service address at runtime (not hardcoded),
delegates backend work to the MCP server via `tools/call`, and handles `OPTIONS`
preflight correctly if it runs cross-origin. Do not claim completion until the
interface loads and a round-trip through the backend succeeds.

Reference: <https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-55.html>
