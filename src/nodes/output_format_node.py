"""AgentCore Platform v1.0"""

# INS-C2-004 — OutputFormatNode (inner domain node 5, last in DomainWorkflowGraph)
# Assembles the final claim-case summary document from the redacted sections
# and the redaction findings. This is the last inner node — it produces the
# claim_summary string the outer PostProcessNode gates.
#
# Output schema note: the external document expresses monetary figures in
# units of 1,000 in the claim's own currency. This node RENDERS on that grid
# (the outer output gate independently ENFORCES it — see post_process_node.py)
# and republishes the sections in their rendered form, so the structured
# result a caller receives and the document text always express identical,
# on-grid values. The detection reuses the gate's own grammar, so renderer and
# gate can never drift apart.
#
# Rendering on the grid happens HERE, after the domain PII redaction step —
# never before it. Rewriting digits ahead of a pattern scan destroys the shape
# the scan recognises (see the layer-order note in post_process_node.py).
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

from src.nodes.post_process_node import _NUM_TOKEN_RE, _enforce_precision_outside_reference
from src.schemas.state import from_json, to_json

logger = logging.getLogger(__name__)

# Section order for the final summary.
_SECTION_ORDER = [
    "case_overview",
    "incident_facts",
    "documentation_summary",
    "injury_medical",
    "damages_assessment",
    "recommended_action",
]

# Human-readable section headers.
_SECTION_HEADERS: Dict[str, str] = {
    "case_overview": "1. Case Overview",
    "incident_facts": "2. Incident Facts",
    "documentation_summary": "3. Documentation Summary",
    "injury_medical": "4. Injury / Medical Summary",
    "damages_assessment": "5. Damages Assessment",
    "recommended_action": "6. Recommended Action",
}

_SEPARATOR = "=" * 72
_SUBSEP = "-" * 72

# Printed when the document renders monetary figures.
_SCHEMA_NOTE = "Monetary figures in this summary are expressed in units of 1,000."


def render_sections(sections: Dict[str, str], reference: Optional[str]) -> tuple[Dict[str, str], int]:
    """Render each redacted section onto the external monetary grid.

    Returns (rendered_sections, snapped_count). The claim reference is a
    structural identifier and is held out of the numeric rewrite by exact
    value (see post_process_node._enforce_precision_outside_reference); the
    same hold-out is applied there, so both layers agree.
    """
    rendered: Dict[str, str] = {}
    snapped_total = 0
    for name, text in sections.items():
        rendered_text, snapped = _enforce_precision_outside_reference(str(text), reference)
        rendered[name] = rendered_text
        snapped_total += snapped
    return rendered, snapped_total


def _assemble_summary(
    reference: str,
    sections: Dict[str, str],
    findings: List[Dict[str, Any]],
) -> str:
    """Assemble the full claim-case summary from rendered sections + findings."""
    lines = [
        _SEPARATOR,
        "INSURANCE CLAIM CASE SUMMARY",
        f"Claim Reference: {reference}",
        _SEPARATOR,
        "",
    ]

    for key in _SECTION_ORDER:
        header = _SECTION_HEADERS.get(key, key.replace("_", " ").title())
        content = sections.get(key, "(Section not generated)")
        lines.append(header)
        lines.append(_SUBSEP)
        lines.append(content)
        lines.append("")

    # Append the redaction trailer.
    total = sum(int(f.get("count", 0)) for f in findings)
    lines += [
        _SEPARATOR,
        "PII HANDLING NOTE",
        _SUBSEP,
        f"  Policyholder PII redactions applied: {total}",
        f"  Redaction findings: {len(findings)}",
        "  Personal data is masked in this summary and not retained beyond session.",
    ]

    # The note is printed only when the document actually renders money. The
    # claim reference is removed from the probe text first: it is a structural
    # identifier that can carry a long digit run, and it is held out of the
    # grid, so it must not be what makes the note appear.
    body = "\n".join(lines).replace(reference, "")
    if _NUM_TOKEN_RE.search(body):
        lines.append(f"  {_SCHEMA_NOTE}")
    lines.append(_SEPARATOR)
    return "\n".join(lines)


class OutputFormatNode(FunctionNode):
    """Assemble the final claim-case summary document (inner domain node).

    Reads the redacted sections and the redaction findings from State, renders
    them onto the external monetary grid, assembles the full claim-case
    summary text, and writes it to claim_summary (and result) for the outer
    PostProcessNode. The rendered sections are republished under
    redacted_sections so the structured result and the document agree.

    Inner node — ANONYMOUS trust (see module docstring).

    Input state keys:
        redacted_sections: str  — JSON-serialised PII-masked section dict
        pii_findings:      str  — JSON-serialised findings list
        claim_reference:   str  — inert reference rendered in the header

    Output state keys (partial dict):
        claim_summary:     str
        redacted_sections: str  — the rendered (on-grid) sections
        result:            str  (same as claim_summary — backbone convention)
        status:            str
        error_log:         list[str]  (only on ERROR)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        # A reason settled earlier in the run is the real one: pass it through
        # untouched instead of doing work on input that was already declined.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        sections: Dict[str, str] = from_json(state.get("redacted_sections"), {})
        findings: List[Dict[str, Any]] = from_json(state.get("pii_findings"), [])
        claim_case: Dict[str, Any] = from_json(state.get("claim_case"), {})

        reference = state.get("claim_reference") or claim_case.get("claim_reference") or "unknown"

        if not sections:
            logger.error("OutputFormatNode: redacted_sections missing in state")
            emit_trace_event(
                "output_format_failed",
                {"reason": "missing_redacted_sections", "claim_reference": reference},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["OutputFormatNode: redacted_sections missing in state"],
            }

        rendered, snapped = render_sections(sections, reference)
        summary = _assemble_summary(reference, rendered, findings)

        logger.info(
            "OutputFormatNode: summary_chars=%d sections=%d grid_snaps=%d",
            len(summary),
            len(rendered),
            snapped,
        )
        emit_trace_event(
            "output_format_complete",
            {
                "claim_reference": reference,
                "summary_length": len(summary),
                "section_count": len(rendered),
                "grid_snaps": snapped,
            },
            state,
        )

        return {
            "claim_summary": summary,
            "redacted_sections": to_json(rendered),
            "result": summary,
            "status": AgentStatus.SUCCESS.value,
        }
