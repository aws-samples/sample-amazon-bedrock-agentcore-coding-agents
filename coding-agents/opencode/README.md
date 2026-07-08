# opencode on AgentCore Runtime

This folder builds and deploys the workshop's frontend coding assistant. opencode
runs `anthropic.claude-sonnet-4-6` through Amazon Bedrock in the Runtime's own
region; the Runtime IAM role supplies the AWS SDK credential chain, so no API key
is stored in the image. Because it uses plain Bedrock (not the OpenAI/mantle
path), it is unaffected by the GPT-5.x allowlisting that gates the Codex path.

## Build and deploy

Prerequisites:

- `../infra.config` exists after `bash ../infra/setup.sh us-west-2`.
- Docker Buildx or Finch can build an arm64 image.
- Your AWS identity can create ECR, IAM, and AgentCore Runtime resources.

Run:

```bash
./setup.sh
python deploy.py
```

`deploy.py` writes the resulting ARN to `runtime_config.json` and mounts the
shared S3 Files access point at `/mnt/s3files`.

## Open a shell

The customer-reproducible path is one AgentCore CLI command:

```bash
agentcore exec --it \
  --runtime "$(jq -r .runtime_arn runtime_config.json)" \
  --region us-west-2
```

Inside the shell, run `/app/run.sh`. The launcher starts in `/mnt/s3files` and
uses the `amazon-bedrock` provider from `~/.config/opencode/opencode.json`.

`python connect.py` remains a repository helper for environments that need the
SDK form of the same command-shell API. It is not required for the workshop.

## Instruction precedence

- `/home/agent/AGENTS.md` supplies the container's baseline role.
- `/mnt/s3files/AGENTS.md` supplies project instructions for a direct shell.
- An orchestrated run receives its own root `AGENTS.md` in the run directory.
- `~/.config/opencode/opencode.json` contains model and provider settings.

The attendee page copies the complete `orchestrator/harness/opencode/` directory,
including its config, into the shared workspace.

## Files

| File | Purpose |
|---|---|
| `setup.sh` | Build and push the arm64 image |
| `deploy.py` | Create or update the Runtime and shared mount |
| `run.sh` | Launch opencode in the active work directory |
| `AGENTS.md` | Baseline frontend-builder instructions |
| `opencode.json` | Bedrock model and provider settings |
| `connect.py` | Optional SDK command-shell helper |
| `cleanup.py` | Delete the Runtime and its IAM role |

Reference: <https://opencode.ai/docs/>
