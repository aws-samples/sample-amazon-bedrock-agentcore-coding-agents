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
- **Start fast: installing dependencies is setup, not startup.** A cold virtual
  environment or package install can consume most of the checker's readiness
  budget before your service answers. Keep install in a separate setup step, or
  make it a no-op when the dependencies are already present, so a second start is
  immediate.
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

## Games and other interactive pages you serve yourself

When you are the only builder and the request is something a person plays or clicks in
a browser, the page is yours too, and ONE service serves both the page and its API.
These standards come from where the result actually runs: the workshop opens it through
a reverse proxy at a path prefix (`https://<host>/proxy/<port>/`), on a host with no
guaranteed route to the public internet.

- **Every URL the page uses is relative.** Scripts, assets, and API calls resolve
  against the page's own location (a path like `scores` or `./assets/...`), never a
  root-absolute `/scores` and never a hardcoded host or port. The proxy strips the
  prefix on the way in, so a root-absolute URL leaves the proxy entirely: under a
  prefix, a page with one absolute URL loads and then silently does nothing.
- **Serve everything yourself.** No CDN, web font, or third-party script. The host may
  not reach them, and a page that depends on one fails as a blank canvas.
- **It has to feel like a game.** Render on a canvas with `requestAnimationFrame`, not
  DOM elements moved by timers; respond to the keyboard without lag; show the controls
  on screen; give it a start state, a running score, a game-over state with the final
  score, a way to enter a name and submit it, and a restart.
- **The high-score table is real, and it has a conventional address.** It is shown on
  the page, persisted so it survives a restart of the service, and read and written
  through your API at a `scores` route relative to the page. `GET` returns a JSON array
  of rows, best first, each with an integer `score` and a player-name field (name it as
  the game wants: `player`, `name`, and `initials` are all read by tools downstream);
  `POST` accepts a JSON body with a name and a score. It is defended: an empty or
  missing name, a name longer than any person would type, and a missing, non-integer,
  negative, or absurd score are refused with a clear error and a correct status code.
- **Start it the documented way, on the port you are given.** `PORT` (or your
  documented default) chooses the port, and the start command and how to play are in
  the documentation, not only in your head. Write that documentation as a file in the
  tree (a README is the obvious choice), because the next reader is a person or another
  agent who has your code and nothing else: the checker has to work out how to start
  this from what you wrote, and at the end of the workshop a human clones the merged
  branch and asks an agent to run it. A deliverable nobody can start is not done.

## Prove it runs (self-verification)

Do not hand off a server you have only read. Before you are done, exercise it the
way a caller will: start it, hit its discovery and one real call over the wire,
and confirm it answers the contract, not just that the process launched. The
separate validator will verify independently; your own check is so you do not
hand off something obviously broken. When you can, leave that proof behind in a
form a reviewer can re-run, but let the task shape what that proof looks like,
do not force a fixed filename or harness.

## Do only your side

You own the server. When a frontend role is on the team you do not write the UI;
when you are the only builder and the request includes a page, that page is yours
(see above). Either way you do not decide the final pass/fail verdict, a separate
validator owns acceptance. Keep the seam clean: a stable contract is what lets the
frontend and the validator work in parallel with you.

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
