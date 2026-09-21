---
name: frontend-design
description: >-
  Build clean, accessible, professional web UI for ANY task. Use when creating
  or changing a frontend: pages, components, forms, dashboards, or a chat/console
  UI. Principles the agent applies to whatever it is asked to build, not a fixed
  file list. Covers layout, type, color, states, accessibility, and the
  thin-client rule (the UI calls a backend for data and logic; it never
  reimplements them).
metadata:
  author: AgentCore Coding Agents Workshop
  version: "1.0.0"
license: MIT-0
---

# Frontend design

You are building a web UI. This is a harness, not a template: apply these
principles to whatever the task asks for. Decide the files, the framework, and
the structure yourself from the request. Nothing here names a file you must
create.

Choose a visual identity that fits the product. A workshop game can have its own
world, palette, typography, composition, and motion; console conventions below
are useful for service interfaces, not a required skin for every experience.
Screenshots are examples of another run, not reference designs to reproduce.

Distilled from the practices Vercel and design-system teams publish for
agent-built frontends. When the task points at a specific stack (React, plain
HTML, a component library), follow that stack's own conventions first and use
these as the cross-cutting bar.

## Build it at the size the task actually is

"Thin" (below) is about where LOGIC lives. It is not permission to ship less UI
than the task asks for. The gate only checks behaviour, so one inline-styled HTML
file can pass a request that deserved an application; a reviewer reads this as
production work, so build it that way.

- **Cover every feature the request names**, each reachable and usable in the UI.
  A page that exercises one endpoint of a multi-feature request is unfinished.
- **Use a real framework when the task is a real app.** A multi-view, stateful
  interface is React/Vue/Svelte with components and a build manifest
  (`package.json`), not one hand-written file with inline `onclick`. Declare your
  dependencies so anyone can install and run it.
- A single static page is the right answer only for a genuinely single-interaction
  task. Say which it is on purpose, rather than defaulting to the smaller one.
- **Separate structure, style, and behaviour** into their own files once there is
  more than a trivial amount of any of them. Inline `style="..."` on every element
  is not a design system.

## The one rule that outranks the rest: the UI is thin

The frontend renders and interacts. It does not own business logic or data.

- In a multi-role build, ownership is literal. If another role owns the backend,
  persistence, or domain logic, build against the shared contract and wait for
  integration. Do not ship a local substitute, copied service, or second full-stack
  implementation merely because your isolated checkout does not contain that
  role's files yet.
- Authoritative records, prices, service results, and model output
  MUST come from a backend call (an API, an MCP tool call, a fetch). The
  UI sends inputs and renders the structured response.
- Do not copy authoritative records, pricing, or service calculations into the
  page. Duplicating a backend calculation lets the UI disagree with the service
  it represents.
- Parse the response and render it. Show the backend's own error when a call
  fails; never invent a value to fill a gap.

If you are asked for a UI over a service, the correctness of every answer lives
on the wire, not in the markup.

A game's immediate input, animation, and gameplay state can live in the browser.
Keep its saved results consistent with the score API. This ownership boundary
does not require a server call for every frame or force every game into the
same interaction model.

### Preserve the service route and hosting path

When changing an existing application, inspect how it reaches its service and
preserve that contract. A same-origin application may be hosted beneath a path
prefix. Replacing a working relative URL with the page's origin or a leading
slash can discard that prefix and send requests to a different application.

For example, from `/proxy/8001/`, `api/scores` resolves beneath that directory;
`/api/scores` resolves at the host root. Choose the URL form that matches the
actual deployment, including any document base URL and client-side routing.
Verify both reads and writes through the address a person will open.

Do not bake in a machine-specific hostname or port. In a browser, `localhost`
and `127.0.0.1` refer to the viewer's machine. If a separate service requires
runtime configuration, use the application's existing configuration mechanism
or add the smallest one the task needs.

Add an endpoint setting or address display only when it helps the product's
user make a decision. A game does not need a service-address banner merely
because it saves scores. Show actionable failures without exposing credentials
or presenting a failed request as an empty result.

