# Claude Code validator on AgentCore Runtime

This folder builds the validator coding assistant: a second Claude Code, steered
by an acceptance-contract `CLAUDE.md` instead of the backend build spec. It is the
RESTORE PATH, not the served roster: the served checker is Kiro. Restore it with
`WORKSHOP_ROLES=claude-code,opencode,claude-code-validator` for an account with no
Kiro subscription, which is the only thing the served Kiro checker needs that
Bedrock alone cannot give.

It decides when a build is done by AUTHORING an executable check for the
deliverable in front of it, and the engine reads that executable's real exit code.
There is no deterministic grading contract: nothing in this repository pre-encodes
what a correct answer looks like, because nobody knew what would be asked. Like the backend, it calls Amazon Bedrock with the Runtime IAM role, so
no model API key is stored in the image. The deployed microVM mounts the shared
workspace at `/mnt/s3files`.

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

`deploy.py` writes the Runtime ARN to `runtime_config.json`.

## Open a shell

Use the AgentCore CLI from this directory:

```bash
agentcore exec --it \
  --runtime "$(jq -r .runtime_arn runtime_config.json)" \
  --region us-west-2
```

Inside the microVM, run `/app/run.sh`. For one command without an interactive
terminal:

```bash
agentcore exec \
  --runtime "$(jq -r .runtime_arn runtime_config.json)" \
  --region us-west-2 "claude --version"
```

The console's Agents page calls the same command-shell service after a Runtime
ARN is saved in Settings. `connect.py` remains an optional SDK helper for
debugging environments that cannot use the CLI.

## Files

| File | Purpose |
|---|---|
| `setup.sh` | Build and push the arm64 image |
| `deploy.py` | Create or update the Runtime and shared mount |
| `run.sh` | Configure the MCP client and launch Claude Code |
| `CLAUDE.md` | Baseline validator (acceptance-gate) instructions |
| `settings.json` | Claude Code settings |
| `connect.py` | Optional SDK command-shell helper |
| `cleanup.py` | Delete the Runtime and its IAM role |
