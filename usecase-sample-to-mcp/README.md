# usecase-sample-to-mcp/: the workshop use case

The module, grading contract, Runtime transport adapter, and deploy script for the
workshop's primary conversion task.

## The task
Take an existing Python module (a sizing/pricing calculator, `cost_analyzer.py`, with plain
Python functions), compose an MCP server plus chatbot UI in one PR, then deploy the graded
server to AgentCore Runtime behind Gateway.

Why relatable: every team has utility code that *should* be an MCP server but nobody does
the tedious conversion; our own samples broke twice from CLI changes; implementation,
validation, and UX are different jobs (justifies multi-agent).

## Role-differentiated build (resolves Raj's "why 3 agents?" pushback)
Roles are LOCKED (see AGENTS.md):
- **Claude Code:** backend; wrap the functions as an MCP server artifact.
- **Kiro:** validator; author the unit + E2E tests that become the acceptance gate.
- **Codex:** frontend builder; a thin chatbot UI that delegates through `tools/call`.

## What lives here
1. `cost_analyzer.py`: the sample module, 5 pure functions exposed through a tool registry
   (`TOOL_SPECS` / `list_tools()` / `dispatch()`). Costs are illustrative fixtures, not
   live AWS pricing.
2. `grading/`: the **deterministic pytest acceptance gate** the orchestrator runs in its
   finalization phase (never an LLM judge). Three checks: `tool_discovery`,
   `tool_correctness`, `input_validation`. The contract is written against an `MCPClient`
   protocol so the IDENTICAL tests run two ways:
   - in-process: `pytest usecase-sample-to-mcp/grading/`
   - over the wire: `MCP_ENDPOINT_URL=<endpoint> pytest usecase-sample-to-mcp/grading/`
   `adapters.py` ships both adapters, including a working JSON-RPC `RemoteMCPClient`
   (SigV4-signed when `MCP_SIGV4=1`, for the IAM-authenticated Gateway path).
3. `reference-server/mcp_server.py`: the **reference conversion**, all 5 tools served
   over MCP's JSON-RPC wire shape, stdlib only. This is the backend role's deliverable as
   working code; Stage 1 attendees compare their by-hand conversion against it, and the
   orchestrator engine boots it as the live artifact the gate grades.
4. `deploy/`: a FastMCP transport adapter plus an idempotent Runtime and Gateway-target deploy.
5. `target-spec.md`: the natural-language task the orchestrator dispatches.

Quick proof it all hangs together (no AWS, no pip install):
```bash
python3 usecase-sample-to-mcp/reference-server/mcp_server.py --port 9000 &
MCP_ENDPOINT_URL=http://127.0.0.1:9000 pytest usecase-sample-to-mcp/grading/   # 10 passed
```

The Stage 2 engine grades the booted PR artifact. Stage 3 runs `deploy/deploy.sh` against that
PR directory, then calls the namespaced tools through Gateway.
