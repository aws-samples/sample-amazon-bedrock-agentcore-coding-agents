# Kiro on AgentCore Runtime

This folder builds the validation assistant. Kiro runs headlessly with a `ksk_`
API key. `setup.sh` stores that key in an AgentCore Identity credential provider,
and `/app/run.sh` retrieves it when a command shell starts. The key is not copied
into the image or shared workspace.

## Prepare the Kiro key

An administrator must enable API key generation for the Kiro subscription. Sign
in at <https://app.kiro.dev>, create a key under **API Keys**, and copy it while
it is shown. This prerequisite applies to an AWS guided event and to your own
account.

## Build and deploy

Prerequisites:

- `../infra.config` exists after `bash ../infra/setup.sh us-west-2`.
- Docker Buildx or Finch can build an arm64 image.
- Your AWS identity can create ECR, IAM, AgentCore Runtime, and AgentCore
  Identity resources.

Run:

```bash
export KIRO_API_KEY=ksk_xxx
./setup.sh
python deploy.py
```

`deploy.py` writes the Runtime ARN to `runtime_config.json` and mounts the shared
workspace at `/mnt/s3files`.

## Open a shell

Use the AgentCore CLI from this directory:

```bash
agentcore exec --it \
  --runtime "$(jq -r .runtime_arn runtime_config.json)" \
  --region us-west-2
```

Inside the microVM, run `/app/run.sh`. It reads `.kiro/steering/*.md` from the
active workspace and launches `kiro-cli chat --trust-all-tools`.

The console's Agents page calls the same command-shell service after a Runtime
ARN and Kiro key are saved in Settings. `connect.py` remains an optional SDK
helper.

Rotate a key by rerunning `setup.sh` with the new `KIRO_API_KEY`. A Runtime
redeploy is not required because each session retrieves the current provider
value.

## Files

| File | Purpose |
|---|---|
| `setup.sh` | Build the image and configure the key provider |
| `deploy.py` | Create or update the Runtime and shared mount |
| `run.sh` | Retrieve the key and launch Kiro |
| `steering/agent.md` | Baseline validator steering |
| `connect.py` | Optional SDK command-shell helper |
| `cleanup.py` | Delete Runtime, IAM, and optional Identity resources |
