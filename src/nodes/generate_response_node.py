"""AgentCore Platform v1.0"""

# INS-C2-004 — GenerateResponseNode
# Inner domain node 3: generate the structured claim-case summary sections.
#
# The section text is synthesised deterministically from the assembled
# context: document narratives are quoted from the claim file, and the
# Damages Assessment / Recommended Action sections are computed from the
# damages aggregates BuildContextNode derived from the caller's estimate
# table. No LLM is invoked — the framework ships no LLM client, so the
# manifest's llm settings (system_prompt_template / temperature / max_tokens)
# are forwarded into this node's constructor and left unread until a
# live-LLM build renders prompts/claim_summary.j2 here.
#
# Sections produced:
#   1. case_overview
#   2. incident_facts
#   3. documentation_summary
#   4. injury_medical
#   5. damages_assessment
#   6. recommended_action
#
# Inner node — ANONYMOUS trust.
# Returns ONLY the state keys this node writes (partial-dict contract).

import logging
from typing import Any, ClassVar, Dict, List, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json, to_json

logger = logging.getLogger(__name__)

# Severity → the routing sentence the Recommended Action section reports.
_SEVERITY_ROUTING: Dict[str, str] = {
    "low": "Exposure is within the low band: settle through standard fast-track handling.",
    "moderate": "Exposure is in the moderate band: assign a claims handler for coverage determination.",
    "high": "Exposure is in the high band: assign a senior claims handler and open a reserve review.",
    "severe": "Exposure is in the severe band: escalate to the major-loss unit before any payment.",
}


def _money(value: Any) -> str:
    """Render a published aggregate. Figures are already on the external grid."""
    if value is None:
        return "n/a"
    return f"{int(value):,d}"


def _join_docs(grouped: Dict[str, List[str]], dtype: str) -> str:
    """Join all rendered snippets of a document type into one block."""
    items = grouped.get(dtype, [])
    return "\n".join(f"  - {snippet}" for snippet in items if snippet)


def _section_case_overview(ctx: Dict[str, Any]) -> str:
    """Generate the Case Overview section."""
    lines = [
        f"Claim Reference:   {ctx.get('claim_reference', 'unknown')}",
        f"Claim Type:        {ctx.get('claim_type', 'unknown')}",
        f"Date of Loss:      {ctx.get('date_of_loss') or 'not recorded'}",
        f"Case Status:       {ctx.get('status', 'open')}",
        f"Claimant Details:  {'on file' if ctx.get('claimant_on_file') else 'not on file'}",
        f"Documents on File: {ctx.get('total_documents', 0)}",
    ]
    return "\n".join(lines)


def _section_incident_facts(grouped: Dict[str, List[str]]) -> str:
    """Generate the Incident Facts section (from FNOL)."""
    fnol = _join_docs(grouped, "FNOL")
    if not fnol:
        return "No First Notice of Loss (FNOL) document on file; incident facts unavailable."
    return "Derived from First Notice of Loss (FNOL):\n" + fnol


def _section_documentation_summary(ctx: Dict[str, Any]) -> str:
    """Generate the Documentation Summary section."""
    counts: Dict[str, int] = ctx.get("doc_counts", {}) or {}
    if not counts:
        return "No documents catalogued."
    lines = [f"  - {dtype}: {count}" for dtype, count in sorted(counts.items())]
    return "Documents by type:\n" + "\n".join(lines)


def _section_injury_medical(grouped: Dict[str, List[str]]) -> str:
    """Generate the Injury / Medical section (from medical reports)."""
    med = _join_docs(grouped, "medical_report")
    if not med:
        return "No medical report on file; no injury/medical findings to summarise."
    return "Summary of medical report(s):\n" + med


