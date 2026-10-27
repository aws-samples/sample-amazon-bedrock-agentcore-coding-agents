"""The agent-execution phase as a Strands graph, instead of a hand-rolled thread join.

The workshop teaches loop engineering, so the loop itself should be expressed with the
agent framework rather than reimplemented with threads and a countdown latch. This
module builds a ``strands.multiagent.GraphBuilder`` graph whose nodes are the ROLES the
run routed, with the edges that encode the one structural rule of the harness:

    every builder  ->  the checker

Builders have no dependencies, so Strands runs them as one parallel entry batch,
exactly as the threads did; the checker runs after them. A scheduler-only marker in
that batch also points to the checker, so failed builders cannot prune the join.

## The line that does not move

The loop may be non-deterministic. **The VERDICT may not.** So this module orchestrates
WHO RUNS AND WHEN, and it decides nothing about the outcome:

  * A node's body is the SAME closure the engine already used, dispatched through the
    SAME ``executor`` seam, so what a role does is unchanged.
  * The acceptance gate, compose, the PR calls, and every fail-loud raise stay in
    deterministic engine code, downstream of this graph.
  * A node that raises is recorded as a role failure and the graph continues, because
    the checker still has to look at whatever WAS written. A red gate is a real
    verdict; a hang is no verdict at all, which is strictly worse.

## Strands behaviors this code has to be explicit about

1. **A node fires when ANY incoming edge is satisfied**, not all of them
   (``Graph._is_node_ready_with_conditions`` returns True on the first satisfied
   edge). With N builders pointing at the checker, the default would start the checker
   after the FIRST builder finished. So the checker's edges carry an explicit
   ``all_builders_done`` condition: AND semantics, stated rather than assumed.
2. **The graph's own node budget is a backstop.** ``set_max_node_executions`` bounds
   total node runs so a mis-specified graph cannot spin; the engine's bounded
   re-implement round (``reviewer.MAX_REVIEW_ROUNDS``) remains the loop bound that
   matters, and it lives outside this graph.
3. **Failed nodes do not nominate successors.** Strands 1.62 prunes their outgoing
   edges before evaluating conditions. A no-op marker in the builder batch gives
   the checker a completion edge even when every builder failed. Its edge uses the
   SAME ``all_builders_done`` condition; each builder and the graph still report
   FAILED. The marker is scheduling only, never a role, artifact, or verdict.
"""

from __future__ import annotations

import time
from typing import Any, Callable

import roles as _roles


class RoleNodeError(RuntimeError):
    """A role node could not be built or dispatched. Fails loud, never a silent skip."""


def _node_result(text: str, status: Any) -> Any:
    """Wrap a role's outcome in the shape Strands expects from a custom node.

    Strands needs a ``MultiAgentResult`` whose ``results`` map carries ``NodeResult``s,
    and a ``NodeResult.result`` must be an ``AgentResult`` / nested ``MultiAgentResult``
    / ``Exception``: the graph flattens results to agent messages when it builds the
    next node's input, so a bare string raises there rather than here. Hence a real
    ``AgentResult`` carrying one text block.

    The engine's real record of what happened is ``run.progress`` (per-role state,
    latency, note, events), so this text is only a short human-readable line: the graph
    is the schedule, not the source of truth about the work.
    """
    from strands.agent import AgentResult
    from strands.multiagent.base import MultiAgentResult, NodeResult
    from strands.telemetry.metrics import EventLoopMetrics
    agent_result = AgentResult(stop_reason="end_turn",
                               message={"role": "assistant",
                                        "content": [{"text": text}]},
                               metrics=EventLoopMetrics(), state={})
    return MultiAgentResult(
        status=status,
        results={"role": NodeResult(result=agent_result, status=status)})


# The node class is built LAZILY, on first use, because it must SUBCLASS the SDK's
# ``MultiAgentBase``: Strands does a real ``isinstance`` check in ``Graph._execute_node``
# and raises "Node ... is not supported" for a duck-typed object, so structural typing is
# not enough here. Building it inside a function keeps ``import role_graph`` free of the
# SDK, which matters because the offline suite imports the engine without strands
# installed on the path.
_ROLE_NODE_CLS: Any = None


