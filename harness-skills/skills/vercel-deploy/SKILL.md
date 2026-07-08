---
name: vercel-deploy
description: >-
  Ship the Codex-built chatbot UI (the FRONTEND BUILDER's artifact in our 3-agent
  harness) to Vercel and wire it to the AgentCore Gateway MCP endpoint. Use when the
  user says "deploy the frontend", "deploy to Vercel", "ship the chatbot UI",
  "publish the UI", "put the chatbot online", "give me a live URL for the UI",
  "promote the frontend to production", or "vercel deploy". This is the deploy half
  of the FRONTEND role; pairs with configure-codex-frontend (which builds the UI).
  Role mapping in this harness is LOCKED: Claude Code = BACKEND (AgentCore MCP
  server), Kiro = VALIDATOR (pytest acceptance gate), Codex = FRONTEND BUILDER
  (this chatbot UI). The UI is a thin MCP client: it calls tools/list + tools/call
  through the Gateway and NEVER holds AWS credentials.
---

# vercel-deploy: ship the Codex chatbot UI to Vercel

You are deploying the **FRONTEND** artifact of the 3-agent harness. Codex (FRONTEND
BUILDER) produced a chatbot UI that talks to the AgentCore Gateway MCP endpoint from
Stage 2/3. Your job is to put that UI on a public URL, point it at the Gateway, and
keep all secrets out of the bundle. This is autonomous, fire-and-forget plumbing: no
race, no winner, just get the UI live and verified.

Hard invariants (do not violate):
- The UI is an **MCP client only**. It calls `tools/list` + `tools/call` through the
  AgentCore Gateway URL. It holds **NO AWS credentials** and signs nothing.
- Anything secret (Gateway auth tokens, API keys) lives in **Vercel env vars /
  Secrets**, never in the client bundle. `NEXT_PUBLIC_*` / `VITE_*` vars are shipped
  to the browser; only the Gateway **URL** (already public-facing) goes there.
- This pairs with `configure-codex-frontend`. If the UI doesn't exist yet, run that
  skill first; this skill only deploys.

---

## Step 1: Gather inputs (AskUserQuestion)

Before touching the CLI, confirm these. Ask the user with AskUserQuestion if any are
unknown; do not guess a Gateway URL.

1. **MCP endpoint URL**: the AgentCore Gateway URL from Stage 2/3. This is what the UI
   calls. Get it from the gateway deploy state if available:
   ```bash
   # from the reference harness Stage 2 gateway deploy
   GATEWAY_URL=$(jq -r '.gateway_url' gateway_mcp/.deployed-state.json)
   echo "$GATEWAY_URL"
   ```
   For the workshop, the "MCP endpoint" the UI points at is this deployed Gateway URL
   (or the deployed MCP server URL when running Gateway-less).
2. **Frontend framework**: Next.js (env prefix `NEXT_PUBLIC_`) or Vite/React
   (env prefix `VITE_`). Detect it instead of asking when possible:
   ```bash
   jq -r '.dependencies.next // .devDependencies.vite // "unknown"' frontend/package.json
   ```
3. **Target environment**: preview only, or promote to production now?
4. **Vercel auth**: interactive `vercel login`, or a `VERCEL_TOKEN` (CI / headless)?

---

## Step 2: Prerequisites

```bash
# Node 18+ (Vercel CLI requirement) and npm
node --version
npm --version

# Install the Vercel CLI globally
npm i -g vercel
vercel --version

# Authenticate (pick one)
vercel login                      # interactive browser/email login
# --- OR, headless / CI ---
export VERCEL_TOKEN="<your-vercel-token>"   # create at vercel.com/account/tokens
```

Run from the chatbot UI directory Codex produced (e.g. the frontend project root that
contains `package.json`). Link it to a Vercel project once:

```bash
vercel link                       # interactive: pick scope + project, writes .vercel/
# --- OR non-interactive ---
vercel link --yes --token "$VERCEL_TOKEN"
```

---

## Step 3: Set the MCP endpoint as a build-time env var

The UI reads the Gateway URL from an env var at build time. Pick the var name that
matches the framework (Step 1). Add it to all three Vercel environments so preview and
prod both resolve it.

```bash
# Next.js UI: var must be NEXT_PUBLIC_* to reach the browser
printf '%s' "$GATEWAY_URL" | vercel env add NEXT_PUBLIC_MCP_ENDPOINT_URL production
printf '%s' "$GATEWAY_URL" | vercel env add NEXT_PUBLIC_MCP_ENDPOINT_URL preview
printf '%s' "$GATEWAY_URL" | vercel env add NEXT_PUBLIC_MCP_ENDPOINT_URL development

# --- OR Vite/React UI: var must be VITE_* ---
printf '%s' "$GATEWAY_URL" | vercel env add VITE_MCP_ENDPOINT_URL production
printf '%s' "$GATEWAY_URL" | vercel env add VITE_MCP_ENDPOINT_URL preview
printf '%s' "$GATEWAY_URL" | vercel env add VITE_MCP_ENDPOINT_URL development
```

