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

## Runnable and self-contained

- The service must actually start and serve, from a clean checkout, with an
  obvious entry point and a way to choose its port/address.
- Prefer the standard library and what the environment already has; add a
  dependency only when it earns its weight, and if you do, make installing it
  obvious.
- Bind to loopback by default for a local/dev server; do not expose it wider than
  the task needs.

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

- The server starts from a clean checkout and serves on a chosen port.
- It imports the source of truth live; grep your own output for a copied constant
  or a duplicated formula and remove it.
- Discovery lists exactly the intended capabilities; one real call returns the
  source's value unchanged.
- Bad input is rejected with the right typed error, not a wrong answer.

The measure of the deliverable is not that a specific file exists; it is that the
service starts, answers its contract over the wire, and never contradicts the
source of truth it wraps.
