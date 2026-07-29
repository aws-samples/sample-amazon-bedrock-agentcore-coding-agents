# Coordinator Runtime package

This directory is the BYO Container code location used by the AgentCore CLI in
Module 2. It wraps the repository's coordinator in `BedrockAgentCoreApp` and does
not duplicate routing or execution logic.

The shipped gate runtime supports Python and Node.js 22
(JavaScript/TypeScript). Add another language's toolchain to `Dockerfile` before
routing projects in that language.

## Package shape

| File | Purpose |
|---|---|
| `main.py` | Runtime HTTP entrypoint and Strands streaming adapter |
| `model/load.py` | Bedrock model construction |
| `stage_engine.py` | Stage the root coordinator and use cases into the build context |
| `configure_deploy.py` | Wire role ARNs, IAM roles, and account settings into generated CLI config |
| `Dockerfile` | Build the coordinator container |

The model can clarify a request or call `list_presets`, `dispatch_backend`,
`dispatch_frontend`, `dispatch_validator`, `run_build`, and `run_status`.
`list_presets` is advisory and starts nothing. Dispatch tools submit work through
the same `orchestrator/engine.py` used by the console.

## Build the generated CLI project

Follow the Workshop Studio page **Deploy the Multi-Agent Coordinator**. The
essential sequence is:

```bash
cd ~/sample-amazon-bedrock-agentcore-coding-agents/orchestrator-agent
python3 stage_engine.py

cd ~/sample-amazon-bedrock-agentcore-coding-agents
agentcore create --name CodingAgents --no-agent --skip-git
cd CodingAgents
agentcore add agent --name orchestrator --type byo --build Container \
  --language Python --framework Strands --model-provider Bedrock \
  --code-location ../orchestrator-agent --entrypoint main.py --protocol HTTP
```

`configure_deploy.py` then writes the three deployed role ARNs and the
CloudFormation execution roles to `CodingAgents/agentcore/agentcore.json`.
Generated AgentCore project files remain untracked.

Before dispatching a build, verify the container with a read-only request:

```bash
cd ~/sample-amazon-bedrock-agentcore-coding-agents/CodingAgents
agentcore dev --logs
# In another terminal:
cd ~/sample-amazon-bedrock-agentcore-coding-agents/CodingAgents
agentcore dev --stream \
  "Call list_presets and tell me which roles add-a-feature uses. Do not dispatch."
```

After this succeeds, deploy with `agentcore deploy --yes --json`. The workshop uses the
attendee's private repository created from this GitHub template for the real PR.
