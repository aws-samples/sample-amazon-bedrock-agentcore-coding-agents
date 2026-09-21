# Coding Agents on Amazon Bedrock AgentCore Runtime

Run Claude Code (backend), Codex (frontend), and Kiro (validator) on Amazon
Bedrock AgentCore Runtime V2. Give the team one request; the coordinator opens a
checked and reviewed pull request for each selected builder. The guided game selects Claude Code
and Kiro, producing one builder PR. A separate Claude Code validator remains a
restore path: deploy and wire it, then select it with `WORKSHOP_ROLES`.
opencode remains the alternate frontend, selected the same way.

The backend defaults to **Claude Opus 5** (`us.anthropic.claude-opus-5`) with
`high` effort. Codex uses **GPT-5.6 Sol** (`us.openai.gpt-5.6-sol`) with
`medium` reasoning through the native Bedrock Runtime provider. The stack's
`BackendModelId` and `FrontendModelId` parameters select different models when
needed. Model access depends on the account; verify a complete tool turn in
Lab 1 before starting a build.

The tested CLI versions are Claude Code **2.1.278**, Codex **0.155.1**, and
Kiro CLI **2.22.1**, declared in `coding-agents/cli-versions.json`. Host and
container installation use that manifest, verify the installed versions, and
disable automatic updates. Change a pin deliberately and repeat the Runtime
tool, shared-file, and telemetry checks before releasing it.

Runtime V2 prepares a snapshot during deployment. The deployers wait for the
expected Runtime revision to report both `READY` and `platformVersion=V2`;
preparation can take several minutes. They stop on failure or a bounded deadline.
The platform version is separate from the numbered configuration revisions.
The coordinator keeps its generated CDK project, then uses the SDK to select V2
because CloudFormation does not expose that setting yet.

The game preset leaves its concept, appearance, controls, and progression to the
builder. Teams share a small score interface: `GET /api/scores` returns saved
`player`/`score` entries, `POST /api/scores` saves an earned result, and workshop
scores run from 0 to 1000. Each game explains its own mapping to that scale.
To add a creative direction in Chat, send
`Use preset=game-from-scratch. Creative direction: <your own idea>`.
The reporter sends the saved score unchanged; the shared board does not make
different games equally difficult.

Each builder works in a named linked Git worktree and separate pull request. The
worktree is local to the coordinator or Runtime; only one normalized source archive
crosses the Runtime boundary. The validator writes an executable for that request,
and the orchestrator runs it. One independent,
read-only review then applies two required lenses to each pull request on its own:
adversarial verification and design/integration. The reviewer never sees a
builder's conversation or edits a builder's code.

This repo is the full code payload. Clone it and follow the workshop content.
Labs 1 and 2 use the prepared host terminal. After playing the generated game,
Lab 3 uses Chat to request a focused improvement, then Development, Agents, and
Governance to follow its identity and usage. Participants review the PR and play
the merged fix. The underlying commands and query remain inspectable.

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

In Lab 2, create a separate **private GitHub repository** for the app your agents
will build. Choose **No template** and turn **Add README** on so it has an initial
commit and default branch. This platform repository stays in the workshop's VS Code
checkout. The coordinator opens one role pull request per builder against the app
repository's default branch, and each pull request is checked and reviewed
on its own. Under the default `human_review` policy, an approved PR stays open
for a person to merge. There is no combined candidate, merge queue, or separate final
pull request. The validator's executable must pass for each pull request, run
against the default branch as it stands plus that diff. The GitHub App authors every
pull request.

A single builder receives the original request and chooses its design. Several
builders begin independently from a shared interface plan. Because each pull request is
checked and reviewed against the default branch AS IT STANDS, once an earlier role's
pull request merges the next role's check runs against a tree that already contains
it. When that merge moves a path a still-open pull request also changed, its owner
gets one bounded refresh. This catches cases where separate branches each worked but
did not agree with each other. It is separate from the one repair allowed after a
failed check or review finding.

A repair re-runs the original executable. Its SHA-256 is recorded with each gate
result and on the pull request, so reviewers can compare the evidence across
rounds. Only a change to the default branch's source snapshot invalidates that
check. If triage finds a fault in the check itself, the run stops for a person.
For example, a document parser can miss a valid example because it pairs code
fences incorrectly. Checker steering requires positive and negative parser
controls; a checking failure must not be reported as missing application behavior.
An operator correction is separate evidence, with its own check hash and review.
The full executable is saved under its digest, locally and in the workshop bucket
when available. The PR comment records the private S3 URI after that write succeeds,
so the original check can be retrieved after its Runtime session ends.

The executable runs in its own local Git checkout. `workshop-base` records the
current default-branch snapshot and `HEAD` records the candidate source. These
are local snapshot commits, not GitHub commit IDs. Git commands issued by the
check therefore inspect the deliverable rather than the surrounding platform
checkout.

Lab 2 deliberately keeps Git metadata off S3 Files. The deployed coordinator uses
`/tmp/workshop-runs`, each coding-agent Runtime creates its turn's worktree under
`/tmp`, and `.git` never enters the exchange archive. S3 Files remains the shared
workspace for direct shell work in Lab 1. The host, deployed coordinator, and
run-state reader use the bucket recorded in `coding-agents/infra.config`, including
stack-specific bucket names; `WORKSHOP_RUNTIME_BUCKET` remains an explicit override.