def _role_node_cls() -> Any:
    """The ``MultiAgentBase`` subclass used for one routed role."""
    global _ROLE_NODE_CLS
    if _ROLE_NODE_CLS is not None:
        return _ROLE_NODE_CLS

    from strands.multiagent.base import MultiAgentBase, Status

    class RoleNode(MultiAgentBase):
        """One routed role as a Strands graph node.

        Wraps the engine's existing per-role closure, so WHAT a role does is unchanged;
        this only makes WHEN it runs the graph's business. ``invoke_async`` is the one
        abstract method (the base's ``stream_async`` already delegates to it), and the
        closure is synchronous, so it runs in a worker thread to keep the event loop
        free while a real microVM dispatch is in flight.
        """

        def __init__(self, agent_id: str, dispatch: Callable[[], None]) -> None:
            self.id = agent_id
            self.agent_id = agent_id
            self._dispatch = dispatch
            self.error: BaseException | None = None

        async def invoke_async(self, task: Any = None, invocation_state: Any = None,
                               **kwargs: Any) -> Any:
            import asyncio
            t0 = time.monotonic()
            try:
                await asyncio.to_thread(self._dispatch)
            except asyncio.CancelledError as exc:
                # Cancellation does not stop the synchronous worker. Do not make
                # it a finished predecessor that could release the checker early.
                # run_graph drains that worker before returning to the engine.
                self.error = exc
                raise
            except BaseException as exc:      # noqa: BLE001 (recorded, not swallowed)
                # Record it and report the node FAILED, rather than letting the
                # exception abort the graph. The engine's real record of the failure is
                # ``run.progress`` (the closure set it already), and the checker still
                # has to look at whatever WAS written: one role's exception must not
                # become a whole-run hang, because a hang reports no verdict at all.
                self.error = exc
                return _node_result(f"{self.agent_id}: {type(exc).__name__}: {exc}",
                                    Status.FAILED)
            return _node_result(
                f"{self.agent_id}: done in {int((time.monotonic() - t0) * 1000)}ms",
                Status.COMPLETED)

    _ROLE_NODE_CLS = RoleNode
    return RoleNode


