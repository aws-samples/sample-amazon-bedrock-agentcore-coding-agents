# Codex on AgentCore Runtime

This folder builds and deploys the workshop's frontend coding assistant. Codex
uses `openai.gpt-5.5` through the Amazon Bedrock Mantle Responses endpoint. The
workshop pins `us-east-2`; the Runtime IAM role supplies the AWS SDK credential
chain, so no OpenAI key or Bedrock API key is stored in the image.

AWS currently documents GPT-5.5 on the `bedrock-mantle` endpoint with model ID
`openai.gpt-5.5`. Confirm current regional availability in the Bedrock model
card before changing `.codex/config.toml`.

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

Inside the shell, run `/app/run.sh`. The launcher starts in `/mnt/s3files`,
adds the active project directory to Codex trust settings, and uses the
`amazon-bedrock` provider from `.codex/config.toml`.

`python connect.py` remains a repository helper for environments that need the
SDK form of the same command-shell API. It is not required for the workshop.

## Instruction precedence

- `/home/agent/.codex/AGENTS.md` supplies the container's baseline role.
- `/mnt/s3files/AGENTS.md` supplies project instructions for a direct shell.
- An orchestrated run receives its own root `AGENTS.md` in the run directory.
- `/home/agent/.codex/config.toml` contains model, provider, and trust settings.

The attendee page copies the complete `orchestrator/harness/codex/` directory,
including its hidden `.codex` folder, into the shared workspace.

## Files

| File | Purpose |
|---|---|
| `setup.sh` | Build and push the arm64 image |
| `deploy.py` | Create or update the Runtime and shared mount |
| `run.sh` | Launch Codex in the active work directory |
| `AGENTS.md` | Baseline frontend-builder instructions |
| `.codex/config.toml` | Bedrock model and provider settings |
| `connect.py` | Optional SDK command-shell helper |
| `cleanup.py` | Delete the Runtime and its IAM role |

Reference: <https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-55.html>
