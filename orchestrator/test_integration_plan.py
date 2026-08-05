"""The shared brief coordinates roles without becoming an answer key."""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import integration_plan  # noqa: E402
from work_items import WorkItem, dependency_order  # noqa: E402


def _items():
    return [
        WorkItem.create("run_1", "claude-code", "backend-builder", "backend",
                        token="back"),
        WorkItem.create("run_1", "opencode", "frontend-builder", "frontend",
                        token="front"),
    ]


def test_fixture_plan_names_dynamic_roles_and_sets_merge_dependencies():
    items = _items()
    plan = integration_plan.create("build anything", items, offline_fixture=True)
    assert set(plan["role_assignments"]) == {"claude-code", "opencode"}
    assert [item.agent for item in dependency_order(items)] == [
        "claude-code", "opencode"]
    assert items[1].depends_on == [items[0].work_id]
    rendered = integration_plan.markdown("build anything", plan, items)
    assert "build anything" in rendered
    assert items[0].work_id in rendered and items[1].work_id in rendered
    assert "Ownership is exclusive" in rendered

    items[0].changed_files = ["api/service.py"]
    items[1].changed_files = ["web/app.tsx"]
    checker = WorkItem.create(
        "run_1", "kiro", "acceptance-validator", "validator",
        kind="checker", token="check")
    checker.changed_files = ["acceptance_check"]
    context = integration_plan.review_context(plan, [*items, checker])
    assert [row["agent"] for row in context["contributions"]] == [
        "claude-code", "opencode"]
    assert context["contributions"][0]["changed_files"] == ["api/service.py"]
    assert context["contributions"][1]["objective"] == "frontend-builder"


def test_invalid_model_plan_repairs_once_then_fails_loud(monkeypatch):
    items = _items()
    valid = integration_plan.create(
        "build anything", _items(), offline_fixture=True)
    responses = iter([
        {"text": '{"shared_contract":[]}'},
        {"text": json.dumps(valid)},
    ])
    monkeypatch.setattr(
        integration_plan.llm,
        "invoke",
        lambda *args, **kwargs: next(responses),
    )
    repaired = integration_plan.create("build anything", items)
    assert set(repaired["role_assignments"]) == {
        "claude-code", "opencode"}

    calls = []
    monkeypatch.setattr(
        integration_plan.llm,
        "invoke",
        lambda *args, **kwargs: (
            calls.append(args[1]) or {"text": '{"shared_contract":[]}'}),
    )
    with pytest.raises(integration_plan.IntegrationPlanError,
                       match="repair attempt exhausted"):
        integration_plan.create("build anything", _items())
    assert len(calls) == 2
    assert "Required builder ids" in calls[1]


def test_repair_router_selects_an_owner_and_falls_back_to_all_builders(
        monkeypatch):
    items = _items()
    plan = integration_plan.create("build anything", items, offline_fixture=True)
    monkeypatch.setattr(
        integration_plan.llm,
        "invoke",
        lambda *args, **kwargs: {
            "text": '{"agents":["opencode"],"reason":"frontend owns the finding"}'
        },
    )
    selected, reason = integration_plan.select_repair_agents(
        "build anything", plan, items, {"gate": "red"})
    assert selected == ["opencode"]
    assert "frontend" in reason

    monkeypatch.setattr(
        integration_plan.llm,
        "invoke",
        lambda *args, **kwargs: {
            "text": '{"agents":["not-a-role"],"reason":"bad"}'
        },
    )
    selected, reason = integration_plan.select_repair_agents(
        "build anything", plan, items, {"gate": "red"})
    assert selected == ["claude-code", "opencode"]
    assert "every builder" in reason