def _section_damages_assessment(ctx: Dict[str, Any]) -> str:
    """Generate the Damages Assessment section.

    When the caller supplied an estimate table the section reports the
    computed aggregates only — never the individual line amounts. Otherwise
    it falls back to whatever the claim documents say.
    """
    grouped: Dict[str, List[str]] = ctx.get("grouped_documents", {}) or {}
    damages: Optional[Dict[str, Any]] = ctx.get("damages")

    parts: List[str] = []
    if damages:
        figures = [
            f"  Estimated total:  {_money(damages.get('estimated_total'))}",
            f"  Deductible:       {_money(damages.get('deductible'))}",
            f"  Net exposure:     {_money(damages.get('net_exposure'))}",
        ]
        if damages.get("policy_limit") is not None:
            limit_note = " (limit reached)" if damages.get("limit_applied") else ""
            figures.append(f"  Policy limit:     {_money(damages.get('policy_limit'))}{limit_note}")
        if damages.get("reserve_variance") is not None:
            figures.append(f"  Reserve variance: {_money(damages.get('reserve_variance'))}")
        figures.append(f"  Exposure band:    {damages.get('severity', 'unknown')}")
        labels = damages.get("line_labels") or []
        figures.append(f"  Estimate lines:   {damages.get('line_count', 0)} ({', '.join(labels) or 'none'})")
        parts.append("Computed from the submitted estimate table (aggregates only):\n" + "\n".join(figures))

    adjuster = _join_docs(grouped, "adjuster_note")
    if adjuster:
        parts.append("Adjuster notes:\n" + adjuster)
    estimate = _join_docs(grouped, "estimate")
    if estimate:
        parts.append("Repair/loss estimates:\n" + estimate)

    if not parts:
        return "No estimate table, adjuster notes or estimates on file; damages assessment pending."
    return "\n\n".join(parts)


def _section_recommended_action(ctx: Dict[str, Any]) -> str:
    """Generate the Recommended Action section.

    Missing core documentation is always reported first; the exposure band,
    when it could be computed, then determines the routing decision.
    """
    counts: Dict[str, int] = ctx.get("doc_counts", {}) or {}
    damages: Optional[Dict[str, Any]] = ctx.get("damages")

    missing: List[str] = []
    if not counts.get("FNOL"):
        missing.append("FNOL")
    if str(ctx.get("claim_type")) == "medical" and not counts.get("medical_report"):
        missing.append("medical report")
    if not counts.get("adjuster_note"):
        missing.append("adjuster assessment")

    parts: List[str] = []
    if missing:
        parts.append("Request the following before adjudication: " + ", ".join(missing) + ".")
    else:
        parts.append("All core documentation is present.")

    if damages:
        parts.append(_SEVERITY_ROUTING.get(str(damages.get("severity")), _SEVERITY_ROUTING["moderate"]))
    else:
        parts.append("No damages figures were submitted: route to a claims handler for manual assessment.")

    return " ".join(parts)


class GenerateResponseNode(FunctionNode):
    """Generate the 6 structured claim-case summary sections.

    Deterministic synthesis from the assembled context; a live-LLM build
    renders prompts/claim_summary.j2 (the manifest llm.system_prompt_template)
    and invokes the platform LLM here.

    Inner node — ANONYMOUS trust (see module docstring).

    Constructor config (see __init__):
        system_prompt_template / temperature / max_tokens — declared LLM
        settings, forwarded for a live-LLM build; unread by this build.

    Input state keys:
        assembled_context: str  — JSON-serialised grouped context + damages

    Output state keys (partial dict):
        summary_sections: str  — JSON-serialised section dict
        status:           str
        error_log:        list[str]  (only on ERROR)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        """Bind the declared generation settings at construction time.

        The node contract is ``execute(self, state) -> dict`` — no extra
        per-invocation parameter — so declared settings arrive through the
        constructor, injected by DomainWorkflowGraph.register_nodes().
        """
        super().__init__()
        self._settings: Dict[str, Any] = dict(config or {})

    def execute(self, state: AgentState, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        # A reason settled earlier in the run is the real one: pass it through
        # untouched instead of doing work on input that was already declined.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        ctx: Dict[str, Any] = from_json(state.get("assembled_context"), {})

        if not ctx:
            logger.error("GenerateResponseNode: assembled_context missing in state")
            emit_trace_event("generate_response_failed", {"reason": "missing_assembled_context"}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["GenerateResponseNode: assembled_context missing in state"],
            }

        # ── Generate all sections ─────────────────────────────────────────────
        grouped: Dict[str, List[str]] = ctx.get("grouped_documents", {}) or {}
        sections: Dict[str, str] = {
            "case_overview": _section_case_overview(ctx),
            "incident_facts": _section_incident_facts(grouped),
            "documentation_summary": _section_documentation_summary(ctx),
            "injury_medical": _section_injury_medical(grouped),
            "damages_assessment": _section_damages_assessment(ctx),
            "recommended_action": _section_recommended_action(ctx),
        }

        logger.info("GenerateResponseNode: sections=%d", len(sections))
        emit_trace_event(
            "generate_response_complete",
            {
                "claim_reference": ctx.get("claim_reference", "unknown"),
                "section_count": len(sections),
                "section_keys": sorted(sections.keys()),
            },
            state,
        )

        return {
            "summary_sections": to_json(sections),
            "status": AgentStatus.SUCCESS.value,
        }