### Expect the browser's cross-origin rules

If the page and the service are on different origins, a JSON `POST` is preflighted:
the browser sends `OPTIONS` first and blocks the real call unless the service allows
it. When you own only the page, prefer same-origin so the question never arises; when
a cross-origin call is unavoidable, say so plainly in your handoff so the service side
can allow it, and surface the browser's own error instead of showing an empty result.

## Layout

- One clear primary action per view. The eye should land on it without a hunt.
- Establish hierarchy with size, weight, and space, in that order, before color.
- Consistent spacing scale (a 4px base is a safe default: 4, 8, 12, 16, 24, 32).
  Whitespace separates groups; it is not decoration.
- Content max-width for reading (~60-75ch). Full-bleed only for tables/canvases.
- Responsive by default: it must be usable at 360px wide and at 1440px. Test both.

## Typography

- A small, fixed type scale. Do not invent a new size per element. A workable
  set: page title, section title, body, small/muted label, and a mono size for
  identifiers, code, and numbers.
- One family for prose, one mono family for code/IDs/numeric columns.
- Line length and line-height are the readability levers, not font size alone.

## Color and states

- For a service interface, neutral surfaces can carry the UI while accents convey
  status and priority. For a game or other expressive product, choose colors that
  support its identity while keeping text, controls, and state easy to distinguish.
- Define semantic roles, not raw hexes scattered inline: background/foreground,
  muted, border, primary, destructive, and a success/warning/error set. Use the
  role name everywhere so a theme change is one place.
- Every interactive element needs visible hover, focus, active, and disabled
  states. A focus ring is not optional.
- Represent every async surface's full lifecycle: loading, empty, error, and
  success. An empty list and a failed fetch must look different, and neither
  should look like success.
- Protect a pending mutation against repeated keyboard or pointer activation.
  Keep the submitted values stable while it runs, allow a deliberate retry after
  failure, and reset the state when the user starts the next operation. Hiding a
  button does not itself prevent another event from submitting the same work.

## Accessibility (non-negotiable, cheap to get right)

- Semantic HTML first: `button` for actions, `a` for navigation, real labels
  tied to inputs, one `h1` then a sensible heading order.
- Keyboard reachable and operable: logical tab order, visible focus, Enter/Space
  activate, Escape closes.
- Scope shortcuts to the active interaction. Document-level handlers must not
  consume normal typing in inputs or editable content, or turn button activation
  into an unrelated action.
- Color is never the only signal (pair it with text or an icon). Meet WCAG AA
  contrast for text.
- Respect `prefers-reduced-motion`; keep motion functional, not flashy.

## Components and composition

- Build a reusable component when a visual pattern appears in 2+ places, has
  interactive behavior, or encodes domain meaning (a status pill, a metric card).
- Do NOT build a component for a one-off layout or a bare className combo.
- Compose over configure: prefer children and small explicit variants over a pile
  of boolean props (`isPrimary`, `isSmall`, `isGhost` ...). Boolean-prop
  proliferation is the smell that a component should be split.
- Keep state where it is used; lift it only when siblings need it.

## Performance (apply when the stack makes it relevant)

- Do not block first paint on data you can defer or stream.
- Fetch independent things in parallel, not in a waterfall.
- Ship only the code a view needs; load heavy pieces on demand.
- Cheap synchronous checks before expensive async work.

## Verify your own work before you hand it off

- It renders with no console errors, at 360px and at a desktop width.
- Every data value on screen came from a backend call you can point to.
- Service reads and writes work through the actual hosting path, including a proxy
  prefix when present. Existing routes still work without new configuration.
- Keyboard-only: you can reach and operate every control, focus is always visible.
- Loading, empty, and error states all exist and are distinct.
- No business logic, pricing, or copied data lives in the page.

The measure of the deliverable is not that a specific file exists; it is that a
person can use the interface and every answer it shows is the backend's, rendered
faithfully.
