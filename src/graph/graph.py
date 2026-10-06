"""AgentCore Platform v1.0"""

# INS-C2-004 — Outer graph (AgentBaseGraph; Cat 2 two-layer nested architecture)
#
# Architecture (Cat 2):
#
#   Outer backbone (fixed — identical to Cat 1, do NOT override add_edges()):
#     START → initialize → pre_process → main → {route} → post_process → finalize → END
#                                             ↓ (RETRY, max 3)
#                                          pre_process
#
#   `main` slot is a GraphNode subclass (ClaimSummaryGraphNode) that delegates
#   the full domain workflow to DomainWorkflowGraph (inner BaseGraph).
#
#   Domain complexity is fully encapsulated inside the inner graph. The outer
#   backbone is never modified.
#
# Directory layout:
#   src/graph/graph.py                 ← outer graph (this file)
#   src/graph/domain_workflow_graph.py ← inner graph (multi-step topology)
#   src/graph/context_bridge.py        ← input_context hand-off (outer → inner)
#
# Rules enforced:
#   ✅ InsuranceClaimCaseSummarizationAgent inherits AgentBaseGraph (framework
#      base class, direct inheritance)
#   ✅ super().register_nodes() called first (fills initialize + finalize)
#   ✅ ClaimSummaryGraphNode assigned to self._nodes["main"]
#   ✅ PreProcessNode (VERIFIED_EXTERNAL) in pre_process slot (trust gate)
#   ✅ PostProcessNode (ANONYMOUS) in post_process slot (output boundary)
#   ✅ _security_gate_output on the agent class (canonical output gate)
#   ✅ merge_output() returns only changed keys
#   ✅ get_output() surfaces the domain result
#   ✅ class name matches config/agent.yaml class: field exactly
#   ❌ add_edges() NOT overridden on the outer graph
#   ❌ No platform-SDK imports (framework/ and shared/ only)

import math
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Optional, cast

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.graph.context_bridge import set_caller_input_context
from src.nodes.post_process_node import PostProcessNode, _security_gate_output
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State

if TYPE_CHECKING:
    from src.graph.domain_workflow_graph import DomainWorkflowGraph

# Runtime-parameter file: src/graph/graph.py -> parents[2] is the repo root.
# config/agent.yaml (the static manifest) holds only registration identity;
# every runtime parameter lives in config/config.yaml.
_RUNTIME_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"


