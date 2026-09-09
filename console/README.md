# Workshop console

The React console is the main working surface for Lab 3 and an optional view of
host-dispatched builds. FastAPI serves the APIs and production SPA from one
origin.

| Area | What it does |
|---|---|
| Development | Opens a host workspace, editor, and terminal |
| Agents | Opens real interactive PTYs on configured coding-agent Runtimes |
| Chat | Runs the coordinator in the host process and displays its evidence |
| Governance | Separates host records from explicit CloudWatch Attribution queries |
| Settings | Configures Runtime targets, Kiro credentials, GitHub, and merge policy |

The host console and deployed coordinator use the same engine code but separate
run registries. A CLI build in the deployed coordinator does not appear here
just because the console knows its ARN. Do not submit another build to fill a
screenshot or an empty run list.

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

```bash
npm --prefix console/web run typecheck
npm --prefix console/web test
npm --prefix console/web run build
```

`typecheck` builds the TypeScript project graph; invoking the solution tsconfig
without its referenced projects does not check the application.

## Lab 3: attribution

Governance > Attribution runs one fixed Logs Insights query over the workshop
telemetry log group. It does not read the host's run ledger or invoke an agent.
The query starts only when the attendee presses **Query telemetry**; counts are
shown only after CloudWatch reports `Complete`. Errors, pending results, and
missing usage are never presented as zero usage.

Development is where the attendee implements and tests
`UserIdentity.to_otel_env()`. The method intentionally ships empty. Restart the
host service from the VS Code terminal after saving the change; restarting it
from its own Development terminal disconnects that terminal. The change affects
future host-console dispatches, not an already deployed coordinator.

A separate, manually tagged prompt in a new Agents session proves export. Its
label is not proof of Cognito authentication. The Attribution table counts
Claude Code request events; it is not Kiro usage reporting or a complete bill.

## Runtime and evidence behavior

Runtime targets must be wired. Missing targets fail explicitly; no fallback
agent, fabricated ARN, PR URL, or successful run is substituted. Keep opencode
inside a PTY. Console dispatch and the Agents view share the actual Runtime
terminal, so a person can observe the same work.

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
