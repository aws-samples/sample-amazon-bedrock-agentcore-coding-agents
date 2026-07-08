"""Model loader: the one place the orchestrator's model id is set.

The `agentcore` CLI scaffolds this exact seam (`model/load.py` with a
`load_model()` that returns a `BedrockModel`) so the model choice lives in one
file you can change without touching the agent logic. We default to a global
cross-region inference profile so the runtime routes around regional load.

The model is the ORCHESTRATOR'S own brain (the chatbot you talk to), not a
per-role model (the dispatched coding agents bring their own). The console's
message-bar model picker sets THIS, per conversation, by passing ``model_id`` in.
"""

from __future__ import annotations

import os

from strands.models import BedrockModel

DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-6"


def load_model(model_id: str | None = None) -> BedrockModel:
    """Return the Bedrock model the orchestrator reasons with (IAM-auth, no key).

    ``model_id`` (when given) wins: the console passes the message-bar choice
    here per conversation. Otherwise the wirable env default (``ORCHESTRATOR_MODEL_ID``)
    applies, then the global Sonnet profile.
    """
    resolved = model_id or os.environ.get("ORCHESTRATOR_MODEL_ID", DEFAULT_MODEL_ID)
    return BedrockModel(model_id=resolved)