def build_graph(agent_ids: list[str], dispatch_for: Callable[[str], Callable[[], None]],
                execution_timeout: float | None = None,
                checker_waits_for: set[str] | None = None,
                after_partial: Callable[[], None] | None = None) -> Any:
    """Build the role graph for ONE run: builders in parallel, then the checker.

    ``dispatch_for(agent_id)`` returns the zero-arg callable that actually runs that
    role (the engine's closure, through its executor seam). Nothing about the roster is
    hardcoded here: which ids are builders and which check comes from the registry, so
    a two-role team or a swapped checker needs no change in this file.

    Raises ``RoleNodeError`` when the routed set cannot form the required shape, rather
    than quietly building a graph that would produce an ungated deliverable.
    """
    from strands.multiagent import GraphBuilder

    checkers = set(_roles.checker_ids())
    builders = [a for a in agent_ids if a not in checkers]
    routed_checkers = [a for a in agent_ids if a in checkers]
    if not agent_ids:
        raise RoleNodeError("NO_ROLES_ROUTED: a graph needs at least one role")

    node_cls = _role_node_cls()
    nodes: dict[str, Any] = {}
    builder = GraphBuilder()

    # Strands runs a graph in BATCHES: a node in the next batch starts only after the
    # whole current batch has finished. So a checker joined to only SOME builders would
    # still wait for the slowest one. When the checker grades only some builders now,
    # those builders and the checker become ONE node of the entry batch (builders in
    # parallel, then the checker), beside the builders whose checks come later.
    partial = sorted(set(checker_waits_for or ()) & set(builders))
    if routed_checkers and partial and len(partial) < len(builders):
        def chain() -> None:
            import contextvars
            import threading
            # asyncio.to_thread copies the bound repository and submitter. These
            # manually-created child threads need the same explicit propagation.
            threads = [
                threading.Thread(target=contextvars.copy_context().run,
                                 args=(dispatch_for(b),), name=f"role-{b}")
                for b in partial
            ]
            started = []
            try:
                for t in threads:
                    t.start()
                    started.append(t)
            finally:
                # A later start can fail after an earlier child is running.
                # These children are not the loop's executor threads, so this
                # chain must join them before its own worker can finish.
                for t in started:
                    t.join()
            for checker in routed_checkers:
                dispatch_for(checker)()
            # The engine's deterministic finalization for whatever is already green,
            # while the builders outside this node still work. Not a verdict here.
            if after_partial is not None:
                after_partial()
        chain_id = "+".join(partial + routed_checkers)
        nodes[chain_id] = node_cls(chain_id, chain)
        builder.add_node(nodes[chain_id], node_id=chain_id)
        for agent_id in builders:
            if agent_id not in partial:
                nodes[agent_id] = node_cls(agent_id, dispatch_for(agent_id))
                builder.add_node(nodes[agent_id], node_id=agent_id)
        builder.set_max_node_executions(len(nodes) + 1)
        if execution_timeout:
            builder.set_execution_timeout(execution_timeout)
        return builder.build(), nodes

    for agent_id in agent_ids:
        node = node_cls(agent_id, dispatch_for(agent_id))
        nodes[agent_id] = node
        builder.add_node(node, node_id=agent_id)

    # AND semantics, stated explicitly. Strands starts a node as soon as ONE incoming
    # edge is satisfied, so without this condition the checker would begin after the
    # first builder finished and would grade a half-written tree.
    def all_builders_done(state: Any) -> bool:
        done = {n.node_id for n in getattr(state, "completed_nodes", set())}
        failed = {n.node_id for n in getattr(state, "failed_nodes", set())}
        # A FAILED builder counts as finished: the checker must still look at whatever
        # was written. Waiting for success would turn one role's failure into a hang,
        # and a hang reports nothing while a red gate reports a real verdict.
        return all(b in done or b in failed for b in builders)

    for checker in routed_checkers:
        for b in builders:
            builder.add_edge(b, checker, condition=all_builders_done)

    extra_nodes = 0
    if builders and routed_checkers:
        # The SDK considers successors only after this entire parallel batch has
        # returned. Keep a successful scheduling edge available when every real
        # builder returned FAILED; never relabel a builder as successful to do it.
        batch_id = "_builder_batch"
        while batch_id in nodes:
            batch_id += "_"
        builder.add_node(node_cls(batch_id, lambda: None), node_id=batch_id)
        for checker in routed_checkers:
            builder.add_edge(batch_id, checker, condition=all_builders_done)
        extra_nodes = 1

    # Backstop only: one execution per node, plus headroom, so a mis-specified graph
    # cannot spin. The loop bound that matters (the single re-implement round) is the
    # engine's, outside this graph.
    builder.set_max_node_executions(len(agent_ids) + extra_nodes + 1)
    if execution_timeout:
        builder.set_execution_timeout(execution_timeout)
    graph = builder.build()
    return graph, nodes


def run_graph(graph: Any) -> Any:
    """Execute a built graph to completion from synchronous engine code.

    The engine's phase runs on a worker thread with no event loop, so drive the async
    graph on a fresh one. Errors propagate: the caller decides what a failed phase
    means, and this module never converts a failure into a pass.
    """
    import asyncio
    loop = asyncio.new_event_loop()

    async def drain() -> None:
        # Cancelling asyncio.to_thread does not stop its synchronous worker. In
        # particular the partial chain can still publish/merge a PR. Never return
        # to engine finalization and release repository admission while that code
        # is running. The existing Runtime dispatch deadlines bound each call; a
        # truly wedged call keeps admission closed for operator recovery.
        pending = {task for task in asyncio.all_tasks(loop)
                   if task is not asyncio.current_task(loop)}
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await loop.shutdown_asyncgens()
        await loop.shutdown_default_executor()

    try:
        return loop.run_until_complete(graph.invoke_async(""))
    finally:
        try:
            loop.run_until_complete(drain())
        finally:
            loop.close()
