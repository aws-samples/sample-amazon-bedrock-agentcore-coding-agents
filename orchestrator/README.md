# Multi-agent coordinator

This package coordinates the active coding-assistant roster declared in
`roles.py`. The shipped execution path dispatches to wired AgentCore Runtime
ARNs. A missing Runtime, artifact, validator-authored check, or GitHub
configuration is an explicit error.

## Request flow

1. `chat.py` creates a Strands coordinator with the selected Bedrock model.
2. The model can ask one clarifying question or call
   `dispatch_backend`, `dispatch_frontend`, `dispatch_validator`, or `run_build`.
3. `engine.py` applies admission, context hydration, pre-flight, execution, and
   finalization around the selected work.
4. `AgentCoreExecutor` sends each role to its deployed Runtime. It does not fall
   back to an in-process builder.
5. Each builder gets a named linked Git worktree on local disk, a unique work id,
   branch, and one pull request against the repository's DEFAULT branch.
6. Then, PER PULL REQUEST: `reviewer.py` executes the validator-authored
   task-specific check against that pull request's tree, then runs one
   independent, read-only review over that pull request alone. Its structured
   response must cover both adversarial-verification and design/integration
   lenses, never reuses a maker conversation, and can make a green gate stricter.
   A red executable can never pass. The exact approval token is
   `LGTM: no changes needed`.
7. `github.py` merges each approved pull request on its own. There is no combined
   candidate, no merge queue, and no separate final PR. If no Gateway resolves,
   pre-flight fails before agent work.

There is no race and no conflict winner. Roles start independently against a shared
contract, and a red pull request never blocks a green sibling. Each pull request is
checked and reviewed against the default branch AS IT STANDS, so once one role's work
merges, the next role's check runs against a tree that contains it. Red evidence buys
one repair turn for the responsible existing pull request. Separately, when a sibling
merges and moves a path a still-open pull request also changed, that owner gets one
bounded refresh. Merging supports either human review or guarded auto-merge.

## Main seams

| File | Responsibility |
|---|---|
| `roles.py` | The role roster, declared ONCE: kind, capability, steering, CLI, env |
| `presets.py` | Routing: which capabilities a request needs |
| `chat.py` | Strands conversation and tool selection |
| `role_graph.py` | The agent-execution schedule as a Strands graph |
| `integration_plan.py` | Flexible shared contract and bounded repair routing |
| `work_items.py` | Linked worktrees, work ids, isolated patches, dependency order, candidate assembly |
| `engine.py` | Five-phase lifecycle, state, compose, and finalization |
| `executor.py` | Real AgentCore executor boundary |
| `runtime_exec.py` | Command-shell dispatch, Runtime-local worktrees, and tree readback |
| `runtime_stage.py` | Exchange normalized source and skill archives through S3 |
| `runtime_config.py` | Resolve per-role Runtime ARN fleets or explicit dev URIs |
| `reviewer.py` | Runs the validator check plus one integrated read-only review |
| `replay.py` | The run's narrative, for the PR body (reports, never judges) |
| `github.py` | Gateway config resolution, PR creation, and the `doctor` preflight |
| `run_store.py` | Durable run state, so a verdict outlives its session |
| `diagnose.py` | The shareable diagnostic bundle (read-only, collects no credentials) |
| `identity_baggage.py` | Carry submitter metadata for audit and cost grouping |
| `policy.py` | Guardrails every engine-run command is screened against |
| `llm.py` | Model id resolution and Bedrock invocation |
| `connection_api.py` | JSON and SSE adapter used by the console |
| `watch_agents.py` | Developer client for manually opened console Runtime terminals; orchestrated builds use Chat/run_status |
| `fixture_executor.py` | TEST-ONLY deterministic producer; never on the shipped path |

The wire contract is in [API_CONTRACT.md](API_CONTRACT.md).

## Configuration

Runtime targets resolve from `AGENTCORE_RUNTIME_<ROLE>` or the file selected by
`WORKSHOP_RUNTIME_CONFIG`. A role can hold multiple ARNs and dispatch uses round
robin selection. An explicit `http://` or `https://` target is the supported
`agentcore dev` test seam. It is not an unwired fallback.

Other important settings:

- `WORKSHOP_ROLES` selects which registered roles are served (an unknown id fails loud).
- `WORKSHOP_RUNS_DIR` selects untracked run state and coordinator-local worktrees.
  The deployed coordinator sets it to `/tmp/workshop-runs`; the box console uses
  repo-local `.runs`. Neither points at the S3 Files NFS mount.
- `WORKSHOP_MAX_RUN_STATE` caps how many persisted run verdicts are kept.
- `WORKSHOP_S3FILES_DIR` points the S3 Files mount at a local dir (the dev seam).
- `WORKSHOP_GITHUB_SETTINGS` isolates the GitHub settings file.
- `GITHUB_GATEWAY_URL` and `GITHUB_REPO` wire the PR path; there is no token.
- `WORKSHOP_RUNTIME_BUCKET` overrides the S3 staging bucket, and is where the
  deployed coordinator mirrors run state (its own filesystem dies with the microVM).
- `WORKSHOP_BEDROCK_REGION` selects coordinator inference region.
- `WORKSHOP_REVIEW_MODEL` selects the model for the integrated read-only review.
- `WORKSHOP_FINAL_MERGE_POLICY` is `human_review` (default) or `auto`.
  `WORKSHOP_MERGE_POLICY` remains a compatibility alias.

GitHub attributes a PR to the App installation whose token the MCP server minted
for the call. Cognito identity baggage records who submitted the run. Those are
separate facts and this package does not infer OAuth OBO delegation.

Before deploying the coordinator, prove the PR path end to end. Both commands
are safe to re-run. Their GitHub check idempotently resets the empty
`workshop/doctor` branch to prove the App can write, but creates no file or pull
request:

```bash
python3 orchestrator/github.py doctor   # can the App reach THIS repo?
python3 orchestrator/diagnose.py        # roles wired, gateway, recent verdicts
```

## Run focused tests

The test suite injects `FixtureExecutor` explicitly. That fixture exercises the
same lifecycle and review code without pretending to be a deployed Runtime.

```bash
python3 -m pytest orchestrator/ -q          # every unit test in this package
python3 -m pytest \
  orchestrator/test_reviewer.py \
  orchestrator/test_runtime_config.py \
  orchestrator/test_runtime_exec.py \
  orchestrator/test_resilience.py -q
```

## Run against deployed roles

First deploy all three folders under `coding-agents/`, then save their ARNs in
Settings or `runtime_config.py`. Start the console as described in
[console/README.md](../console/README.md), open **Tasks**, and submit an
outcome-oriented request.

The run panel must show only routed roles, their unique work ids and pull requests,
every executable checkpoint, each pull request's independent review with both required
lenses, and its real `pr_url` and merge state.
