# Coding Agents on Amazon Bedrock AgentCore Runtime

[![The sample console: Claude Code, opencode and Kiro on AgentCore Runtime, with a live Claude Code session attached to its Runtime ARN](docs/media/console-walkthrough-poster.png)](docs/media/console-walkthrough.mp4)

*The sample console, recorded on a real deployment: the three served roles, a live
Runtime session, and the governance views. [Play the walkthrough
(22s)](docs/media/console-walkthrough.mp4).*

Run Claude Code (backend), opencode (frontend), and Kiro (validator) on Amazon
Bedrock AgentCore Runtime. Give the team one request and receive one checked pull
request. A second Claude Code is kept as the validator's restore path, so an account
without a Kiro subscription runs the same workshop with one env var.

Each builder works in a named linked Git worktree and separate pull request. The
worktree is local to the coordinator or Runtime; only one normalized source archive
crosses the Runtime boundary. The validator writes an executable for that request,
and the orchestrator runs it. One independent,
read-only review then applies two required lenses to each pull request on its own:
adversarial verification and design/integration. The reviewer never sees a
builder's conversation or edits a builder's code.

This repo is the full workshop payload. Clone it and follow the workshop content; every
step is reproducible with the CLI, starting from this one clone.

> **Current project-language support:** Python and Node.js 22
> (JavaScript/TypeScript). Add the required toolchain to
> `orchestrator-agent/Dockerfile` before using another language, because that
> container executes the validator's check.

This repository is the single source of truth for all demo and harness **code**. The
matching Workshop Studio teaching content (guided lab pages and the CloudFormation
template) is published on Workshop Studio; the CloudFormation bootstrap clones this
repository directly into the box home, so the customer-reproducible path is exactly a
`git clone` of this URL (which yields `~/sample-amazon-bedrock-agentcore-coding-agents`)
followed by the CLI steps the workshop teaches.

This repository is also a GitHub **template**. In Lab 2 of the workshop you click
**Use this template -> Create a new repository** to get your own isolated copy (no
fork, no shared credentials). Each builder opens ONE role pull request against the
repository's default branch, and each pull request is checked, reviewed, and merged
on its own: there is no combined candidate, no merge queue, and no separate final
pull request. The validator's executable must pass for each pull request, run
against the default branch as it stands plus that diff. The GitHub App authors every
pull request.

Builders begin independently from the same shared plan. Because each pull request is
checked and reviewed against the default branch AS IT STANDS, once an earlier role's
pull request merges the next role's check runs against a tree that already contains
it. When that merge moves a path a still-open pull request also changed, its owner
gets one bounded refresh. This catches cases where separate branches each worked but
did not agree with each other. It is separate from the one repair allowed after a
failed check or review finding.

Lab 2 deliberately keeps Git metadata off S3 Files. The deployed coordinator uses
`/tmp/workshop-runs`, each coding-agent Runtime creates its turn's worktree under
`/tmp`, and `.git` never enters the exchange archive. S3 Files remains the shared
workspace for direct shell work in Lab 1.

## Layout

- `coding-agents/` the three coding-agent harnesses (container + setup.sh + deploy.py + connect.py) and shared infra/gateway
  - `claude-code/` backend builder (Claude Code, native Bedrock)
  - `opencode/` frontend builder (opencode, native Bedrock)
  - `kiro/` acceptance-contract validator (Kiro CLI; steered by `.kiro/steering/*.md` with `inclusion: always`, which directs it to author an executable check whose exit code is the gate; authenticates with your own `ksk_` key, fetched from the AgentCore Identity Token Vault at session start)
  - `claude-code-validator/` restore path (hidden; kept restorable like `codex/`, not on the served roster by default): the same acceptance-check-authoring contract in a `CLAUDE.md`, Bedrock-native with no key, for an account without a Kiro subscription
- `orchestrator/` the Strands orchestrator engine (routing, engine, executor, reviewer, github)
  - `orchestrator/roles.py` declares the served roster (`WORKSHOP_ROLES`-configurable); this is the single place role ids, kinds (builder/checker), and capabilities (backend/frontend/validator) live
- `orchestrator-agent/` the deployable Strands agent bundle
- `console/` the React + FastAPI console (Agents / Fleets / Governance)
- `interactive-api/` `metrics-api/` the Stage 1 interactive + Stage 3 metrics engines
- `harness-skills/` agent skills used to configure the harnesses
- `e2e/` the end-to-end workshop journey + integration suite

## Tests

The full suite is collected from this repo root:

```bash
python3 -m pytest -q
```

`pytest.ini` declares the `testpaths`; the root `conftest.py` isolates GitHub and
Runtime credentials so no test can read a token or open a pull request.

## When something is not working

Both collect no credentials and are safe to re-run:

```bash
python3 orchestrator/github.py doctor   # can the GitHub App reach YOUR repo?
python3 orchestrator/diagnose.py        # roles wired, gateway, recent verdicts
python3 orchestrator/diagnose.py <run_id>   # + that run's engine-log tail
```

Run `doctor` BEFORE deploying the coordinator. The mistakes that cost the most time
(an App installed on a different repository, a wrong owner in `GITHUB_REPO`) all pass
a plain gateway health check and then fail when a build tries to write, after the
agents have already run. `diagnose.py` invokes the same GitHub doctor check.
To prove write permission, that check idempotently resets the
`workshop/doctor` branch; it writes no file and opens no pull request.

Every finished run also persists its verdict, so `run_status <run_id>` still answers
from a NEW coordinator session, and `list_runs` finds it when the run id is lost.

## License

MIT-0. See `LICENSE`.
