"""A model error must never produce the deployment script's success message."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

SPEC = importlib.util.spec_from_file_location(
    "coordinator_probe", Path(__file__).with_name("probe_coordinator.py"))
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def test_streamed_model_error_is_rejected_even_when_cli_returns_zero():
    # The real fresh-event failure: the CLI printed this model error and exited 0.
    response = (
        "Error: An error occurred (ValidationException) when calling the "
        "ConverseStream operation: The provided model identifier is invalid."
    )
    with pytest.raises(probe.ProbeFailure, match="model identifier is invalid"):
        probe.read_result(json.dumps({"success": True, "response": response}), 0)


@pytest.mark.parametrize("payload,exit_code", [
    ({"success": False, "error": "Access denied"}, 0),
    ({"success": True, "response": "An answer"}, 1),
    ({"success": True, "response": ""}, 0),
    ({"success": True, "response": " \n\t "}, 0),
    ({"success": True, "response": {"error": "Model unavailable"}}, 0),
    ({"response": "Missing status"}, 0),
    ([], 0),
])
def test_missing_or_failed_probe_is_not_success(payload, exit_code):
    with pytest.raises(probe.ProbeFailure):
        probe.read_result(json.dumps(payload), exit_code)


def test_non_json_output_is_not_a_probe_result():
    with pytest.raises(probe.ProbeFailure):
        probe.read_result("Session: test\n", 0)


def test_answer_does_not_depend_on_a_models_choice_of_synonym():
    # "checker" and "validator" describe the same role. The deployment wrapper
    # verifies transport and a nonempty answer; the attendee inspects the routing.
    answer = "claude-code is the backend builder; kiro is the checker."
    result = probe.read_result(json.dumps({"success": True, "response": answer}), 0)
    assert result["response"] == answer


def test_timeout_exits_nonzero_without_printing_success(capsys, tmp_path):
    with patch.object(sys, "argv", ["probe", "--project", str(tmp_path)]):
        with patch.object(probe.subprocess, "run", side_effect=subprocess.TimeoutExpired("agentcore", 180)):
            assert probe.main() == 1
    output = capsys.readouterr()
    assert "180 seconds" in output.err
    assert not output.out
