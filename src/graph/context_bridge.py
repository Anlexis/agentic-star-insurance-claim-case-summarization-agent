"""AgentCore Platform v1.0"""

# src/graph/context_bridge.py — carries the caller's input_context across the
# outer→inner graph boundary.
#
# Why this exists: GraphNode.execute() (framework) invokes the inner graph as
# `subgraph.invoke(user_input, session_id=..., ctx=...)` WITHOUT forwarding the
# outer state's input_context, so inner-node reads of state["input_context"]
# (the caller's damages data) would always see {} through the full nested
# graph. The sanctioned subclass hooks bridge it:
#
#   ClaimSummaryGraphNode.extract_input(state)   [runs BEFORE subgraph.invoke]
#       → set_caller_input_context(state["input_context"])
#   DomainWorkflowGraph._extra_initial_state()   [runs INSIDE subgraph.invoke]
#       → returns {"input_context": get_caller_input_context()}
#
# A ContextVar keeps the hand-off correct per thread/task, so concurrent
# invocations in one process cannot see each other's context.

from contextvars import ContextVar
from typing import Any

_CALLER_INPUT_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar("ins_c2_004_caller_input_context", default=None)


def set_caller_input_context(input_context: dict[str, Any] | None) -> None:
    """Stash the outer graph's input_context for the imminent inner-graph invoke."""
    _CALLER_INPUT_CONTEXT.set(dict(input_context) if input_context else {})


def get_caller_input_context() -> dict[str, Any]:
    """Read (without consuming) the stashed input_context; {} when none was set."""
    return _CALLER_INPUT_CONTEXT.get() or {}
