"""The GitHub endpoint attendees deploy themselves in Lab 3.

`aws cloudformation package` zips coding-agents/pr_review/ (handler.py) and `deploy`
creates the stack from the host. Its names must fit what the host role may create
(agentcore-* roles, coding-agents-* functions and log groups, /workshop/webhook-url),
and connect-github.sh reads the URL from that parameter.
"""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / "coding-agents" / "pr_review"


class _Loader(yaml.SafeLoader):
    pass


def _tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return {suffix: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {suffix: loader.construct_sequence(node)}
    return {suffix: loader.construct_mapping(node)}


_Loader.add_multi_constructor("!", _tag)


def _template():
    return yaml.load((FOLDER / "template.yaml").read_text(), Loader=_Loader)


def test_the_function_is_the_folders_own_handler_packaged_not_inlined():
    function = _template()["Resources"]["PrReviewFunction"]["Properties"]
    assert function["Code"] == "."
    assert function["Handler"] == "handler.handler"
    assert (FOLDER / "handler.py").is_file()
    assert "def handler(" in (FOLDER / "handler.py").read_text()
    assert function["FunctionName"] == {"Ref": "AWS::StackName"}


def test_names_fit_what_the_host_may_create():
    resources = _template()["Resources"]
    assert resources["PrReviewRole"]["Properties"]["RoleName"] == {"Sub": "agentcore-pr-review-${AWS::Region}"}
    assert resources["PrReviewWebhookUrlParameter"]["Properties"]["Name"] == "/workshop/webhook-url"
    assert resources["PrReviewLogGroup"]["Properties"]["LogGroupName"] == {"Sub": "/${AWS::StackName}/PrReviewWebhook"}


def test_the_review_session_may_reach_a_console_built_runtime():
    role = _template()["Resources"]["PrReviewRole"]["Properties"]
    statements = role["Policies"][0]["PolicyDocument"]["Statement"]
    session = next(s for s in statements if s.get("Sid") == "OneReviewSession")
    assert any("runtime/claude_code-*" in str(r) for r in session["Resource"])


def test_the_url_is_an_output():
    assert "WebhookUrl" in _template()["Outputs"]


def _statements():
    role = _template()["Resources"]["PrReviewRole"]["Properties"]
    return {st.get("Sid"): st for st in role["Policies"][0]["PolicyDocument"]["Statement"] if st.get("Sid")}


def test_the_function_reads_the_webhook_secret_not_the_app_key():
    resource = str(_statements()["WebhookSecret"]["Resource"])
    assert "github-mcp/webhook-" in resource and "github-app" not in resource


def test_the_hand_off_is_never_retried():
    """A retried asynchronous hand-off would post a second review of the same event."""
    config = _template()["Resources"]["PrReviewHandOff"]
    assert config["Type"] == "AWS::Lambda::EventInvokeConfig"
    assert config["Properties"]["MaximumRetryAttempts"] == 0


def test_each_event_is_recorded_in_a_table_the_host_may_create():
    table = _template()["Resources"]["PrReviewDeliveries"]["Properties"]
    assert table["TableName"] == {"Sub": "${AWS::StackName}-deliveries"}
    assert table["TimeToLiveSpecification"] == {"AttributeName": "expires", "Enabled": True}
    function = _template()["Resources"]["PrReviewFunction"]["Properties"]
    assert function["Environment"]["Variables"]["DELIVERIES_TABLE"] == {"Ref": "PrReviewDeliveries"}
    assert _statements()["OnceEach"]["Action"] == ["dynamodb:PutItem", "dynamodb:DeleteItem"]


def _arn(suffix):
    return {"Sub": "arn:${AWS::Partition}:bedrock-agentcore:${AWS::Region}:${AWS::AccountId}:" + suffix}


def test_the_review_session_statement_is_unchanged():
    assert _statements()["OneReviewSession"] == {
        "Sid": "OneReviewSession", "Effect": "Allow",
        "Action": ["bedrock-agentcore:InvokeAgentRuntimeCommand", "bedrock-agentcore:StopRuntimeSession"],
        "Resource": [_arn("runtime/claude_code-*")]}


def test_develop_may_start_the_coordinator_and_nothing_more():
    """One invocation of the coordinator Runtime the CLI names CodingAgents_orchestrator,
    on the Runtime and on its endpoint resource form."""
    assert _statements()["StartTheTeam"] == {
        "Sid": "StartTheTeam", "Effect": "Allow", "Action": "bedrock-agentcore:InvokeAgentRuntime",
        "Resource": [_arn("runtime/CodingAgents_orchestrator-*"),
                     _arn("runtime/CodingAgents_orchestrator-*/runtime-endpoint/*")]}
    handler = (FOLDER / "handler.py").read_text()
    assert 'COORDINATOR_NAME = "CodingAgents_orchestrator"' in handler


def test_no_statement_may_stop_a_coordinator_session():
    """The team's build runs in the coordinator session after the invocation returns;
    stopping that session would end the build."""
    for statement in _statements().values():
        actions = statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
        if any(a in ("bedrock-agentcore:StopRuntimeSession", "bedrock-agentcore:*", "*") for a in actions):
            assert "orchestrator" not in str(statement["Resource"]), statement["Sid"]
