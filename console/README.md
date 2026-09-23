# Agent Studio

The React console is the main working surface for Lab 3 and an optional view of
host-dispatched builds. FastAPI serves the APIs and production SPA from one
origin.

| Area | What it does |
|---|---|
| Development | Opens a host workspace, editor, and terminal |
| Agents | Opens real interactive PTYs on configured coding-agent Runtimes |
| Chat | Runs the coordinator in the host process and displays its evidence |
| Governance > Usage | Queries exported CloudWatch request and token usage |
| Governance > Controls | Inspects identity, approval settings, and limits; previews policy decisions |
| Governance > Activity | Shows the host audit trail and manages recorded Runtime sessions |
| Settings | Configures Runtime targets, Kiro credentials, GitHub, and merge policy |

The host console and deployed coordinator use the same engine code but separate
run registries. A CLI build in the deployed coordinator does not appear here
just because the console knows its ARN. Do not submit another build to fill a
screenshot or an empty run list.

The sidebar groups Development, Agents, and Chat under **Workspace** and the
three governance pages under **Governance**. Settings is in the top navigation.
Agents has one destination with tabs derived from the served roster. Its
**Manage sessions** action opens Activity's Sessions tab.

## Development

From the repository root, install dependencies:

```bash
python3 -m pip install -r console/requirements.txt
npm --prefix console/web ci
```

Run the frontend and backend in separate terminals:

```bash
npm --prefix console/web run dev
```

```bash
cd console
CONSOLE_DEV=1 CONSOLE_PORT=8080 python3 -m uvicorn server:app \
  --host 0.0.0.0 --port 8080 --reload \
  --reload-dir . --reload-dir ../interactive-api \
  --reload-dir ../orchestrator --reload-dir ../metrics-api \
  --timeout-graceful-shutdown 5
```

Open `http://localhost:5174` or `http://localhost:8080`. For production, run
`npm --prefix console/web run build`, then `python3 console/server.py`.
The event's `stage2-console` service uses the built frontend.

Set `WORKSHOP_REVIEW_MODEL` before starting the console to select the integrated
PR reviewer independently from the backend's `WORKSHOP_CLAUDE_MODEL`.
For the event service, supply both values in the `stage2-console` unit environment;
terminal exports do not update a running service. The console's chat model picker
selects the coordinator's conversation model and does not change the reviewer.

```bash
npm --prefix console/web run typecheck
npm --prefix console/web test
npm --prefix console/web run build
```

`typecheck` builds the TypeScript project graph; invoking the solution tsconfig
without its referenced projects does not check the application.

## Lab 3: improve the game and trace its usage

After playing the Lab 2 game, submit one observed problem through Chat. The
selected builder changes the existing application, the coordinator opens its PR,
and Kiro authors its executable check. Review that evidence and the source before
merging, then repeat the original play test. Development, Controls, and Usage
provide the identity and telemetry exercise while the build runs.

Governance > Usage runs one fixed Logs Insights query over the workshop
telemetry log group. It does not read the host's run ledger or invoke an agent.
The query starts only when the attendee presses **Run query**; counts are
shown only after CloudWatch reports `Complete`. Errors, pending results, and
missing usage are never presented as zero usage. Navigating between governance
pages preserves the current query evidence.

`UserIdentity.to_otel_env()` carries known submitters by default. Chat builds and
new Agents sessions receive the server-admitted Cognito identity and the role's
complete telemetry transport settings. A shared Agents terminal keeps the
identity of the person who opened it; viewing or typing into it does not change
that identity. Open a new session after switching users.

Lab 3 inspects this mapping in Development and verifies it against exported
requests. If you customize the mapping, restart the host service from VS Code;
restarting from Development disconnects that terminal. Changes affect future
host-console sessions and dispatches, not an already deployed coordinator.

CLI requests without user metadata and historical unlabeled events remain
Untagged. A label supplied manually is not proof of Cognito authentication.
Usage separates Claude Code API request events and Codex completed responses
carrying token usage. Each CLI gets its own summary, user rows, and tagging
coverage. The agent filter narrows the table; Export JSON retains the full result.
The default table shows input including cache and output. Cache components and
reasoning details expand separately; Codex's cache is already included in its
input total, and reasoning is already included in output.

Counts describe exported usage events, not every API attempt or a complete bill.
Kiro credits and coordinator SDK calls use separate reporting paths. Copy query,
the query ID, log group, region, and UTC bounds support an independent source check.

Controls reads the same identity mapping, merge policy, role registry, and
execution limits used by this host. **Evaluate action** runs the real policy
checker on submitted data without executing the command or file operation.
Its decision links to the corresponding audit record in Activity. Raw targets
are not logged; audit records retain the decision and a SHA-256 digest.

Activity separates **Audit trail** and **Sessions** into tabs. Event details
show the result and actor first; **Raw event** exposes the recorded JSON for
inspection and export. These are host records, not an account-wide audit log.

## Runtime and evidence behavior

The console discovers the Runtime configs written by the event bootstrap and
role deploy scripts. It does not require another coordinator ARN for Chat,
because its coordinator runs in-process. Settings also discovers Kiro credential
provider metadata provisioned by the CLI or event, without reading the secret.
An unavailable metadata lookup is **unknown**, not an absent key.

Runtime targets must be wired. Missing targets fail explicitly; no fallback
agent, fabricated ARN, PR URL, or successful run is substituted. The alternate
opencode frontend requires a PTY. Console dispatch and the Agents view share the actual Runtime
terminal, so a person can observe the same work.

Terminal streams mark the first history snapshot with `replay: true`. The client
resets its screen and parses that snapshot with input disabled, so historical
terminal queries cannot send replies into the live shell. New output retains
normal terminal input and query handling. Snapshot subscription is atomic, and
Development tracks an absolute output offset even after its bounded buffer rolls.

Each builder PR has its own check and review. An approved PR remains open under
the default `human_review` policy. The console distinguishes approved, merged,
partially merged, blocked, and missing evidence. Polling errors do not erase a
previously observed run.

A Runtime stop is confirmed only by `stopped: true`. Session-local processes and
files are lost; files already saved to shared storage remain. A Runtime that is
configured but idle is still different from a stopped session.

The served GitHub App authors PRs through the broker. Cognito or local user data
records a submitter; it does not prove OAuth delegation. See the
[coordinator contract](../orchestrator/API_CONTRACT.md) and
[metrics contract](../metrics-api/API_CONTRACT.md) for the API shapes.
