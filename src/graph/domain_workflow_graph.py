"""AgentCore Platform v1.0"""

# INS-C2-004 — DomainWorkflowGraph (inner BaseGraph)
#
# This is the INNER graph for the Cat 2 two-layer nested architecture.
# It encapsulates the insurance claim-case summarization pipeline:
#
#   START
#     → input_validate     (InputValidateNode)
#     → build_context      (BuildContextNode)
#     → generate_response  (GenerateResponseNode — LLM)
#     → pii_redact         (PiiRedactNode)
#     → output_format      (OutputFormatNode)
#     → END
#
# Called by ClaimSummaryGraphNode.get_subgraph() (graph.py).
# get_output() shapes the sub_result dict consumed by merge_output() there.
#
# Rules enforced:
#   ✅ Inherits BaseGraph (fully custom topology — no forced backbone)
#   ✅ Implements all 7 BaseGraph ABC methods
#   ✅ register_nodes() does NOT call super() (abstract in BaseGraph)
#   ✅ Does NOT register initialize / finalize (outer backbone concerns)
#   ✅ All inner nodes declare required_trust_level = TrustLevel.ANONYMOUS
#   ✅ get_output() designed together with ClaimSummaryGraphNode.merge_output()
#   ✅ Static config reaches the domain nodes through their constructors
#      (the node contract is execute(self, state) -> dict — state only)
#   ✅ _extra_initial_state() seeds the caller's input_context (context bridge)
#   ❌ No platform-SDK imports (framework/ and shared/ only)
#   ❌ Not placed under src/subagents/

from typing import Any

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.graph.context_bridge import get_caller_input_context
from src.nodes.build_context_node import BuildContextNode
from src.nodes.generate_response_node import GenerateResponseNode
from src.nodes.input_validate_node import InputValidateNode
from src.nodes.output_format_node import OutputFormatNode
from src.nodes.pii_redact_node import PiiRedactNode
from src.schemas.state import State


class DomainWorkflowGraph(BaseGraph):
    """Inner domain workflow graph for INS-C2-004.

    Inherits BaseGraph directly for a fully custom node topology.
    Called by ClaimSummaryGraphNode.get_subgraph() in graph.py.

    Pipeline (linear):
        START
          → input_validate     (InputValidateNode)
          → build_context      (BuildContextNode)
          → generate_response  (GenerateResponseNode — LLM)
          → pii_redact         (PiiRedactNode)
          → output_format      (OutputFormatNode)
          → END

    All nodes are FunctionNode subclasses with ANONYMOUS trust_level.
    initialize / finalize are outer backbone concerns — not registered here.
    """

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "ins_c2_004_claim_case_summarization_workflow"

    @property
    def state_schema(self) -> type:
        return State

    # ── Config validation ─────────────────────────────────────────────────────

    def _validate_config(self) -> None:
        """No mandatory config: every declared setting has a safe default.

        The outer graph validates each value before forwarding it, so an
        absent or malformed key simply leaves the owning node on its built-in
        default rather than failing graph construction.
        """

    def _extra_initial_state(self) -> dict[str, Any]:
        """Seed the inner state with the caller's input_context.

        GraphNode.execute() (framework) does not forward the outer state's
        input_context into subgraph.invoke(), so the outer graph stashes it in
        a ContextVar (ClaimSummaryGraphNode.extract_input) and this hook reads
        it back — see src/graph/context_bridge.py. Without this, inner-node
        reads of state["input_context"] (the caller's damages data) would
        always see {}.
        """
        return {"input_context": get_caller_input_context()}

    # ── Node registration ─────────────────────────────────────────────────────

    def register_nodes(self) -> None:
        """Register all 5 domain nodes.

        No super() call — BaseGraph.register_nodes() is abstract.
        Do NOT register initialize or finalize; those are outer backbone
        concerns handled by AgentBaseGraph in graph.py.
        Every key registered here is referenced in add_edges().

        Config injection: the node contract is `execute(self, state) -> dict`
        — no per-invocation config parameter. The two config-driven nodes
        therefore receive the declared settings (read from config/config.yaml
        and forwarded by ClaimSummaryGraphNode._parent_config() into this
        graph's `config`) through their constructors. When the graph is built
        standalone (`DomainWorkflowGraph()`), `self.config` is empty and both
        nodes fall back to their module defaults.
        """
        settings = self.config or {}

        self._nodes["input_validate"] = InputValidateNode()
        self._nodes["build_context"] = BuildContextNode(config=settings)
        self._nodes["generate_response"] = GenerateResponseNode(config=settings)
        self._nodes["pii_redact"] = PiiRedactNode()
        self._nodes["output_format"] = OutputFormatNode()

    # ── Edge wiring ───────────────────────────────────────────────────────────

    def add_edges(self) -> None:
        """Wire the linear claim-summary generation topology.

        Linear flow:
            input_validate → build_context → generate_response
            → pii_redact → output_format → END.

        No conditional branching — all paths through the summary pipeline are
        linear in v1.  route() satisfies the ABC but is not used at runtime.
        """
        self._sg.add_edge(START, "input_validate")
        self._sg.add_edge("input_validate", "build_context")
        self._sg.add_edge("build_context", "generate_response")
        self._sg.add_edge("generate_response", "pii_redact")
        self._sg.add_edge("pii_redact", "output_format")
        self._sg.add_edge("output_format", END)

    # ── Routing ───────────────────────────────────────────────────────────────

    def route(self, state: AgentState) -> str:
        """Conditional routing — required by BaseGraph ABC.

        Linear topology; add_conditional_edges() is not used, so this method
        is never called at runtime.  Returns END on error so an unexpected
        invocation does not re-enter a processing node.
        """
        if state.get("status") == AgentStatus.ERROR.value:
            return END
        return "output_format"

    # ── Output shape ──────────────────────────────────────────────────────────

    def get_output(self, state: AgentState) -> dict[str, Any]:
        """Shape the output dict returned to the outer graph as sub_result.

        This dict is received by ClaimSummaryGraphNode.merge_output()
        in graph.py as the `sub_result` argument.  Both methods are designed
        together to guarantee field-name consistency:

            Inner get_output() emits:   "claim_reference", "claim_summary",
                                        "redacted_sections", "pii_findings",
                                        "damages_assessment", "status"
            Outer merge_output() reads: sub_result.get(...) for each key above.

        The raw generated sections (summary_sections) are deliberately NOT
        emitted: only the rendered, PII-masked, on-grid section set crosses
        the boundary.
        """
        return {
            # the reason must leave the subgraph or the outer graph cannot report it
            "error_code": state.get("error_code"),
            "claim_reference": state.get("claim_reference"),
            "claim_summary": state.get("claim_summary"),
            "redacted_sections": state.get("redacted_sections"),
            "pii_findings": state.get("pii_findings"),
            "damages_assessment": state.get("damages_assessment"),
            "status": state.get("status"),
            "node_history": state.get("node_history", []),
            "correlation_id": state.get("correlation_id"),
        }
