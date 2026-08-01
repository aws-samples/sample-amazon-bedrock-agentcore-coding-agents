"""The orchestrator agent: a Strands agent on Amazon Bedrock AgentCore Runtime.

This is the artifact attendees deploy in Module 2 with the `agentcore`
CLI. It is the SAME shape the CLI scaffolds (`agentcore add agent --framework
Strands`): a ``BedrockAgentCoreApp`` wrapping a Strands ``Agent``, with an
``@app.entrypoint`` that streams the agent's reasoning back to the caller.

What makes it an orchestrator rather than a chatbot is its tools. It follows the
agents-as-tools pattern: the model clarifies an ambiguous request first, then
calls the active roles on their own AgentCore Runtimes. ``chat.py`` generates
the ``dispatch_*`` tools from the active ``roles.py`` roster, so the model
decides which wired roles run instead of using a fixed fan-out.

  * ``list_presets``      : the example starting points (any request works)
                            (advisory: it suggests which capabilities a task needs).
  * ``dispatch_*``        : generated tools for the roles in the active roster.
  * ``run_build``         : the composed pipeline; dispatch the routed roles,
                            compose their work, execute the validator-authored
                            check, require the integrated read-only review, and report
                            the PR result.
  * ``run_status``        : read back a run's verdict, gate checks, and PR URL.

The model decides the sequence: clarify if the ask is ambiguous, then either
dispatch individual agents as tools (subagents-as-tool) or call ``run_build`` for
the full composed pipeline. The tools do the real work by calling the same
in-process engine the console drives: each ``dispatch_*`` submits a single-role
run to that role's DEPLOYED Runtime. ``run_build`` assembles the routed builder
work and passes the immutable candidate through the validator-authored executable
and one integrated review that must cover adversarial and design/integration lenses.

Run a non-dispatching local check from the generated CLI project:
    agentcore dev --logs
    agentcore dev --stream "List the preset starting points. Do not dispatch anything."

Deploy it (new @aws/agentcore CLI, container, CDK):
    agentcore deploy
    agentcore invoke --stream "List the preset starting points. Do not dispatch anything."
"""

from __future__ import annotations

import os
import sys
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp

# The orchestrator's brain (system prompt, tools, and Agent factory) lives in
# the orchestrator package (``chat.py``), so the deployed runtime and the console
# chat endpoint share ONE definition. On the workshop box that package sits next
# to this file (the container stages orchestrator/ in); locally we add it
# to the path so `python3 main.py` works straight from the repo.
_HERE = os.path.dirname(os.path.abspath(__file__))
for _cand in (os.path.join(_HERE, "orchestrator"),
              os.path.join(_HERE, "..", "orchestrator")):
    if os.path.isdir(_cand):
        sys.path.insert(0, os.path.abspath(_cand))
        break


import chat as _chat              # noqa: E402  the orchestrator brain (prompt+tools+agent)

app = BedrockAgentCoreApp()
log = app.logger

# This file is the thin AgentCore Runtime wrapper: it builds the chat.py agent
# and streams its turns. The tools are real-only: each dispatch_*/run_build
# submits a run to the engine, which sends each routed role to its DEPLOYED
# AgentCore Runtime; a role with no wired runtime ARN fails loud at pre-flight,
# never a local build.
SYSTEM_PROMPT = _chat.SYSTEM_PROMPT  # re-exported for tests/back-compat

_agent = None


def _get_or_create_agent():
    """Lazily build the orchestrator agent (created once per runtime, reused per
    call). ``ORCHESTRATOR_MODEL_ID`` sets the model; chat.build_agent wires the
    system prompt and the roster-derived tool set."""
    global _agent
    if _agent is None:
        _agent = _chat.build_agent()
    return _agent


@app.entrypoint
async def invoke(payload: dict[str, Any], context: Any = None):
    """AgentCore Runtime entry point. Streams the orchestrator's text back.

    ``payload`` carries ``{"prompt": "<the task>"}``: the natural-language task
    a user typed. We stream the agent's text deltas so the console (or
    ``agentcore invoke``) shows the orchestrator working in real time. A run is
    born only when the agent calls a dispatch_*/run_build tool, exactly as in the
    console chat.

    Identity propagation: when ``user_identity`` is in the payload (set by the
    console from its Cognito session), we set it as the current identity so the
    engine tags the run and the runtime dispatch carries the OBO headers.
    """
    prompt = (payload or {}).get("prompt") or ""
    if not prompt:
        yield "No prompt found. Send {\"prompt\": \"<your task>\"}."
        return

    # Propagate user identity (Cognito baggage) into the engine context
    user_identity = (payload or {}).get("user_identity")
    if user_identity:
        try:
            from identity_baggage import UserIdentity, set_current_identity
            set_current_identity(UserIdentity.from_dict(user_identity))
        except Exception:
            pass

    log.info("orchestrator invoked: %s", prompt[:200])
    agent = _get_or_create_agent()
    async for event in agent.stream_async(prompt):
        if "data" in event and isinstance(event["data"], str):
            yield event["data"]


if __name__ == "__main__":
    app.run()