## Layout

- `coding-agents/` the three coding-agent harnesses (container + setup.sh + deploy.py + connect.py) and shared infra/gateway
  - `claude-code/` backend builder (Claude Code, native Bedrock, Opus 5/high)
  - `codex/` frontend builder (Codex 0.155.1, Bedrock Runtime Responses, Sol/medium)
  - `kiro/` acceptance-contract validator (Kiro CLI; steered by `.kiro/steering/*.md` with `inclusion: always`, which directs it to author an executable check whose exit code is the gate; authenticates with your own `ksk_` key, fetched from the AgentCore Identity Token Vault at session start)
  - `opencode/` alternate frontend (hidden; restore with `WORKSHOP_ROLES=claude-code,opencode,kiro`)
  - `claude-code-validator/` alternate checker (hidden; restore with `WORKSHOP_ROLES=claude-code,codex,claude-code-validator`): the same acceptance-check-authoring contract in a `CLAUDE.md`, Bedrock-native with no key, for an account without a Kiro subscription
- `orchestrator/` the Strands orchestrator engine (routing, engine, executor, reviewer, github)
  - `orchestrator/roles.py` declares the served roster (`WORKSHOP_ROLES`-configurable); this is the single place role ids, kinds (builder/checker), and capabilities (backend/frontend/validator) live
- `orchestrator-agent/` the deployable Strands agent bundle
- `console/` the React + FastAPI console (Development / Agents / Chat / Governance / Settings)
- `interactive-api/` `metrics-api/` the Stage 1 interactive + Stage 3 metrics engines
- `harness-skills/` agent skills used to configure the harnesses
- `e2e/` the end-to-end workshop journey + integration suite

## Tests

The full suite is collected from this repo root:

```bash
WORKSHOP_SKIP_LIVE=1 WORKSHOP_E2E_LIVE=0 python3 -m pytest -q
npm --prefix console/web run typecheck
npm --prefix console/web test
npm --prefix console/web run build
```

`pytest.ini` declares the `testpaths`; the root `conftest.py` isolates GitHub and
Runtime credentials so no test can read a token or open a pull request.
The offline suite verifies platform behavior, not a future agent-generated
application. A fresh event run still needs its real executable and review
evidence. Known console submitters are mapped to telemetry by default. Lab 3
verifies that mapping and the exported usage events from Claude Code and Codex,
shown separately for each user. CLI requests without user metadata and previously
unlabeled events remain unattributed. Codex's input total includes cache tokens;
the API normalizes both CLI formats without counting that cache twice. Missing
measurements remain unavailable. Kiro credits and coordinator SDK usage are
outside this CloudWatch query.

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

## Gotchas worth knowing before you debug

Each of these cost real time on a live run, and each has a cheap tell.

- **The opencode restore path needs a pseudo-terminal.** Version 1.17.20's default formatter blocks when
  stdout is not a tty, so a non-PTY invocation hangs with no output, no error and almost
  no CPU, its debug log stopping right after `init`. Its supported paths run it in a
  PTY (`agentcore exec --it`, and a Runtime PTY per dispatched turn). Preserve
  that PTY when restoring it.
- **A closed shell is not a successful command.** Read the Runtime's termination
  status, including a non-zero `ExitCode` cause. A connection that closes without
  a command result reports an execution error.
- **Browser input needs browser evidence.** A passing service check does not prove
  that global keyboard shortcuts leave form fields usable, or that repeated
  Enter sends only one request. Exercise the actual interaction and inspect
  the complete event-handler path before approving a UI change.
- **Enabling Claude Code telemetry is not exporting it.** A dispatched run gets seven
  variables from `_CLAUDE_TELEMETRY` in `orchestrator/roles.py`. With only
  `CLAUDE_CODE_ENABLE_TELEMETRY=1` the CLI collects and sends nowhere, and Logs Insights
  stays empty. Check the sidecar too: inside the Runtime,
  `curl -fsS http://127.0.0.1:13133` answers `"status":"Server available"`.
- **Only stage steering a role actually reads.** Every mounted role can read
  `/mnt/s3files`. In a hand-opened shell, Claude Code stays in `$HOME` and reads
  its baked `CLAUDE.md`; Codex uses `/mnt/s3files` when the mount is attached.
  Kiro copies only `.kiro/steering/validator.md` from the mount into its private
  `$HOME` and starts there, keeping the frontend's `AGENTS.md` out of its context.
  Kiro uses an explicit `WORKSHOP_AGENT_WORKDIR` when one is provided for dispatched work.
- **A session id must be at least 33 characters**, which is why every example generates a
  UUID. And `.bashrc` exports reach interactive shells only, so scripted runs must pass
  the environment explicitly.
- **Trust the pull request, not the chat turn.** The coordinator's prose is a model
  summarising state and can overstate; `role_prs`, `gate_history`, `review_rounds`,
  `fail_reason` and `next_action` are what the engine recorded.
- **You cannot create a GitHub App from an API.** `coding-agents/gateway_mcp/create-github-app.py`
  automates everything around it (manifest, redirect, conversion, installation id
  discovery), but a human still presses **Create GitHub App** and installs it. Adding a
  new repository to an App installation you own also needs a browser: a user token gets
  `403 You do not have permission to modify this app`.

## License

MIT-0. See `LICENSE`.
