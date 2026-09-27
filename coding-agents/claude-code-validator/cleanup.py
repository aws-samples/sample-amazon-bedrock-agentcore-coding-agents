"""
Delete the Claude Code validator (restore path) AgentCore runtime, its IAM role, and its ECR repository.
Does NOT delete shared infra (VPC, S3 Files). The teardown itself is shared:
coding-agents/role_cleanup.py.

Usage:
    python cleanup.py
"""
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(SCRIPT_DIR))
import role_cleanup  # noqa: E402

if __name__ == "__main__":
    role_cleanup.parse_args("Delete the Claude Code validator (restore path) Runtime, IAM role, and ECR repository.")
    sys.exit(role_cleanup.cleanup(SCRIPT_DIR))
