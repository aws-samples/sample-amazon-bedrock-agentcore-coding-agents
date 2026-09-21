# Codex on AgentCore Runtime

This folder builds and deploys the workshop's frontend coding assistant. Codex
CLI is pinned to **0.155.1** and uses **`us.openai.gpt-5.6-sol`** through the
built-in **`amazon-bedrock-runtime`** provider. That provider uses
`https://bedrock-runtime.<region>.amazonaws.com/openai/v1` with Bedrock SigV4.
The Runtime IAM role supplies the AWS SDK credential chain; no OpenAI or
Bedrock API key is stored in the image.

The region comes from `AWS_REGION`, `AWS_DEFAULT_REGION`, or the deployment
configuration. The Runtime and its S3 Files access point must be in the same
region. This path does not use the older `amazon-bedrock` Mantle provider.

## Build and deploy

Prerequisites:

- `../infra.config` exists after `bash ../infra/setup.sh "$AWS_REGION"`.
- Docker Buildx or Finch can build an arm64 image.
- Your AWS identity can create ECR, IAM, and AgentCore Runtime resources.
- The account can invoke the configured model in the deployment region.
  `deploy.py` grants invocation and streaming on OpenAI foundation models and
  regional OpenAI inference profiles, plus `bedrock:InvokeModel` on
  `arn:aws:bedrock:<region>:<account>:project/default`. An SCP or model access
  restriction can still deny inference; Runtime `READY` is not an inference test.

Run:

```bash
./setup.sh
python3 deploy.py
```

`deploy.py` writes the resulting ARN to `runtime_config.json`. It mounts the
shared S3 Files access point at `/mnt/s3files` when configured. A bootstrap
deployment can use `WORKSHOP_DEFER_MOUNT=1` until Lab 1 attaches the mount.
Rebuild or mirror the updated image before deploying; changing only the role
registry does not update an existing container.

`WORKSHOP_CODEX_MODEL` selects the frontend default. `WORKSHOP_MODEL_CODEX`
overrides it; the legacy all-role `WORKSHOP_MODEL` also takes precedence over
the default. Prefer a role-specific override when the roster uses different
model providers. `/app/run.sh --model MODEL` and a per-task model override win
for their invocation. The coordinator uses `ORCHESTRATOR_MODEL_ID` independently.

## Open a shell

The customer-reproducible path is one AgentCore CLI command:

```bash
RUNTIME_ARN="$(jq -r .runtime_arn runtime_config.json)"
RUNTIME_REGION="$(printf '%s' "$RUNTIME_ARN" | cut -d: -f4)"
agentcore exec --it --runtime "$RUNTIME_ARN" --region "$RUNTIME_REGION"
```

Inside the shell, run `/app/run.sh`. The launcher starts in `/mnt/s3files`,
trusts only the active project for that process, and uses the
`amazon-bedrock-runtime` provider. With `WORKSHOP_AGENT_WORKDIR` set, it stays
in that worktree. The coordinator's headless path calls `codex exec` directly;
`entrypoint.sh` writes the provider region before serving `/ping`, so that
path is configured without needing the launcher.

`python connect.py` remains a repository helper for environments that need the
SDK form of the same command-shell API. It is not required for the workshop.

## Instruction precedence

- `/home/agent/.codex/AGENTS.md` supplies the container's baseline role.
- `/mnt/s3files/AGENTS.md` supplies project instructions for a direct shell.
- An orchestrated run receives its own root `AGENTS.md` in the run directory.
- `/home/agent/.codex/config.toml` contains model and provider settings.
- `CODEX_HOME`, if explicitly set, means the actual Codex config directory.
  The launcher does not repurpose it as the user's home.

Only source archives cross into the dispatched role's named local Git worktree
under `/tmp`. Git metadata and GitHub credentials do not enter those archives.
The worker has no automatic Gateway MCP configuration or Gateway invoke grant;
the coordinator owns GitHub reads and writes.
The canonical project configuration has no region pin; the container supplies it.

## Usage telemetry

The generated `[otel]` configuration sends native Codex logs to the container's
local OTLP HTTP receiver on port 4318. User-prompt logging and trace export are
disabled. `entrypoint.sh` starts the pinned OpenTelemetry collector and checks
its health endpoint before serving the Runtime.

The collector exports only successful `response.completed` events carrying token
usage to `/workshop/coding-agents/telemetry` in the deployment region. It removes
raw SSE frames, API attempt events, prompts, tool content, and unrelated attributes.
It retains the native token counters and the user, team, agent, run, and session
resource labels supplied by the console or coordinator. CloudWatch writes use the
Runtime role, scoped to that log group.

Codex 0.155.1 can emit a zero log timestamp with a valid observed timestamp. The
collector uses that observed timestamp when the log timestamp is zero, so the
event reaches the correct CloudWatch time window. It converts the native string
input/output counters to numbers without replacing missing measurements with zero.

Lab 3 compares these completed-response events with Claude Code's API request
events. Codex's input total already includes cached input; its reasoning tokens
are a subset of output. A failed or incomplete response supplies no completed
usage event. These counts therefore do not measure every attempted request.
An unavailable collector is reported in the Runtime startup log; CLI success
alone does not prove that usage was exported.

## Files

| File | Purpose |
|---|---|
| `setup.sh` | Build and push the arm64 image |
| `deploy.py` | Create or update the Runtime and shared mount |
| `entrypoint.sh` / `configure_codex.py` | Configure direct and interactive dispatch before startup |
| `run.sh` | Launch Codex in the active work directory |
| `AGENTS.md` | Baseline frontend-builder instructions |
| `.codex/config.toml` | Bedrock model and provider settings |
| `otel-collector-config.yaml` | Filter native usage events and export them to CloudWatch |
| `connect.py` | Optional SDK command-shell helper |
| `cleanup.py` | Delete the Runtime and its IAM role |

Reference: <https://docs.aws.amazon.com/bedrock/latest/userguide/inference-responses.html>
