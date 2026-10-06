"""AgentCore Platform v1.0"""

# INS-C2-004 — BuildContextNode
# Inner domain node 2: assemble the validated claim documents into a
# structured, generation-ready context grouped by document type, and compute
# the claim's damages aggregates from the caller-supplied damages data.
#
# This is the Chat-pattern "context builder": it does not call an LLM; it
# organises the source documents (FNOL, medical reports, adjuster notes,
# photos, …) into a compact structure the GenerateResponseNode consumes, and
# turns the caller's estimate table into the aggregate figures the Damages
# Assessment section reports.
#
# Caller data is re-validated here against the SAME contract the ingest
# boundary applies (validate_input_context in pre_process_node), so a direct
# inner-graph invocation gets the identical fail-closed rule. When no damages
# data is supplied the node degrades to the document-derived baseline.
#
# Inner node — ANONYMOUS trust.
# Returns ONLY the state keys this node writes (partial-dict contract).

import logging
import re
from typing import Any, ClassVar, Dict, List, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.progress import emit_progress
from src.services.failure_message import INPUT_REJECTED

from src.nodes.pre_process_node import validate_input_context
from src.schemas.state import finite_in_range, from_json, to_json

logger = logging.getLogger(__name__)

# Per-document character budget when assembling context (keeps the generation
# step bounded; a live-LLM build would chunk/retrieve instead).
_MAX_DOC_CHARS = 2000

# Control characters (except tab/newline) never reach the rendered summary.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Approved external precision: every published monetary aggregate is
# expressed in units of 1,000 (the output boundary independently enforces the
# same grid on the assembled document — see post_process_node.py).
_EXTERNAL_ROUND_UNIT = 1000

# Default exposure severity bands. Operators override them in
# config/config.yaml (damages.*); a malformed or out-of-order override is not
# adopted, so a bad configuration can never silently downgrade a severe claim.
_DEFAULT_MODERATE_THRESHOLD = 100_000.0
_DEFAULT_HIGH_THRESHOLD = 1_000_000.0
_DEFAULT_SEVERE_THRESHOLD = 10_000_000.0

# Threshold sanity bounds for a declared override.
_THRESHOLD_MIN = 0.0
_THRESHOLD_MAX = 1_000_000_000_000.0


def _render_document(doc: Dict[str, Any]) -> str:
    """Render one document to a bounded, control-character-free snippet."""
    content = _CONTROL_CHARS_RE.sub("", str(doc.get("content", ""))).strip()
    if len(content) > _MAX_DOC_CHARS:
        content = content[:_MAX_DOC_CHARS] + " …[truncated]"
    return content


def snap_to_grid(value: float) -> int:
    """Round a monetary figure onto the approved external grid (units of 1,000)."""
    return int(round(value / _EXTERNAL_ROUND_UNIT) * _EXTERNAL_ROUND_UNIT)


def classify_severity(net_exposure: float, thresholds: Dict[str, float]) -> str:
    """Classify net exposure into a severity band."""
    if net_exposure >= thresholds["severe"]:
        return "severe"
    if net_exposure >= thresholds["high"]:
        return "high"
    if net_exposure >= thresholds["moderate"]:
        return "moderate"
    return "low"


def _resolved_thresholds(config: Optional[Dict[str, Any]]) -> Dict[str, float]:
    """Resolve the severity bands from the declared configuration.

    Every declared value must be a finite number in range AND the three bands
    must stay strictly ascending; otherwise the built-in defaults are kept in
    full. Partial adoption is deliberately not allowed — a half-applied band
    set is how a "severe" claim quietly becomes "moderate".
    """
    defaults = {
        "moderate": _DEFAULT_MODERATE_THRESHOLD,
        "high": _DEFAULT_HIGH_THRESHOLD,
        "severe": _DEFAULT_SEVERE_THRESHOLD,
    }
    if not isinstance(config, dict):
        return defaults

    declared: Dict[str, float] = {}
    for band, key in (("moderate", "moderate_threshold"), ("high", "high_threshold"), ("severe", "severe_threshold")):
        parsed = finite_in_range(config.get(key), _THRESHOLD_MIN, _THRESHOLD_MAX)
        if parsed is None:
            return defaults
        declared[band] = parsed

    if not declared["moderate"] < declared["high"] < declared["severe"]:
        logger.warning("BuildContextNode: declared severity bands are not ascending — keeping defaults")
        return defaults
    return declared


