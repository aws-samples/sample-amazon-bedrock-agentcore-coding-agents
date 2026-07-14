# Target spec: compose first, deploy the graded artifact next

## Stage 2 task

> Convert the AWS sizing and pricing module into an MCP server artifact plus a thin
> chatbot UI. Every tool in `cost_analyzer.TOOL_SPECS` must be exposed. Done means
> `usecase-sample-to-mcp/grading/` is green against the booted artifact and both files
> are composed into one PR. Stage 3 deploys that graded server to AgentCore Runtime
> and adds it to Gateway.

| Agent | Deliverable | Acceptance |
|---|---|---|
| Claude Code | `deliverable/mcp_server.py` | five tools, correct values, invalid input rejected |
| Claude Code validator | validation report over the deterministic contract | pytest remains the pass or fail floor |
| opencode | `deliverable/chatbot.html` | thin `fetch` plus `tools/call`, no pricing logic |

The roles produce different parts of one change set. They are composed, not raced.
The reviewer boots `mcp_server.py`, sets `MCP_ENDPOINT_URL` to that process, and runs
the same ten tests before finalization opens the PR through the GitHub MCP Gateway.

## Optional promotion adapter

`usecase-sample-to-mcp/deploy/deploy.sh <pr-clone>/deliverable` packages the graded
server with a FastMCP transport adapter, deploys it to AgentCore Runtime, and registers
the Runtime as the `CostAnalyzerMCP` Gateway target. A namespaced Gateway tool call must
return `140.16` for two `m5.large` instances.

The PR author is the GitHub App installation. The authenticated console submitter
is recorded separately for audit and per-user cost. Neither fact is mislabeled as
OAuth OBO exchange.
