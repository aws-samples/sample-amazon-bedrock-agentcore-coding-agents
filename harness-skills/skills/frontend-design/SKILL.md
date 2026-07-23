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

Distilled from the practices Vercel and design-system teams publish for
agent-built frontends. When the task points at a specific stack (React, plain
HTML, a component library), follow that stack's own conventions first and use
these as the cross-cutting bar.

## The one rule that outranks the rest: the UI is thin

The frontend renders and interacts. It does not own business logic or data.

- Every value the user sees that comes from a computation, a price, a record, or
  a model MUST come from a backend call (an API, an MCP tool call, a fetch). The
  UI sends inputs and renders the structured response.
- Do not embed the numbers, the pricing, the rules, or a copy of the data in the
  page. The moment the UI computes a result itself it can disagree with the
  system of record, and it will.
- Parse the response and render it. Show the backend's own error when a call
  fails; never invent a value to fill a gap.

If you are asked for a UI over a service, the correctness of every answer lives
on the wire, not in the markup.

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

- Neutral surfaces carry the UI; reserve accent color for meaning (status,
  priority, the primary action), not for decoration.
- Define semantic roles, not raw hexes scattered inline: background/foreground,
  muted, border, primary, destructive, and a success/warning/error set. Use the
  role name everywhere so a theme change is one place.
- Every interactive element needs visible hover, focus, active, and disabled
  states. A focus ring is not optional.
- Represent every async surface's full lifecycle: loading, empty, error, and
  success. An empty list and a failed fetch must look different, and neither
  should look like success.

## Accessibility (non-negotiable, cheap to get right)

- Semantic HTML first: `button` for actions, `a` for navigation, real labels
  tied to inputs, one `h1` then a sensible heading order.
- Keyboard reachable and operable: logical tab order, visible focus, Enter/Space
  activate, Escape closes.
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
- Keyboard-only: you can reach and operate every control, focus is always visible.
- Loading, empty, and error states all exist and are distinct.
- No business logic, pricing, or copied data lives in the page.

The measure of the deliverable is not that a specific file exists; it is that a
person can use the interface and every answer it shows is the backend's, rendered
faithfully.