def _runtime_config() -> dict[str, Any]:
    """Read the runtime parameters from config/config.yaml.

    This is the same file the platform registry loads and passes as
    Graph(config=...); the standalone server (src/api/server.py) reads it here
    so the deployed agent and a registry-loaded agent see identical
    configuration. Returns an empty dict — never raises — when the file is
    absent, unreadable, not valid YAML, or not a mapping (the graph then runs
    on its built-in defaults).
    """
    try:
        import yaml

        loaded = yaml.safe_load(_RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return loaded


def _config_number(value: Any, lo: float, hi: float) -> Optional[float]:
    """Validate a declared numeric setting: a real number, finite, within [lo, hi].

    Bools, strings, non-numerics, NaN/Infinity, and out-of-range values return
    None (the caller then keeps the node's built-in default). A non-finite
    threshold is the dangerous case: NaN comparisons are always False, so a
    NaN severity band would classify every claim as low exposure.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    if not math.isfinite(parsed) or not lo <= parsed <= hi:
        return None
    return parsed


class ClaimSummaryGraphNode(GraphNode):
    """GraphNode subclass assigned to the `main` slot of InsuranceClaimCaseSummarizationAgent.

    Wraps DomainWorkflowGraph (inner Cat 2 BaseGraph).
    Called by the AgentBaseGraph backbone after pre_process and before post_process.

    Contracts:
      get_subgraph()  — instantiate and return DomainWorkflowGraph
      extract_input() — pull validated_input from outer state; bridge input_context
      merge_output()  — map sub_result fields into outer state delta (changed keys only)
      error_strategy  — "propagate": re-raise inner errors as SubgraphError (fail-fast)
    """

    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = False

    def get_subgraph(self) -> "DomainWorkflowGraph":
        """Instantiate and return the inner domain workflow graph.

        DomainWorkflowGraph is imported lazily (inside the method) to avoid
        circular-import risk at module load time and to match the Cat 2 pattern.
        """
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        return DomainWorkflowGraph(config=self._parent_config())

    def execute(self, state: AgentState) -> dict[str, Any]:
        """Skip the inner graph when the request was already found unacceptable.

        A request declined by pre_process has no validated input to act on, so
        running the inner graph would only produce a second, vaguer reason for
        the same rejection - and overwrite the specific one already settled.
        """
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        result: dict[str, Any] = super().execute(state)
        return result

    def extract_input(self, state: AgentState) -> str:
        """Return the string input passed into inner_graph.invoke().

        PreProcessNode validates and normalises the raw user_input and writes
        the result to validated_input.  Prefer that; fall back to user_input
        if validated_input is absent (e.g. in unit tests).

        Also bridges the caller's input_context to the inner graph:
        GraphNode.execute() does not forward input_context on subgraph.invoke(),
        and extract_input is the last hook in this repo's code that sees the
        outer state before the inner invoke — see src/graph/context_bridge.py.
        """
        set_caller_input_context(cast("dict[str, Any] | None", state.get("input_context")))
        return cast(str, state.get("validated_input", state.get("user_input", "")))

    def merge_output(self, state: AgentState, sub_result: dict[str, Any]) -> dict[str, Any]:
        """Map inner graph sub_result back into the outer state delta.

        sub_result is the dict returned by DomainWorkflowGraph.get_output().
        Returns ONLY changed keys — never the full state.

        Key coupling (designed together with DomainWorkflowGraph.get_output()):
          Inner get_output() emits  → "claim_reference", "claim_summary",
                                       "redacted_sections", "pii_findings",
                                       "damages_assessment", "status"
          This merge_output() reads → sub_result.get(...) for each of these keys.

        The raw generated sections stay inside the inner graph: only the
        rendered, PII-masked, on-grid form crosses the boundary, so every
        representation a caller can receive carries the same figures.

        PostProcessNode (outer post_process) reads claim_summary from state to
        apply the output boundary and set formatted_output.
        """
        return {
            # Outer reason wins: a reason settled before the inner run is the real
            # one, and a plain sub_result.get() would erase it.
            "error_code": state.get("error_code") or sub_result.get("error_code", ""),
            "claim_reference": sub_result.get("claim_reference"),
            "claim_summary": sub_result.get("claim_summary"),
            "redacted_sections": sub_result.get("redacted_sections"),
            "pii_findings": sub_result.get("pii_findings"),
            "damages_assessment": sub_result.get("damages_assessment"),
            "result": sub_result.get("claim_summary"),
            "status": sub_result.get("status"),
        }

    def _parent_config(self) -> dict[str, Any]:
        """Forward the declared runtime settings to the inner graph.

        Reads config/config.yaml (see _runtime_config) and returns a flat
        settings dict, which DomainWorkflowGraph.register_nodes() injects into
        the domain node constructors. Constructor injection is the config route
        because the node contract is ``execute(self, state) -> dict`` — a node
        may not take a per-invocation config argument.

        Every forwarded value is validated here (type, finiteness, range) so a
        malformed configuration file can neither crash graph construction nor
        weaken the exposure classification: a non-finite severity band would
        compare False against every exposure and report every claim as low.
        Invalid or absent keys are simply not forwarded; each node then falls
        back to its module default.
        """
        cfg = _runtime_config()
        damages_raw = cfg.get("damages")
        damages: dict[str, Any] = damages_raw if isinstance(damages_raw, dict) else {}
        llm_raw = cfg.get("llm")
        llm: dict[str, Any] = llm_raw if isinstance(llm_raw, dict) else {}

        declared: dict[str, Any] = {}

        for key in ("moderate_threshold", "high_threshold", "severe_threshold"):
            band = _config_number(damages.get(key), 0.0, 1_000_000_000_000.0)
            if band is not None:
                declared[key] = band

        template = llm.get("system_prompt_template")
        if isinstance(template, str) and template:
            declared["system_prompt_template"] = template

        temperature = _config_number(llm.get("temperature"), 0.0, 2.0)
        if temperature is not None:
            declared["temperature"] = temperature

        max_tokens = _config_number(llm.get("max_tokens"), 1, 100_000)
        if max_tokens is not None and max_tokens == int(max_tokens):
            declared["max_tokens"] = int(max_tokens)

        return declared


class InsuranceClaimCaseSummarizationAgent(AgentBaseGraph):
    """Outer graph for INS-C2-004 (Cat 2 — Chat pattern).

    Inherits AgentBaseGraph directly (framework base class). Domain logic is
    fully encapsulated in ClaimSummaryGraphNode (main slot), which delegates to
    DomainWorkflowGraph (inner BaseGraph).

    Backbone (fixed — identical to Cat 1):
        START → initialize → pre_process → main → post_process → finalize → END

    register_nodes() and get_output() are the only overrides:
      - super().register_nodes() fills: initialize, finalize (framework defaults)
      - pre_process: PreProcessNode    (VERIFIED_EXTERNAL — trust gate)
      - main:        ClaimSummaryGraphNode (delegates to DomainWorkflowGraph)
      - post_process: PostProcessNode  (ANONYMOUS — output boundary)
      - get_output(): surfaces the domain result

    add_edges() is NOT overridden — backbone wiring belongs to the framework.

    Runtime configuration: the platform registry loads config/config.yaml and
    passes it as Graph(config=...); the standalone server does the same via
    _runtime_config(). AgentBaseGraph itself consumes max_retry from that
    config (retry routing), so the declared value is live in both deployments.

    Class name MUST match config/agent.yaml `class:` field exactly.
    server.py imports this as `Graph` via the alias below.
    """

    @property
    def name(self) -> str:
        """Agent identifier registered with the platform registry."""
        return "InsuranceClaimCaseSummarizationAgent"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        """Fill all 5 backbone slots.

        super().register_nodes() MUST be called first — it injects the
        framework's default InitializeNode (sets schema_version, session_id,
        trust_level) and FinalizeNode (builds response_metadata, total_time_ms).
        """
        super().register_nodes()  # fills: initialize, finalize

        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = ClaimSummaryGraphNode()
        self._nodes["post_process"] = PostProcessNode()

    # add_edges() is NOT overridden — backbone wiring belongs to the framework.

    def _security_gate_output(self, content: str) -> Optional[str]:
        """Canonical output-gate entry point on the agent class.

        Delegates to the module-level scanner in post_process_node so the node
        and the agent can never diverge on what a credential looks like.
        """
        return _security_gate_output(content)

    def get_output(self, state: AgentState) -> dict[str, Any]:
        """Surface the domain claim-case summary on the outer invoke() return.

        AgentBaseGraph.get_output() returns only the minimal
        ``{output, status, trace_id, correlation_id, node_history}`` envelope.
        On the compiled outer-graph success path that dropped the structured
        domain result — the rendered sections, the redaction findings and the
        damages aggregates — so a successful summarization returned None for
        every domain field. This override extends the base envelope so a
        successful invocation actually returns the domain result.

        The output invariant is preserved on every representation:
          * ``formatted_output`` is what the gated PostProcessNode produced,
            so it is the only caller-facing value on either path (on a block
            it is the gate's own content-free withholding notice).
          * ``result`` is surfaced ONLY on the gated success path. On any
            outcome that does not reach post_process — a non-success inner
            result forwarded by merge_output(), a node that raised, a terminal
            status routed straight to finalize — ``state["result"]`` is still
            the PRE-GATE summary merge_output() copied out of the inner graph.
            The framework base resolves its ``output`` key as
            ``formatted_output or result`` without consulting status, so that
            fallback is re-resolved here as well: an absent gate output stays
            absent and never becomes the un-gated document.
          * ``redacted_sections`` is the RENDERED section set (PII-masked and
            on-grid — see output_format_node); the raw generated sections
            never leave the inner graph.
          * The structured fields are surfaced ONLY when the gate passed
            (status == SUCCESS). On any non-success outcome — including a
            credential block — they are withheld (None): the section
            redaction scrubs policyholder PII but is not a credential gate,
            so it must never ship on a blocked invoke.
        """
        output = cast(dict[str, Any], super().get_output(state))
        succeeded = state.get("status") == AgentStatus.SUCCESS.value

        formatted_output = state.get("formatted_output")
        output["formatted_output"] = formatted_output
        if succeeded:
            output["result"] = state.get("result")
            output["claim_summary"] = formatted_output or state.get("result")
        else:
            # `result` is the PRE-GATE summary on every path that did not reach
            # the output gate. It is never the caller-facing value on a
            # non-success outcome, and the base envelope's `formatted_output or
            # result` fallback is re-resolved without it.
            output["result"] = None
            output["claim_summary"] = None
            output["output"] = formatted_output or None

        # Structured domain result — surfaced only on the gated success path.
        output["claim_reference"] = state.get("claim_reference") if succeeded else None
        output["redacted_sections"] = state.get("redacted_sections") if succeeded else None
        output["pii_findings"] = state.get("pii_findings") if succeeded else None
        output["damages_assessment"] = state.get("damages_assessment") if succeeded else None
        return output


# Alias for backward compat (server.py imports Graph).
# Class name InsuranceClaimCaseSummarizationAgent matches config/agent.yaml class: field.
Graph = InsuranceClaimCaseSummarizationAgent
