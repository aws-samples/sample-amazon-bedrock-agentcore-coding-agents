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
| `promote_runtime.py` | Verify the selected platform on the current CLI/CDK Runtime; update it when needed |
| `probe_coordinator.py` | Require a real, non-error answer from the deployed coordinator |
| `Dockerfile` | Build the coordinator container |

The model can clarify a request or call `list_presets`, `run_build`, `run_status`,
and the builder dispatch tools generated from the connected role registry.
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
`GITHUB_GATEWAY_URL`, `GITHUB_REPO`, and `AWS_REGION` are set, deploy with:

```bash
cd ~/sample-amazon-bedrock-agentcore-coding-agents/orchestrator-agent
./deploy-coordinator.sh
```

**V1 is the default.** To opt into V2, first run
`export WORKSHOP_RUNTIME_PLATFORM_VERSION=V2` in that terminal. Set it to `V1`
to select V1 explicitly. Only these exact values are accepted; an unset setting
uses the default in `coding-agents/cli-versions.json`.

The script prints the wiring, stages the engine, creates the CLI project, and
runs `configure_deploy.py` before validation and deployment. Configuration
includes the worker ARNs, execution roles, account, region, repository, and merge
policy. Generated files under `CodingAgents/` stay untracked.

After the CLI deployment succeeds, the wrapper verifies the Runtime's identity
and configuration against the current CDK deployment. A Runtime already `READY`
on the selected platform needs no extra platform update. When an update is
needed, the shared SDK helper waits for its exact accepted revision to become
`READY` on the selected platform before running the application probe. V2 snapshot
preparation can take several minutes. A failure or bounded deadline stops deployment.

If you deploy the generated project manually, keep the same
`WORKSHOP_RUNTIME_PLATFORM_VERSION` environment and complete the same verification
from `CodingAgents/` after `agentcore deploy` succeeds:

```bash
python3 ../orchestrator-agent/promote_runtime.py --project .
```

The shared `coding-agents/cli-versions.json` pins the deployment CLI, AWS SDK,
and coding-agent CLIs. Install those versions through the workshop setup scripts
before deploying.

The closing read-only probe asks the deployed coordinator which roles
`add-a-feature` uses. It should answer without creating a run. The wrapper checks
the CLI's structured result and response, because a streamed model error can
otherwise return exit code zero. An error, empty answer, or 180-second timeout
stops the script before it prints the build-submission instructions.

Export model settings before running the script. It forwards the stack's
`WORKSHOP_CLAUDE_MODEL`, `WORKSHOP_CODEX_MODEL`, and `WORKSHOP_SMALL_MODEL`
values into the coordinator process. `ORCHESTRATOR_MODEL_ID` independently
selects the coordinator's chat model. Existing `WORKSHOP_MODEL` and
`WORKSHOP_MODEL_<ROLE>` overrides still take precedence for role dispatch;
per-request model choices take precedence over those. Blank named defaults
are omitted. Credentials are not copied from the host environment.
