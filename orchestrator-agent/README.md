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
| `session_activity.py` | Register background builds with the Runtime async-task lifecycle |
| `model/load.py` | Bedrock model construction |
| `stage_engine.py` | Stage the root coordinator and use cases into the build context |
| `configure_deploy.py` | Wire role ARNs, IAM roles, and account settings into generated CLI config |
| `Dockerfile` | Build the coordinator container |

The model can clarify a request or call `list_presets`, `dispatch_backend`,
`dispatch_frontend`, `dispatch_validator`, `run_build`, and `run_status`.
`list_presets` is advisory and starts nothing. Dispatch tools submit work through
the same `orchestrator/engine.py` used by the console.

A dispatch returns while its build continues in a worker thread.
`session_activity.py` registers that work with `app.add_async_task()` before the
chat response closes. The SDK then reports `HealthyBusy` through `/ping`, so the
idle timer does not reclaim an active build when the caller disconnects.
The registration is completed once every worker stops, or when the engine's
execution bound expires. It makes no model calls or self-invocations. An
actually idle session can still expire; terminal results remain in the run store.

## Deploy the coordinator

Complete Lab 1 and the GitHub Gateway setup first. In the terminal where
`GITHUB_GATEWAY_URL`, `GITHUB_REPO`, and `AWS_REGION` are set, run:

```bash
cd ~/sample-amazon-bedrock-agentcore-coding-agents/orchestrator-agent
./deploy-coordinator.sh
```

The script prints the wiring, stages the engine, creates the CLI project, and
runs `configure_deploy.py` before validation and deployment. Configuration
includes the worker ARNs, execution roles, account, region, repository, and merge
policy. Generated files under `CodingAgents/` stay untracked.

The closing read-only probe asks the deployed coordinator which roles
`add-a-feature` uses. It should answer without creating a run.

Export model settings before running the script. It forwards the stack's
`WORKSHOP_CLAUDE_MODEL`, `WORKSHOP_OPENCODE_MODEL`, and `WORKSHOP_SMALL_MODEL`
values into the coordinator process. `ORCHESTRATOR_MODEL_ID` independently
selects the coordinator's chat model. Existing `WORKSHOP_MODEL` and
`WORKSHOP_MODEL_<ROLE>` overrides still take precedence for role dispatch;
per-request model choices take precedence over those. Blank named defaults
are omitted. Credentials are not copied from the host environment.
