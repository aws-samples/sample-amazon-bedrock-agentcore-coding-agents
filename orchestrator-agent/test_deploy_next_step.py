"""The deploy's closing hint is the request the page tells the room to send.

October 13 live walk: the deploy still printed `./ask.sh "Use preset=team-game."` after
part 4 had moved to a request with the attendee's own creative direction.
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "orchestrator"))

import progress  # noqa: E402


def test_the_printed_request_is_the_pages():
    script = (HERE / "deploy-coordinator.sh").read_text()
    printed = re.findall(r'^echo "  (\./ask\.sh .*)"$', script, re.M)
    assert printed, "deploy-coordinator.sh no longer prints the next request"
    assert printed[-1].replace('\\"', '"') == progress.CMD_ASK[-1]


def test_the_closing_hint_asks_for_no_grant_step():
    # Lab 1's prepared role already carries Claude Code's archive grant, and Deploy the
    # Coordinator step 4 adds none (live dry run, 2026-10-29: the old hint still sent the
    # attendee to "Claude Code's grant").
    script = (HERE / "deploy-coordinator.sh").read_text()
    closing = script[script.index('echo "Coordinator deployed.'):]
    assert "grant" not in closing.split("./ask.sh")[0]
