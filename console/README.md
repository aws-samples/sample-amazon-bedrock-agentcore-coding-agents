# Workshop console

The console is a convenience UI over the same repository and AWS APIs attendees
use from the CLI. It does not introduce a second implementation.

- **Agents** opens command shells on deployed coding-agent Runtimes.
- **Fleets** chats with the coordinator and streams routed runs.
- **Governance** reads the run ledger through the metrics API.
- **Settings** wires Runtime ARNs, the Kiro credential provider, and GitHub.

The frontend is React, Vite, and Tailwind. FastAPI serves the APIs and the built
SPA from one origin.

## Development

Install dependencies once:

```bash
python3 -m pip install -r console/requirements.txt
npm --prefix console/web install
```

Run the frontend and backend in separate terminals. The backend command watches
all sibling engines, not only `console/`:

```bash
npm --prefix console/web run dev
```

```bash
cd console
CONSOLE_DEV=1 CONSOLE_PORT=8080 python3 -m uvicorn server:app \
  --host 0.0.0.0 --port 8080 --reload \
  --reload-dir . \
  --reload-dir ../interactive-api \
  --reload-dir ../orchestrator \
  --reload-dir ../metrics-api \
  --timeout-graceful-shutdown 5
```

Open `http://localhost:5174` or `http://localhost:8080`. For a static production
build, run `npm --prefix console/web run build`, then `python3 console/server.py`.

## Runtime behavior

The shipped path is real or fail-loud:

- Agent shells call `InvokeAgentRuntimeCommandShell` for a wired Runtime ARN.
- Coordinator dispatch calls wired Runtime ARNs or an explicitly configured
  local `agentcore dev` URI. An unwired role is an error.
- A PR URL is returned only after GitHub accepted the PR. The author is the PAT
  owner or GitHub App installation used for that call.
- Cognito or local user data in the ledger identifies who submitted the run. It
  supports audit and cost attribution, but is not proof of OAuth OBO delegation.

See [the coordinator contract](../orchestrator/API_CONTRACT.md) and
[the metrics contract](../metrics-api/API_CONTRACT.md) for wire shapes.
