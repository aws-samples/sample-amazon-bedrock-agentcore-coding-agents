"""Harness configuration: where each role's steering lives, and what it asks for.

A role's steering file is its identity (who it is, how it works), and it may carry
``harness:setup`` blocks declaring what to install into its container before it
works: MCP servers, skills, install commands. That is the attendee's extension seam,
and this module is the only thing that reads it.

What this module deliberately does NOT do is describe the work. It knows no protocol,
no filename, no expected value, and no shape of a correct answer. Validation is
agentic: the validator role writes the check and its real exit code decides, so
nothing here (and nothing anywhere in this repository) encodes what the agents are
supposed to produce.

This replaced the deterministic ``builders.py``, which generated a working reference
server and chatbot page from ``harness:build`` / ``harness:ui`` blocks. Those blocks
and that generator are gone: they were the repository writing the answer.
"""

from __future__ import annotations

import os
import re
from typing import Any

import roles as _roles

_HERE = os.path.dirname(os.path.abspath(__file__))
_HARNESS = os.path.join(_HERE, "harness")


def harness_file(agent: str) -> str:
    """Where this role's steering file lives, in its own real format.

    Resolved from the role REGISTRY (``roles.py``), which declares each role's
    steering filename once, so there is no per-agent path table here to fall out of
    step with the roster. Raises for an unregistered role rather than guessing.

    There is no per-use-case variant any more: the attendee's request is whatever
    they type, so there is nothing for a use case to select.
    """
    role = _roles.get(agent)
    return os.path.join(_HARNESS, role.harness_dir, role.steering_file)


def steering_filename(agent: str) -> str:
    """The filename the role's CLI reads from its cwd (CLAUDE.md, AGENTS.md, ...),
    so the engine can stage steering without knowing which CLI it is talking to."""
    return _roles.get(agent).steering_file



# --------------------------------------------------------------- setup parsing
def _fenced_blocks(text: str, tag: str) -> list[str]:
    """Return the bodies of ALL ```<tag> ... ``` fenced blocks, in order. Used for
    ``harness:setup``: a role's steering may ship a default setup block (e.g. the
    skill it installs) AND an attendee may add their own; both must apply, so the
    parser reads every block rather than only the first."""
    return re.findall(r"```" + re.escape(tag) + r"\s*\n(.*?)```", text, re.DOTALL)

def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()

def parse_setup_spec(steering_path: str) -> dict[str, Any]:
    """Parse the OPTIONAL ``harness:setup`` block from any role's steering file.

    The named blocks (``harness:build`` / ``harness:ui`` / ``harness:gate``) are
    the defaults the workshop ships, but a harness is the attendee's to extend.
    Anything listed here is set up in the role's container before it works, the
    way a developer extends their own harness with MCP servers, extra skills, or
    install steps.

        ```harness:setup
        mcp:
          - name: github
            url: https://<gateway-id>.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp
        skills:
          - skills/my-team-skill
        install:
          - pip install --quiet rich
        ```

    Returns ``{"mcp": [{name,url}], "skills": [paths], "install": [commands]}``,
    all empty when the block is absent (the defaults need no setup).
    """
    # Merge EVERY harness:setup block: the shipped default (the role's skill) plus
    # any the attendee adds. Concatenate their bodies and parse as one.
    body = "\n".join(_fenced_blocks(_read(steering_path), "harness:setup"))
    spec: dict[str, Any] = {"mcp": [], "skills": [], "install": []}
    section = None
    pending_mcp: dict[str, str] | None = None
    for raw in body.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        if stripped in ("mcp:", "skills:", "install:"):
            if pending_mcp:
                spec["mcp"].append(pending_mcp)
                pending_mcp = None
            section = stripped[:-1]
            continue
        if section == "mcp":
            if stripped.startswith("- "):
                if pending_mcp:
                    spec["mcp"].append(pending_mcp)
                pending_mcp = {}
                stripped = stripped[2:].strip()
            if ":" in stripped and pending_mcp is not None:
                k, _, v = stripped.partition(":")
                pending_mcp[k.strip()] = v.strip()
        elif section in ("skills", "install") and stripped.startswith("- "):
            spec[section].append(stripped[2:].strip())
    if pending_mcp:
        spec["mcp"].append(pending_mcp)
    return spec
