# Coding Agents on Amazon Bedrock AgentCore Runtime

Run multiple coding agents (Claude Code, Claude Code validator, opencode) in parallel on
Amazon Bedrock AgentCore Runtime, orchestrate them from one chat, grade their output
deterministically with `pytest`, and govern the fleet (identity, per-user cost,
observability).

This repo is the full workshop payload. Clone it and follow the workshop content; every
step is reproducible with the CLI, starting from this one clone.

This repository is the single source of truth for all demo and harness **code**. The
matching Workshop Studio teaching content (guided lab pages and the CloudFormation
template) is published on Workshop Studio; the CloudFormation bootstrap clones this
repository directly into the box home, so the customer-reproducible path is exactly a
`git clone` of this URL (which yields `~/sample-amazon-bedrock-agentcore-coding-agents`)
followed by the CLI steps the workshop teaches.

This repository is also a GitHub **template**. In Module 1 of the workshop you click
**Use this template -> Create a new repository** to get your own isolated copy (no
fork, no shared credentials): the sample module it ships
(`usecase-sample-to-mcp/cost_analyzer.py`) is the raw material the coding agents
transform, and the Module 2 pull request the orchestrator opens lands on that
per-attendee repository through the GitHub App gateway.

## Layout

- `coding-agents/` the three coding-agent harnesses (container + setup.sh + deploy.py + connect.py) and shared infra/gateway
  - `claude-code/` backend MCP-server builder (Claude Code, native Bedrock)
  - `claude-code-validator/` acceptance-contract validator (Claude Code, native Bedrock; steered by a `harness:gate` CLAUDE.md)
  - `opencode/` frontend chatbot UI builder (opencode, native Bedrock)
  - `kiro/` legacy restore path (hidden; kept restorable like `codex/`, not on any served roster)
- `orchestrator/` the Strands orchestrator engine (router, engine, executor, reviewer, github)
- `orchestrator-agent/` the deployable Strands agent bundle
- `console/` the React + FastAPI console (Agents / Fleets / Governance)
- `interactive-api/` `metrics-api/` the Stage 1 interactive + Stage 3 metrics engines
- `usecase-sample-to-mcp/` the use case: a plain Python module (`cost_analyzer.py`, the input the agents transform; also the grading reference) plus the `pytest` grading contract
- `harness-skills/` agent skills used to configure the harnesses
- `e2e/` the end-to-end workshop journey + integration suite

## Tests

The full suite is collected from this repo root:

```bash
python3 -m pytest -q
```

`pytest.ini` declares the `testpaths`; the root `conftest.py` isolates GitHub /
runtime credentials so no test can read a real token or open a real pull request.

## License

MIT-0. See `LICENSE`.
