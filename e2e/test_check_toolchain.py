"""The check toolchain is the same, pinned, everywhere a check runs or is self-tested.

November 17 (60 team builds): checks that installed a browser or jsdom at check time
failed on their own setup and stopped builds, and others fell back to hand-built DOM
fixtures that misjudged working pages. The coordinator (where the engine runs every
authored check) and each checker's own image now install one toolchain from
check-tools.sh, and the check process receives its two locations.

    python3 -m pytest e2e/test_check_toolchain.py -q
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "orchestrator"))

import reviewer  # noqa: E402

SCRIPT = ROOT / "coding-agents" / "check-tools.sh"
IMAGES = {
    "coordinator": ROOT / "orchestrator-agent" / "Dockerfile",
    "kiro": ROOT / "coding-agents" / "kiro" / "Dockerfile",
    "claude-code-validator": ROOT / "coding-agents" / "claude-code-validator" / "Dockerfile",
}
ENV_LINE = "ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright NODE_PATH=/opt/check-tools/node_modules"


def test_the_coordinator_copy_of_the_installer_is_identical():
    assert (ROOT / "orchestrator-agent" / "check-tools.sh").read_bytes() == SCRIPT.read_bytes()


def test_versions_are_exact_pins():
    text = SCRIPT.read_text()
    for name in ("PLAYWRIGHT_VERSION", "JSDOM_VERSION"):
        assert re.search(rf"^{name}=\d+\.\d+\.\d+$", text, re.M), name
    assert "playwright@${PLAYWRIGHT_VERSION}" in text and "jsdom@${JSDOM_VERSION}" in text
    assert "install --only-shell chromium" in text


def test_every_checking_image_installs_it_and_sets_its_locations():
    for name, dockerfile in IMAGES.items():
        text = dockerfile.read_text()
        assert ENV_LINE in text, name
        assert re.search(r"^RUN bash /opt/(workshop-cli/)?check-tools\.sh$", text, re.M), name


def test_the_check_process_receives_the_toolchain_locations():
    assert {"PLAYWRIGHT_BROWSERS_PATH", "NODE_PATH"} <= reviewer._CHECK_ENV_NAMES
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/ms-playwright"
    try:
        assert reviewer._check_environment()["PLAYWRIGHT_BROWSERS_PATH"] == "/opt/ms-playwright"
    finally:
        os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
