---
name: backend-engineering
description: >-
  Build a well-structured backend service for ANY task: an API, a tool server,
  a data endpoint, or an MCP server that exposes a module's capabilities over the
  wire. Use when implementing the server side of a build. Principles the agent
  applies to whatever it is asked to build, not a fixed file list. Covers the
  wrap-do-not-reimplement rule, contract fidelity, input validation, errors,
  runnability, and honest self-verification.
metadata:
  author: AgentCore Coding Agents Workshop
  version: "1.0.0"
license: MIT-0
---

# Backend engineering

You are building the server side of a task. This is a harness, not a template:
apply these principles to whatever the request is. Decide the files, the
language, the framework, and the shape yourself from the task. Nothing here names
a file you must create.

## The one rule that outranks the rest: wrap, do not reimplement

When the task is to expose existing logic (a module, a library, a dataset) over
an interface, your job is to bridge to it, not to copy it.

- Import and call the source of truth live. Never paste its data, its formulas,
  or its rules into your server. A copied constant drifts from the original the
  first time the original changes; the point of the service is that it cannot.
- Preserve the source's public contract exactly: the names, the input shapes, and
  the returned structures it already defines. Do not rename, reshape, or "improve"
  them. Downstream consumers and any acceptance test are written against the
  original contract.
- Resolve where the source lives portably (an env var, a path argument, a
  discoverable import root), so the same server runs on the build host, in the
  runtime, and in a reviewer's fresh clone.

## Contract fidelity

- Expose exactly the capability set the task defines: no fewer (a missing tool
  fails discovery), no extra surface invented on a whim.
- Match the wire protocol the task names precisely. If it is JSON-RPC, honor its
  request/response/error shape; if it is REST, honor its methods and status
  codes. Read the protocol's own spec rather than guessing its envelope.
- Round-trip fidelity: what a caller sends maps to the source call, and what the
  source returns maps back to the caller unchanged in meaning.

## Input validation and errors

- Validate at the boundary. Reject unknown names, wrong types, and out-of-range
  values with a clear, typed error. Never silently coerce bad input into a
  plausible-but-wrong answer, that is worse than an error because it looks right.
- Map failure kinds to the protocol's own error codes (unknown method vs. bad
  arguments are different errors). Do not collapse everything to a 500 or a
  generic message.
- Fail loud and specific: the caller should learn what was wrong, not just that
  something was.

## Build it at the size the task actually is

The gate only asks "does it do what was asked?", so the cheapest thing that passes
is a real temptation. Resist it. A reviewer reads this as production work.

- **Match the scope to the request.** A request naming several features, real
  persistence, and validation is a project, not a script. Do not collapse it into
  one file because one file can be made to pass.
- **Structure it as you would at work.** Separate the concerns the task actually
  has (routing, domain logic, storage, validation) into their own modules with
  real names. A single 100-line file holding all of them is a prototype, and
  saying "keep it minimal" to yourself is not a design decision.
- **Use a real framework when the task is a real service.** A production HTTP
  service in Python is FastAPI or Flask, not a hand-rolled
  `BaseHTTPRequestHandler`; in Node it is Express or Fastify, not raw `http`.
  Hand-rolling the protocol layer is how you end up re-implementing routing,
  parsing, and status codes badly. Declare your dependencies in the manifest the
  ecosystem expects (`requirements.txt`, `package.json`) so anyone can install and
  run it.
- Reach for the standard library for genuinely small helpers, not to avoid the
  framework a real service would use.
- **Persistence means persistence.** If the task says data must survive a restart,
  an in-memory dict is a failed requirement even if the gate's checks happen to
  pass in one process.

## Runnable and self-contained

- The service must actually start and serve, from a clean checkout, with an
  obvious entry point and a way to choose its port/address.
- **Your start command must STAY IN THE FOREGROUND and keep serving until it is
  killed.** Whatever checks your work will launch that command and then poll the
  service, so a command that forks, backgrounds itself, or returns once the server
  is "up" looks identical to a server that died: the poll finds nothing. Do not end
  the start path with `&`, do not `nohup`/disown it, and do not exit after printing
  a ready message. If your framework's runner can detach, use its foreground mode.
- Honour the port you are given (an env var if one is set, otherwise your
  documented default) rather than always binding a hardcoded one. Something that
  probes your service will choose a free port to avoid collisions; if you ignore it
  and bind your own, the probe polls an address nothing is listening on.
- **Start fast: installing dependencies is setup, not startup.** Your workspace is a
  network file mount, where a cold virtualenv plus dependency install measures around
  47 seconds (7 on local disk). If your start command does that install first, every
  second of it is spent before your service answers anything, and a checker polling
  for readiness sees a service that never came up. Keep install in a separate setup
  step, or make it a no-op when the dependencies are already present, so a second
  start is immediate.
- Bind to loopback by default for a local/dev server; do not expose it wider than
  the task needs.

## Callable from a browser

If anything with a UI will call your service, a browser is a client with rules of its
own, and ignoring them means the call fails before your code ever runs.

- A JSON request from a page on another origin is **preflighted**: the browser sends
  `OPTIONS` first. Answer it, and send the matching allow-origin, allow-methods, and
  allow-headers on the real response too. A service that only handles `GET` and `POST`
  looks fine from `curl` and is unreachable from a page.
- Keep the allowance as narrow as the task allows, and be deliberate about it rather
  than discovering it from a blocked console message later.
- Preflight is a browser concern only: your own `curl` check passing proves nothing
  about it, so verify with a real cross-origin call or state the constraint in your
  handoff.

## Prove it runs (self-verification)

Do not hand off a server you have only read. Before you are done, exercise it the
way a caller will: start it, hit its discovery and one real call over the wire,
and confirm it answers the contract, not just that the process launched. The
separate validator will verify independently; your own check is so you do not
hand off something obviously broken. When you can, leave that proof behind in a
form a reviewer can re-run, but let the task shape what that proof looks like,
do not force a fixed filename or harness.

## Do only your side

You own the server. You do not write the UI, and you do not decide the final
pass/fail verdict, a separate validator owns acceptance. Keep the seam clean: a
stable contract is what lets the frontend and the validator work in parallel with
you.

## Verify your own work before you hand it off

- The server starts from a clean checkout and serves on a chosen port. Prove it the
  way a checker will: run your start command, and from ANOTHER shell call the
  service. If your command returned control before you could make that call, it
  backgrounded itself and it will read as a dead service.
- It imports the source of truth live; grep your own output for a copied constant
  or a duplicated formula and remove it.
- Discovery lists exactly the intended capabilities; one real call returns the
  source's value unchanged.
- Bad input is rejected with the right typed error, not a wrong answer.

The measure of the deliverable is not that a specific file exists; it is that the
service starts, answers its contract over the wire, and never contradicts the
source of truth it wraps.
