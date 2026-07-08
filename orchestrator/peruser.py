"""Per-user identity for cost attribution (Stage 3).

The coding agents run their CLI inside their AgentCore Runtime and call Bedrock
under the runtime's execution role, so every call lands in the Bedrock
model-invocation log as the *agent* role (for example
`assumed-role/agentcore-claude_code-us-west-2-role/BedrockAgentCore-...`), not the
human who asked for the work. You can tell which agent ran, but not who it ran for.

To attribute cost to the human, the agent briefly assumes a shared, pre-provisioned
role using the USER as the role-session-name. The session name is what the
invocation log records as the caller, so the log (and the cost dashboard built on
it) can slice by user. The role and its trust are pre-provisioned for you (the
workshop CFN); the one thing you write is the session-name assumption below.

`assume_as_user` returns the shell snippet that does it. The orchestrator's dispatch
(`runtime_exec`, which you do not edit) runs that snippet right before the agent's
CLI, so the CLI's Bedrock calls sign as the user.
"""
import re
import shlex


def assume_as_user(user_id: str, role_arn: str, region: str) -> str:
    """Return a shell snippet that assumes ``role_arn`` with ``user_id`` as the
    role-session-name and exports the temporary credentials, so the command that
    runs after it has its Bedrock calls attributed to this user.

    Returns "" when there is no user or no role wired: the dispatch then runs as
    the agent's runtime role (the un-attributed default), never a fabricated user.
    """
    if not user_id or not role_arn:
        return ""
    session_name = _session_name(user_id)
    return (
        f"_CREDS=$(aws sts assume-role --region {shlex.quote(region)} "
        f"--role-arn {shlex.quote(role_arn)} "
        f"--role-session-name {shlex.quote(session_name)} "
        f"--query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' "
        f"--output text) && export "
        f'AWS_ACCESS_KEY_ID=$(echo "$_CREDS" | cut -f1) '
        f'AWS_SECRET_ACCESS_KEY=$(echo "$_CREDS" | cut -f2) '
        f'AWS_SESSION_TOKEN=$(echo "$_CREDS" | cut -f3); '
    )


def _session_name(user_id: str) -> str:
    """An IAM RoleSessionName allows [\\w+=,.@-] and 2 to 64 chars. Emails pass
    as-is (so the log shows the real identity); anything else is replaced, and the
    whole thing is trimmed to the limit."""
    safe = re.sub(r"[^\w+=,.@-]", "-", user_id or "")[:64]
    return safe or "unknown-user"
