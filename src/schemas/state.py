"""AgentCore Platform v1.0"""

# State must be a flat TypedDict — never a Pydantic BaseModel. LangGraph
# checkpoints use msgpack serialization; Pydantic objects cause silent
# corruption. Extend AgentState with agent-specific fields only. Do NOT add
# credentials, secrets, or Pydantic models.
#
# INS-C2-004 — Insurance Claim Case Summarization Agent
# Two-layer nested Cat 2 graph: outer backbone (AgentBaseGraph) + inner
# domain workflow (BaseGraph). Fields below cover both layers.
#
# Serialization contract: all dict/list-valued fields are stored as JSON-
# serialized Optional[str]. Use to_json() / from_json() below at every
# producer and consumer node — one contract end-to-end. Never type a
# dict/list field as a bare dict/list; that causes msgpack serialization
# failures.

import json
import math
from typing import Any, Optional

from framework.schemas.agent_state import AgentState


def to_json(value: Any) -> Optional[str]:
    """Serialize a value to a JSON string for State storage."""
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def from_json(value: Optional[str], default: Any = None) -> Any:
    """Deserialize a JSON string from State storage."""
    if value is None:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def finite_in_range(value: Any, lo: float, hi: float) -> Optional[float]:
    """Parse a caller-controlled numeric: FINITE float within [lo, hi], else None.

    Rejects bools, non-numerics, and — critically — non-finite values:
    ``float()`` happily parses ``"NaN"`` / ``"Infinity"`` (and Python's json
    accepts bare ``NaN`` in a request body), and IEEE NaN comparisons are
    always False, which turns every threshold check into a silent FAIL-OPEN.
    Every caller-supplied number must come through here, and a rejection is
    reported by naming the FIELD — never by echoing the value back.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or not lo <= parsed <= hi:
        return None
    return parsed


class State(AgentState):
    """Flat TypedDict for INS-C2-004.

    All shared fields (user_input, status, session_id, node_history,
    error_log, hitl_*, etc.) are inherited from AgentState.

    dict/list fields use JSON-serialized Optional[str].
    formatted_output is NOT re-declared here — it is inherited from AgentState.
    """

    # ------------------------------------------------------------------
    # Outer layer — set by PreProcessNode (pre_process backbone)
    # ------------------------------------------------------------------

    # Validated and normalised JSON string of the claim-case payload.
    # Produced by PreProcessNode; consumed by inner InputValidateNode.
    validated_input: Optional[str]

    # JSON-serialised channel/request metadata dict (stored as str).
    # Shape: {"source": str, "channel": str}
    enriched_context: Optional[str]

    # ------------------------------------------------------------------
    # Inner layer — domain nodes (DomainWorkflowGraph)
    # ------------------------------------------------------------------

    # Inert, normalised claim reference (lowercase identifier). The one
    # identifier rendered into the external summary.
    claim_reference: Optional[str]

    # JSON-serialised normalised claim case (stored as str).
    # Shape: {claim_reference, claim_type, date_of_loss, status,
    #   claimant_on_file (bool), documents (list of {type, content}),
    #   doc_index (dict: type -> [indices])}
    claim_case: Optional[str]

    # JSON-serialised structured context grouped by document type (stored as
    # str). Shape: {doc_type: [rendered text, ...], ...} plus roll-up counters
    # and the computed damages aggregates used by the generation step.
    assembled_context: Optional[str]

    # JSON-serialised computed damages aggregates (stored as str). Shape:
    # {estimated_total, deductible, net_exposure, policy_limit, limit_applied,
    #  reserve_variance, severity, line_labels, line_count}. Aggregates only —
    #  never per-line amounts, and every figure already on the external grid.
    damages_assessment: Optional[str]

    # JSON-serialised summary section dict (stored as str). Keys are the 6
    # claim-summary sections: case_overview, incident_facts,
    # documentation_summary, injury_medical, damages_assessment,
    # recommended_action. Each value is the rendered text for that section.
    summary_sections: Optional[str]

    # JSON-serialised PII-redacted section dict (stored as str).
    # Same key shape as summary_sections, with policyholder PII masked.
    redacted_sections: Optional[str]

    # JSON-serialised list of PII redaction findings (stored as str).
    # Shape: [{"section": str, "kind": str, "count": int}, ...]
    pii_findings: Optional[str]

    # Final formatted claim-case summary document (plain text).
    # Assembled by inner OutputFormatNode from redacted_sections.
    claim_summary: Optional[str]

    # ------------------------------------------------------------------
    # Outer layer — set by PostProcessNode (post_process backbone)
    # ------------------------------------------------------------------

    # Primary result surfaced to the caller.
    # Set to the gated content after the output boundary passes.
    # formatted_output (from AgentState) is also set by PostProcessNode.
    result: Optional[str]

    # ------------------------------------------------------------------
    # Tracing / audit — framework-managed; do NOT write from node code
    # ------------------------------------------------------------------

    trace_id: Optional[str]
    correlation_id: Optional[str]
    error_code: Optional[str]
    # node_history inherited from AgentState
