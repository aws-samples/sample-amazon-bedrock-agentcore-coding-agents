# Multi-agent coordinator

This package coordinates the three coding assistants deployed in Module 1. The
shipped execution path dispatches to wired AgentCore Runtime ARNs. A missing
Runtime, artifact, test contract, or GitHub credential is an explicit error.

## Request flow

1. `chat.py` creates a Strands coordinator with the selected Bedrock model.
2. The model can ask one clarifying question or call
   `dispatch_backend`, `dispatch_frontend`, `dispatch_validator`, or `run_build`.
3. `engine.py` applies admission, context hydration, pre-flight, execution, and
   finalization around the selected work.
4. `AgentCoreExecutor` sends each role to its deployed Runtime. It does not fall
   back to an in-process builder.
5. `reviewer.py` runs the deterministic pytest floor, then an optional LLM judge.
   A red floor can never pass. The exact pass token is
   `LGTM: no changes needed`.
6. `github.py` composes one branch and opens a real PR. If no GitHub credential
   resolves, the PR step fails and `pr_url` remains null.

There is no race and no winner. Roles contribute different artifacts to one
deliverable. One failed review allows one bounded reimplementation pass, then the
run requires a human.

## Main seams

| File | Responsibility |
|---|---|
| `chat.py` | Strands conversation and tool selection |
| `router.py` | Versioned workflow registry and advisory route ladder |
| `engine.py` | Five-phase lifecycle, state, compose, and finalization |
| `executor.py` | Real AgentCore executor boundary |
| `runtime_exec.py` | Command-shell dispatch and artifact readback |
| `runtime_stage.py` | Stage skills and grading data on S3 Files |
| `runtime_config.py` | Resolve per-role Runtime ARN fleets or explicit dev URIs |
| `reviewer.py` | Pytest floor, critique, and optional stricter LLM judge |
| `github.py` | Credential resolution, branch push, and real PR creation |
| `identity_baggage.py` | Carry submitter metadata for audit and cost grouping |
| `connection_api.py` | JSON and SSE adapter used by the console |

The wire contract is in [API_CONTRACT.md](API_CONTRACT.md).

## Configuration

Runtime targets resolve from `AGENTCORE_RUNTIME_<ROLE>` or the file selected by
`WORKSHOP_RUNTIME_CONFIG`. A role can hold multiple ARNs and dispatch uses round
robin selection. An explicit `http://` or `https://` target is the supported
`agentcore dev` test seam. It is not an unwired fallback.

Other important settings:

- `WORKSHOP_REPO_ROOT` selects the attendee fork used as the compose base.
- `WORKSHOP_RUNS_DIR` selects untracked run state.
- `WORKSHOP_GITHUB_SETTINGS` isolates the GitHub credential store.
- `GITHUB_TOKEN` and `GITHUB_REPO` provide the environment credential path.
- `WORKSHOP_RUNTIME_BUCKET` overrides the S3 staging bucket.
- `WORKSHOP_BEDROCK_REGION` selects coordinator inference region.

GitHub attributes a PR to the PAT owner or App installation used for the API
call. Cognito identity baggage records who submitted the run. Those are separate
facts and this package does not infer OAuth OBO delegation.

## Run focused tests

The test suite injects `FixtureExecutor` explicitly. That fixture exercises the
same lifecycle and review code without pretending to be a deployed Runtime.

```bash
python3 -m pytest \
  orchestrator/test_router.py \
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

The run panel must show only roles chosen by the coordinator, per-role shell
transcripts, the deterministic gate result, and a real `pr_url` after GitHub
accepts the PR.
