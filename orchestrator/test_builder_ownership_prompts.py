"""Exercise the actual prompt builders with single and multiple owners."""
from types import SimpleNamespace

import pytest

import engine
import integration_plan
import roles
from work_items import WorkItem


@pytest.mark.parametrize("builders", [
    ("claude-code",),
    ("codex",),
    ("claude-code", "codex"),
])
@pytest.mark.parametrize("repair", [False, True])
def test_routed_ownership_reaches_the_prompt_even_during_one_owners_repair(
    monkeypatch, tmp_path, builders, repair,
):
    monkeypatch.setattr(engine, "_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(
        "socket.socket.connect",
        lambda *_args: pytest.fail("prompt construction must stay offline"),
    )
    run = engine.Run(
        run_id="run_ownership", task="Complete the user's original request.",
        agents=[*builders, "kiro"], roles={},
    )
    for agent in run.agents:
        definition = roles.get(agent)
        run.work_items[agent] = WorkItem.create(
            run.run_id, agent, definition.role_name, definition.capability,
            kind=definition.kind, token=agent,
        )
    items = [run.work_items[agent] for agent in builders]
    run.integration_brief = integration_plan.create(
        run.task, items, offline_fixture=len(items) > 1)
    if repair:
        # A repair dispatches one owner, but that does not turn a multi-builder
        # request into sole ownership of its siblings' capabilities.
        run._active_builders = {builders[0]}
    prompts = []
    worker = engine.Engine(executor_obj=SimpleNamespace(name="fixture"))
    monkeypatch.setattr(worker, "_runtime_cli",
                        lambda _run, _agent, _role, prompt, _model: prompts.append(prompt))
    monkeypatch.setattr(run, "term", lambda *_args, **_kwargs: None)
    try:
        for agent in builders[:1] if repair else builders:
            role = engine.RoleResult(agent=agent, role=roles.get(agent).role_name)
            if roles.get(agent).capability == "frontend":
                worker._cli_frontend_work(run, "", role)
            else:
                worker._cli_backend_server(run, role)
    finally:
        worker.shutdown()
    assert prompts
    for prompt in prompts:
        assert run.task in prompt
        if len(builders) == 1:
            assert "only builder assigned" in prompt
            assert "complete requested behavior" in prompt
            assert "not supposed to contain the other builders" not in prompt
            assert "do not make it standalone" not in prompt
        else:
            assert "Ownership is exclusive" in prompt
            assert "second implementation of a sibling role's capability" in prompt
            assert "only builder assigned" not in prompt
