---
name: configure-codex-frontend
description: >-
  Configure Codex as the frontend builder in the AgentCore harness.
  Use for deploying the Codex Runtime, staging AGENTS.md and .codex/config.toml,
  or verifying that the generated interface resolves its service address at runtime
  when the request calls for a networked interface.
---

# Configure the Codex frontend builder

Codex builds the frontend interface. Claude Code builds the backend; Kiro authors
an executable check that the engine runs. A separate reviewer inspects the pull
request, and the workshop's default merge policy leaves it open for a person.
The request determines the files, language, and design.

## Prerequisites

- `coding-agents/infra.config` exists.
- The account can invoke `us.openai.gpt-5.6-sol` in the deployment region, or
  `WORKSHOP_CODEX_MODEL` names an available Runtime Responses model.
- `AWS_REGION` or `AWS_DEFAULT_REGION` identifies the deployment region.
- The Runtime execution role can use the AWS SDK credential chain.
- Docker Buildx or Finch can build arm64 images.

Codex does not need an OpenAI key, AgentCore workload identity, or API-key
credential provider. Codex **0.155.1** uses the built-in
`amazon-bedrock-runtime` provider and the Runtime IAM role's AWS credential chain.
The deploy helper grants invocation and streaming on OpenAI model/profile
targets, plus `bedrock:InvokeModel` on the regional `project/default` ARN.
Do not add a Mantle wildcard or a GitHub Gateway grant to this worker.

## Deploy

```bash
cd coding-agents/codex
./setup.sh
python3 deploy.py
```

`runtime_config.json` must contain a Runtime ARN and the Runtime must reach
`READY` before continuing. Test an actual CLI turn too; `/ping` and `READY`
confirm process/deployment health, not model access.

## Stage project guidance

From the repository root, stage the frontend's project guidance and design skill
for a direct Lab 1 shell:

```bash
cp orchestrator/harness/codex/AGENTS.md /mnt/s3files/AGENTS.md
mkdir -p /mnt/s3files/skills
cp -R harness-skills/skills/frontend-design /mnt/s3files/skills/
test -s /mnt/s3files/AGENTS.md
test -s /mnt/s3files/skills/frontend-design/SKILL.md
```

The root `AGENTS.md` defines the frontend role. The container's global Codex
configuration supplies provider, model, and region. `entrypoint.sh` configures it
before serving shells, including direct `codex exec` dispatch. The launcher
trusts its actual work directory for that process without changing `CODEX_HOME`.

Lab 2 parses the canonical steering's `harness:setup` block and stages the same
design skill beside the role's source in its named linked worktree under `/tmp`.
The skill travels in its own Runtime input archive. Honor exclusive ownership in
`.workshop/integration-brief.md`; do not implement a sibling role's capability.
Keep Git metadata local and transfer only source archives. No GitHub credential
belongs in the Runtime.

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
RUNTIME_ARN="$(jq -r .runtime_arn coding-agents/codex/runtime_config.json)"
RUNTIME_REGION="$(printf '%s' "$RUNTIME_ARN" | cut -d: -f4)"
agentcore exec --it --runtime "$RUNTIME_ARN" --region "$RUNTIME_REGION"
```

Inside the Runtime, run `/app/run.sh` and give Codex the task. Verify the real
command exit and produced files. If the interface calls a backend, exercise that
round trip and its browser behavior, including cross-origin preflight when
applicable. The validator decides the task-specific executable check; deployment
configuration must not prescribe the application or turn a failed check green.

Reference: <https://docs.aws.amazon.com/bedrock/latest/userguide/inference-responses.html>
