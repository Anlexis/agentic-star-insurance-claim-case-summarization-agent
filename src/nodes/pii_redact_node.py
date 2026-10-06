"""AgentCore Platform v1.0"""

# INS-C2-004 — PiiRedactNode
# Inner domain node 4 (domain-fit): redact policyholder PII from the generated
# summary sections so that personal data does not persist in State beyond the
# session.
#
# This is a DOMAIN content-filtering step, NOT the credential output gate —
# that gate is _security_gate_output() in post_process_node.py. Here we scrub
# personally-identifiable information (emails, phone numbers, national IDs,
# payment-card and social-security-style numbers) that legitimately may appear
# in claim documents but must not leave the pipeline in the clear.
#
# redact_pii() is the single definition of the pattern set: the output
# boundary re-runs it as a residual sweep, and it must always run BEFORE any
# numeric rewriting, never after (see post_process_node.py).
#
# Inner node — ANONYMOUS trust.
# Returns ONLY the state keys this node writes (partial-dict contract).

import logging
import re
from typing import Any, ClassVar, Dict, List, Optional, Pattern, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json, to_json

logger = logging.getLogger(__name__)

# PII patterns to redact from the summary sections. Ordered so that the most
# specific / longest patterns run first (payment card / IDs before phone).
#
# The phone pattern deliberately requires a leading "+" (international) or a
# word-boundary "0" (Japanese national format) so it does NOT clobber claim
# references (e.g. "clm-2026-004567") or ISO dates (e.g. "2026-07-01"), which
# start with other digits. Its body accepts spaces and tabs but NOT line
# breaks: a phone number never spans lines, and allowing newlines let one
# match run across a rule line into unrelated content.
_PII_PATTERNS: List[Tuple[Pattern[str], str]] = [
    (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "email"),
    (re.compile(r"\b(?:\d[ \-]?){13,16}\b"), "payment_card"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "ssn"),
    (re.compile(r"\b\d{4}[ \-]?\d{4}[ \-]?\d{4}\b"), "national_id"),
    (re.compile(r"(?:\+|\b0)\d[\d\-\t ().]{7,}\d"), "phone"),
]


def redact_pii(text: str) -> Tuple[str, Dict[str, int]]:
    """Redact PII in a text block.

    Returns (redacted_text, {kind: count}). This is the single definition of
    the pattern sweep — the output boundary calls it too.
    """
    counts: Dict[str, int] = {}
    redacted = text
    for pattern, kind in _PII_PATTERNS:
        # Count matches on the progressively-redacted text so overlapping
        # patterns do not double-count the same span.
        found = pattern.findall(redacted)
        if found:
            counts[kind] = counts.get(kind, 0) + len(found)
            redacted = pattern.sub(f"[REDACTED:{kind}]", redacted)
    return redacted, counts


class PiiRedactNode(FunctionNode):
    """Redact policyholder PII from the generated summary sections.

    Scans each generated section for emails, phone numbers, national-ID,
    payment-card and social-security-style numbers, replaces them with a
    typed redaction marker, and records a per-section findings list.
    Distinct from the credential gate in post_process — this is domain PII
    scrubbing.

    Inner node — ANONYMOUS trust (see module docstring).

    Input state keys:
        summary_sections: str  — JSON-serialised section dict

    Output state keys (partial dict):
        redacted_sections: str  — JSON-serialised redacted section dict
        pii_findings:      str  — JSON-serialised list of findings
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
        sections: Dict[str, str] = from_json(state.get("summary_sections"), {})

        if not sections:
            logger.error("PiiRedactNode: summary_sections missing in state")
            emit_trace_event(
                "pii_redact_failed",
                {"reason": "missing_summary_sections"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PiiRedactNode: summary_sections missing in state"],
            }

        redacted_sections: Dict[str, str] = {}
        findings: List[Dict[str, Any]] = []
        total_redactions = 0

        for name, text in sections.items():
            redacted, counts = redact_pii(str(text))
            redacted_sections[name] = redacted
            for kind, count in counts.items():
                total_redactions += count
                findings.append({"section": name, "kind": kind, "count": count})

        logger.info(
            "PiiRedactNode: sections=%d redactions=%d finding_types=%d",
            len(redacted_sections),
            total_redactions,
            len(findings),
        )
        emit_trace_event(
            "pii_redact_complete",
            {
                "section_count": len(redacted_sections),
                "total_redactions": total_redactions,
                "finding_kinds": sorted({f["kind"] for f in findings}),
            },
            state,
        )

        return {
            "redacted_sections": to_json(redacted_sections),
            "pii_findings": to_json(findings),
            "status": AgentStatus.SUCCESS.value,
        }
