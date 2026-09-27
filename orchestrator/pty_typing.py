"""Keep every line typed into a Runtime shell short enough to survive the terminal.

A dispatched command reaches its Runtime shell as typed keystrokes. Until bash's
line editor takes over, the PTY is in canonical mode, where Linux keeps at most
4095 characters of one line and drops the rest. On 2026-09-27 a Lab 2 build typed
a 10,980-character line (the base64 prompt made it one line), bash received a
truncated command, and it waited at a continuation prompt until the 1,200-second
dispatch limit. A probe on the same Runtime ran a 3,930-character line and hung
on a 4,730-character one.

So a command with any long line is typed as short base64 chunks and run with
``eval`` in the same shell. That keeps ``cd``, exports, ``exit`` and the exit
status exactly as if the whole command had been typed, and the typed bytes carry
no quote, tab, ``!`` or control character for the terminal to reinterpret.
Short commands are typed unchanged, so a watched terminal still shows them.
"""
from __future__ import annotations

import base64

SAFE_LINE = 3000   # well under the 4095-character canonical limit
CHUNK = 1000       # also under macOS's 1024, so local PTY tests exercise the same path


def typeable(command: str) -> str:
    """The keystrokes to send for ``command``, each line short enough to arrive whole."""
    body = command.rstrip("\n")
    if all(len(line) <= SAFE_LINE for line in body.split("\n")):
        return body + "\n"
    encoded = base64.b64encode(body.encode("utf-8")).decode("ascii")
    # POSIX forms only, so any Runtime login shell runs it; base64 needs no quoting.
    lines = ["__wc="]
    lines += [f'__wc="${{__wc}}{encoded[i:i + CHUNK]}"' for i in range(0, len(encoded), CHUNK)]
    lines.append('eval "$(printf %s "$__wc" | base64 -d)"')
    return "\n".join(lines) + "\n"
