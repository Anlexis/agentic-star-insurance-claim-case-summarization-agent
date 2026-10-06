"""AgentCore Platform v1.0"""

# INS-C2-004 — DEPRECATED: MainNode
#
# This file is superseded by the Cat 2 nested architecture: the `main` slot of
# InsuranceClaimCaseSummarizationAgent is filled by ClaimSummaryGraphNode
# (src/graph/graph.py), which delegates the domain workflow to
# DomainWorkflowGraph (domain nodes in src/nodes/).
#
# The stub is retained on purpose so the `src.nodes.main_node` import path keeps
# resolving and its execute() contract stays explicitly defined: it emits an
# audit event and returns a SUCCESS status carrying the deprecation message.
# It is NOT imported by graph.py or any runtime path.
# DO NOT USE for new work.

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event

_DEPRECATION_MSG = (
    "DEPRECATED: MainNode is superseded by the Cat 2 DomainWorkflowGraph pipeline "
    "(ClaimSummaryGraphNode -> DomainWorkflowGraph). "
    "This stub is retained for import compatibility only."
)


class MainNode(FunctionNode):
    """DEPRECATED -- superseded by Cat 2 domain nodes in DomainWorkflowGraph."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> dict[str, Any]:
        # An audit event is required in every execute().
        emit_trace_event(
            "main_node_deprecated_called",
            {
                "warning": "MainNode is deprecated and not part of the Cat 2 pipeline",
                "replacement": "ClaimSummaryGraphNode + DomainWorkflowGraph",
            },
            state,
        )
        return {
            "status": AgentStatus.SUCCESS.value,
            "result": _DEPRECATION_MSG,
        }