def compute_damages(context: Dict[str, Any], thresholds: Dict[str, float]) -> Optional[Dict[str, Any]]:
    """Compute the published damages aggregates from validated caller data.

    Returns None when the caller supplied no estimate table (the summary then
    falls back to the document-derived narrative). Published figures are
    snapped onto the external grid here, so the structured result and the
    rendered document express identical, on-grid values; individual line
    amounts are never published — only their labels and the aggregates.
    """
    lines: List[Dict[str, Any]] = context.get("estimate_lines") or []
    if not lines:
        return None

    exact_total = sum(float(line["amount"]) for line in lines)
    deductible = float(context.get("deductible", 0.0))
    policy_limit = context.get("policy_limit")
    reserve_amount = context.get("reserve_amount")

    exact_net = max(exact_total - deductible, 0.0)
    limit_applied = False
    if policy_limit is not None and exact_net > float(policy_limit):
        exact_net = float(policy_limit)
        limit_applied = True

    assessment: Dict[str, Any] = {
        "estimated_total": snap_to_grid(exact_total),
        "deductible": snap_to_grid(deductible),
        "net_exposure": snap_to_grid(exact_net),
        "policy_limit": snap_to_grid(float(policy_limit)) if policy_limit is not None else None,
        "limit_applied": limit_applied,
        "reserve_variance": (snap_to_grid(exact_total - float(reserve_amount)) if reserve_amount is not None else None),
        "severity": classify_severity(exact_net, thresholds),
        "line_labels": sorted({str(line["label"]) for line in lines}),
        "line_count": len(lines),
    }
    return assessment


class BuildContextNode(FunctionNode):
    """Assemble the claim documents into a structured context.

    Groups the normalised documents by canonical type, renders each to a
    bounded text snippet, and computes the claim's damages aggregates from the
    caller's validated damages data. Produces assembled_context — a dict keyed
    by document type with a rendered-snippet list per type, plus roll-up
    counters and the damages aggregates the generation step reports.

    Inner node — ANONYMOUS trust (see module docstring).

    Constructor config (see __init__):
        moderate_threshold / high_threshold / severe_threshold — exposure
        severity bands; absent or malformed values keep the module defaults.

    Input state keys:
        claim_case:    str   — JSON-serialised normalised claim payload
        input_context: dict  — optional caller damages data; re-validated here

    Output state keys (partial dict):
        assembled_context:  str  — JSON-serialised grouped context
        damages_assessment: str  — JSON-serialised aggregates (None when absent)
        status:             str
        error_log:          list[str]  (only on ERROR)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        """Bind the declared severity bands at construction time.

        The node contract is ``execute(self, state) -> dict`` — no extra
        per-invocation parameter — so declared settings arrive through the
        constructor, injected by DomainWorkflowGraph.register_nodes().
        """
        super().__init__()
        self._thresholds = _resolved_thresholds(config)

    def execute(self, state: AgentState, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        claim_case: Dict[str, Any] = from_json(state.get("claim_case"), {})

        if not claim_case:
            logger.error("BuildContextNode: claim_case missing in state")
            emit_trace_event("build_context_failed", {"reason": "missing_claim_case"}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["BuildContextNode: claim_case missing in state"],
            }

        # ── Caller damages data (same contract as the ingest boundary) ────────
        caller_context, context_error = validate_input_context(state.get("input_context"))
        if context_error is not None:
            logger.warning("BuildContextNode: caller damages data rejected")
            emit_trace_event("build_context_failed", {"reason": "invalid_input_context"}, state)
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": [f"BuildContextNode: {context_error}"],
            }

        claim_reference = claim_case.get("claim_reference", "unknown")
        documents: List[Dict[str, Any]] = claim_case.get("documents", []) or []

        # ── Group + render documents by type ──────────────────────────────────
        grouped: Dict[str, List[str]] = {}
        for doc in documents:
            dtype = str(doc.get("type", "other"))
            grouped.setdefault(dtype, []).append(_render_document(doc))

        doc_counts = {dtype: len(items) for dtype, items in grouped.items()}

        damages = compute_damages(caller_context, self._thresholds)

        assembled_context: Dict[str, Any] = {
            "claim_reference": claim_reference,
            "claim_type": claim_case.get("claim_type", "unknown"),
            "date_of_loss": claim_case.get("date_of_loss"),
            "status": claim_case.get("status", "open"),
            "claimant_on_file": bool(claim_case.get("claimant_on_file")),
            "channel": caller_context.get("channel", "unknown"),
            "grouped_documents": grouped,
            "doc_counts": doc_counts,
            "total_documents": len(documents),
            "damages": damages,
        }

        logger.info(
            "BuildContextNode: doc_types=%d total_docs=%d damages=%s",
            len(grouped),
            len(documents),
            "computed" if damages else "absent",
        )
        emit_trace_event(
            "build_context_complete",
            {
                "claim_reference": claim_reference,
                "document_types": sorted(grouped.keys()),
                "total_documents": len(documents),
                "damages_severity": damages["severity"] if damages else None,
            },
            state,
        )

        return {
            "assembled_context": to_json(assembled_context),
            "damages_assessment": to_json(damages),
            "status": AgentStatus.SUCCESS.value,
        }