Confirm what got set (values for `NEXT_PUBLIC_*` / `VITE_*` are visible by design:
they ship to the browser; that's fine for a public Gateway URL):

```bash
vercel env ls
```

Security check before continuing:
- The ONLY thing exposed to the browser is the Gateway **URL**. Good.
- If the Gateway requires a bearer/JWT, that token is a **secret**: do NOT prefix it
  with `NEXT_PUBLIC_`/`VITE_`. Keep it as a plain server-side env var and have the UI's
  server route / API handler attach it, so it never lands in the client bundle:
  ```bash
  printf '%s' "$GATEWAY_AUTH_TOKEN" | vercel env add MCP_GATEWAY_TOKEN production
  ```
- Never write AWS access keys, GitHub App private keys, or the Token Vault key
  (`ksk_*` / `sk-*`) into the UI or its env. The UI talks to the Gateway only.

---

## Step 4: Local check (`vercel dev`)

Run the UI locally with Vercel's runtime so env injection matches production behavior.

```bash
vercel dev                        # serves on http://localhost:3000 by default
```

Smoke test the wiring: open the UI, send a chat message, and confirm it reaches the
Gateway. Independently verify the endpoint itself answers `tools/list` (this is exactly
what the UI does under the hood, IAM-signed in our harness):

```bash
awscurl --service bedrock-agentcore --region us-west-2 -X POST "$GATEWAY_URL" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/list","id":1,"params":{}}'
```

If `tools/list` returns the tool set, the backend half (Claude Code's MCP server,
fronted by the Gateway) is healthy and the UI has something to call.

---

## Step 5: Deploy a preview

```bash
vercel                            # builds + deploys, prints a unique preview URL
# --- OR headless ---
vercel --token "$VERCEL_TOKEN" --yes
```

Open the printed preview URL and run the same smoke test as Step 4 against the live
deployment. The preview is disposable and per-deploy, ideal for letting the VALIDATOR
(Kiro) or a human eyeball the UI before promoting.

---

## Step 6: Promote to production

```bash
vercel --prod                     # builds + deploys to the production alias
# --- OR headless ---
vercel --prod --token "$VERCEL_TOKEN" --yes
```

Capture the production URL it prints; that's the deliverable for the FRONTEND role.

---

## Step 7: Verify the wired UI end-to-end

Confirm the live UI is actually driving the Gateway, not just rendering:
1. Load the production URL, send a message that triggers a tool (e.g. ask it to list
   the repo's open issues), and confirm a tool result comes back; that proves a full
   `tools/list` → `tools/call` round-trip through the Gateway.
2. Re-check `tools/call` directly if the UI errors, to isolate UI vs. Gateway:
   ```bash
   awscurl --service bedrock-agentcore --region us-west-2 -X POST "$GATEWAY_URL" \
     -H "Content-Type: application/json" \
     -d '{"jsonrpc":"2.0","method":"tools/call","id":2,"params":{"name":"<tool>","arguments":{}}}'
   ```
3. Open the browser devtools Network tab on the live UI and confirm no AWS credentials,
   no `ksk_*`/`sk-*` keys, and no GitHub App key appear in any request or in the JS
   bundle. Only the Gateway URL (and, if used, a token attached server-side) should be
   present. This is the **security-by-default** invariant: the agent/UI can't bypass
   platform invariants because it never holds the credentials in the first place.

---

## How this fits the harness (autonomous, no race)

- **BACKEND (Claude Code)** deploys the AgentCore MCP server; the **Gateway** fronts it
  with IAM/JWT auth and tool routing. That Gateway URL is the contract this UI consumes.
- **VALIDATOR (Kiro)** runs the acceptance gate against the same endpoint; the UI and
  the gate point at one source of truth:
  ```bash
  MCP_ENDPOINT_URL="$GATEWAY_URL" pytest usecase-sample-to-mcp/grading/
  ```
- **FRONTEND BUILDER (Codex)** built this chatbot UI; this skill ships it. Pairs with
  `configure-codex-frontend`.
- This is finalization plumbing, not a contest: there is no winner and no
  fastest/cheapest ranking. Submit, deploy, verify, walk away.

## Extensibility notes

- The endpoint is injected purely via env var, so the same UI bundle can be re-pointed
  at any AgentCore Gateway (dev/preview/prod, or a different repo's Gateway) without a
  code change; this is the **extensibility / flexibility** principle: swap the backend
  behind a stable interface (the MCP `tools/list` + `tools/call` contract) without
  touching the frontend.
- Hosting is likewise swappable. Vercel is the default here; the same env-var wiring
  works on any static/SSR host. The MCP contract is the seam.
- Cost note (illustrative): static/preview hosting like this is a rounding error next
  to Bedrock inference + agent compute; infrastructure is a small fraction of total
  cost and inference dominates. Keep the expensive surface (the agents) governed; the
  UI host is cheap.
