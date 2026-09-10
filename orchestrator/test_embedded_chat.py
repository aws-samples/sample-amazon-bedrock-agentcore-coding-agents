"""Agent Studio can converse without a separately deployed coordinator Runtime.

A fresh Lab 3 host has worker Runtime connections but no coordinator ARN in the
console settings. Chat runs in that host process. Requiring a coordinator ARN
blocked every chat turn before the model was called.
"""

import contextvars

import connection_api
from identity_baggage import get_current_identity


def test_chat_without_coordinator_streams_and_keeps_identity_and_history(monkeypatch):
    monkeypatch.setattr(connection_api.runtime_config, "resolve", lambda role: None)
    monkeypatch.setattr(connection_api, "_CONVERSATIONS", {})
    observed = []

    def stream(prompt, *, model_id, messages, attachments):
        observed.append((prompt, list(messages), get_current_identity().email))
        yield {"type": "text", "text": "The coordinator runs on this host."}
        yield {
            "type": "done",
            "messages": messages + [{"role": "user", "content": [{"text": prompt}]}],
        }

    monkeypatch.setattr(connection_api._chat, "stream_chat", stream)

    def converse():
        identity = {"user_id": "attendee-sub", "user_email": "attendee@workshop.aws"}
        first = list(connection_api.chat_stream(
            "fresh-console", "Where does Chat run?", user_identity=identity))
        second = list(connection_api.chat_stream(
            "fresh-console", "Does it need another Runtime?", user_identity=identity))
        return first, second

    first, second = contextvars.copy_context().run(converse)
    assert first == second == [
        {"type": "text", "text": "The coordinator runs on this host."},
        {"type": "done"},
    ]
    assert observed[0] == (
        "Where does Chat run?", [], "attendee@workshop.aws")
    assert observed[1] == (
        "Does it need another Runtime?",
        [{"role": "user", "content": [{"text": "Where does Chat run?"}]}],
        "attendee@workshop.aws",
    )
