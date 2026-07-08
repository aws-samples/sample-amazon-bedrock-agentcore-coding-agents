# Coordinator Runtime package

This directory is the BYO Container code location used by the AgentCore CLI in
Module 2. It wraps the repository's coordinator in `BedrockAgentCoreApp` and does
not duplicate routing or execution logic.

## Package shape

| File | Purpose |
|---|---|
| `main.py` | Runtime HTTP entrypoint and Strands streaming adapter |
| `model/load.py` | Bedrock model construction |
| `stage_engine.py` | Stage the root coordinator and use cases into the build context |
| `configure_deploy.py` | Wire role ARNs, IAM roles, and account settings into generated CLI config |
| `Dockerfile` | Build the coordinator container |

The model can clarify a request or call `route_task`, `dispatch_backend`,
`dispatch_frontend`, `dispatch_validator`, `run_build`, and `run_status`.
`route_task` is advisory and starts nothing. Dispatch tools submit work through
the same `orchestrator/engine.py` used by the console.

## Build the generated CLI project

Run the customer instructions in
[Deploy the Multi-Agent Coordinator](../content/30-stage2-orchestrate/2-deploy-the-orchestrator/index.en.md).
The essential sequence is:

```bash
cd ~/src/orchestrator-agent
python3 stage_engine.py

cd ~/src
agentcore create --name CodingAgents --no-agent --skip-git
cd CodingAgents
agentcore add agent --name orchestrator --type byo --build Container \
  --language Python --framework Strands --model-provider Bedrock \
  --code-location ../orchestrator-agent --entrypoint main.py --protocol HTTP
```

`configure_deploy.py` then writes the three deployed role ARNs and the
CloudFormation execution roles to `CodingAgents/agentcore/agentcore.json`.
Generated AgentCore project files remain untracked.

Before GitHub is connected, verify the container without dispatching a build:

```bash
cd ~/src/CodingAgents
agentcore dev --logs
# In another terminal:
cd ~/src/CodingAgents
agentcore dev --stream \
  "Use route_task to classify a backend-only version-string fix. Do not dispatch."
```

After this succeeds, deploy with `agentcore deploy`. The next workshop page
connects a disposable fork before the first real multi-agent run.
