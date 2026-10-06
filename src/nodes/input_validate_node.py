"""AgentCore Platform v1.0"""

# INS-C2-004 — InputValidateNode
# Inner domain node 1: domain-level validation of the claim-case payload.
#
# Distinct from PreProcessNode (trust + structural JSON check): this node
# applies domain-business rules — the claim reference is normalised to an
# inert identifier, claim type / case status / loss date are locked to
# closed vocabularies, documents are typed and capped, and a per-type
# document index is built for the downstream context builder.
#
# Every caller-supplied value that ends up in a STRUCTURED field of the
# rendered summary is locked here to an inert form; the only free text that
# reaches the summary is the document body being summarised, which is length-
# bounded, control-character stripped, PII-redacted and gated downstream.
#
# Inner node — ANONYMOUS trust (the outer PreProcessNode with
# VERIFIED_EXTERNAL already enforced trust; inner nodes must be ANONYMOUS so
# the outer invocation context passes through the GraphNode boundary without
# rejection).
#
# Returns ONLY the state keys this node writes (partial-dict contract).

import json
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

from src.schemas.state import to_json

logger = logging.getLogger(__name__)

# The claim reference is the one caller-supplied identifier rendered verbatim
# into the external summary, so it is locked to an inert lowercase form: it
# must start with a letter and use only [a-z0-9_-], 2-32 characters. Callers
# commonly send upper-case references ("CLM-2026-004567"); those are accepted
# and normalised by lower-casing. Lower-casing is load-bearing, not cosmetic:
# it guarantees the reference can never present as a currency-marked amount
# to the output boundary's monetary grammar (which recognises upper-case
# three-letter codes), so a reference can never smuggle a monetary figure
# past the grid.
_REFERENCE_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")

# Structural cap on the claim file.
_MAX_DOCUMENTS = 100

# Closed vocabulary for the claim type — anything unrecognised becomes
# "other" rather than echoing caller text into the summary header.
_CLAIM_TYPE_ALIASES: Dict[str, str] = {
    "auto": "auto",
    "motor": "auto",
    "vehicle": "auto",
    "car": "auto",
    "property": "property",
    "home": "property",
    "homeowner": "property",
    "fire": "property",
    "medical": "medical",
    "health": "medical",
    "injury": "medical",
    "bodily_injury": "medical",
    "liability": "liability",
    "gl": "liability",
    "casualty": "liability",
}

# Closed vocabulary for the case status.
_CASE_STATUSES = frozenset({"open", "closed", "pending", "reopened", "under_review"})

# Canonical document-type labels (index/group source documents by these).
_DOC_TYPE_ALIASES: Dict[str, str] = {
    "fnol": "FNOL",
    "first_notice_of_loss": "FNOL",
    "notice_of_loss": "FNOL",
    "medical": "medical_report",
    "medical_report": "medical_report",
    "med_report": "medical_report",
    "adjuster": "adjuster_note",
    "adjuster_note": "adjuster_note",
    "adjuster_notes": "adjuster_note",
    "photo": "photo",
    "photos": "photo",
    "image": "photo",
    "police_report": "police_report",
    "estimate": "estimate",
    "invoice": "estimate",
}

# An unrecognised document type is kept only when it is already an inert
# identifier; otherwise it is filed under "other".
_DOC_TYPE_RE = re.compile(r"^[a-z0-9_]{1,32}$")

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _normalise_doc_type(raw: Any) -> str:
    """Map a caller-supplied document type to a canonical, inert label."""
    key = str(raw or "").lower().strip().replace(" ", "_")
    if key in _DOC_TYPE_ALIASES:
        return _DOC_TYPE_ALIASES[key]
    return key if _DOC_TYPE_RE.match(key) else "other"


def _normalise_documents(raw_docs: Any) -> List[Dict[str, str]]:
    """Normalise the documents list to [{type, content}, ...].

    Photo entries fall back to their caption when no content is present.
    """
    if not isinstance(raw_docs, list):
        return []
    docs: List[Dict[str, str]] = []
    for entry in raw_docs:
        if not isinstance(entry, dict):
            continue
        dtype = _normalise_doc_type(entry.get("type"))
        content = entry.get("content")
        if content is None:
            content = entry.get("caption", "")
        docs.append({"type": dtype, "content": str(content)})
    return docs


def normalise_reference(raw: Any) -> Optional[str]:
    """Return the inert lowercase claim reference, or None if it is invalid."""
    if not isinstance(raw, str):
        return None
    candidate = raw.strip().lower()
    return candidate if _REFERENCE_RE.match(candidate) else None


class InputValidateNode(FunctionNode):
    """Domain validation of the claim-case payload for INS-C2-004.

    Applies business-rule checks beyond the structural JSON check in
    PreProcessNode: claim-reference normalisation, closed-vocabulary claim
    type / case status, document typing and capping, and a per-type document
    index used by the downstream BuildContextNode.

    Inner node — ANONYMOUS trust (see module docstring).

    Input state keys:
        validated_input: str  — normalised JSON string from PreProcessNode
                                Falls back to user_input for unit-test convenience.

    Output state keys (partial dict):
        claim_case:      str  — JSON-serialised normalised claim payload
        claim_reference: str  — inert lowercase reference
        status:          str
        error_log:       list[str]  (only on ERROR)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        raw = state.get("validated_input") or state.get("user_input", "")

        # ── Parse ─────────────────────────────────────────────────────────────
        try:
            payload: Dict[str, Any] = json.loads(raw) if isinstance(raw, str) else {}
        except (json.JSONDecodeError, ValueError):
            logger.error("InputValidateNode: JSON parse error")
            emit_trace_event("input_validate_failed", {"reason": "json_parse_error"}, state)
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": ["InputValidateNode: validated_input is not valid JSON"],
            }

        if not isinstance(payload, dict):
            emit_trace_event("input_validate_failed", {"reason": "payload_not_dict"}, state)
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": ["InputValidateNode: payload is not a JSON object"],
            }

        # ── Claim reference (inert identifier) ────────────────────────────────
        claim_reference = normalise_reference(payload.get("claim_id"))
        if claim_reference is None:
            emit_trace_event("input_validate_failed", {"reason": "invalid_claim_reference"}, state)
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": [
                    "InputValidateNode: claim_id must start with a letter and use only "
                    "letters, digits, hyphen or underscore (2-32 characters)"
                ],
            }

        # ── Normalise documents ───────────────────────────────────────────────
        raw_documents = payload.get("documents", [])
        if isinstance(raw_documents, list) and len(raw_documents) > _MAX_DOCUMENTS:
            emit_trace_event("input_validate_failed", {"reason": "too_many_documents"}, state)
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": [f"InputValidateNode: documents accepts at most {_MAX_DOCUMENTS} entries"],
            }

        documents = _normalise_documents(raw_documents)
        if not documents:
            emit_trace_event("input_validate_failed", {"reason": "no_documents"}, state)
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": ["InputValidateNode: documents list is empty"],
            }

        # ── Per-type document index ───────────────────────────────────────────
        doc_index: Dict[str, List[int]] = {}
        for idx, doc in enumerate(documents):
            doc_index.setdefault(doc["type"], []).append(idx)

        # ── Closed-vocabulary claim metadata ──────────────────────────────────
        raw_claim_type = str(payload.get("claim_type", "")).lower().strip()
        claim_type = _CLAIM_TYPE_ALIASES.get(raw_claim_type, "other" if raw_claim_type else "unknown")

        raw_status = str(payload.get("status", "")).lower().strip()
        case_status = raw_status if raw_status in _CASE_STATUSES else "open"

        raw_date = str(payload.get("date_of_loss", "")).strip()
        date_of_loss = raw_date if _ISO_DATE_RE.match(raw_date) else None

        claimant = payload.get("claimant")
        claimant_on_file = bool(isinstance(claimant, dict) and any(str(v).strip() for v in claimant.values()))

        # ── Build normalised claim_case ───────────────────────────────────────
        # The claimant's own details are deliberately NOT carried forward: the
        # summary records only whether claimant details are on file, so no
        # personal name or contact string can reach the rendered document.
        claim_case: Dict[str, Any] = {
            "claim_reference": claim_reference,
            "claim_type": claim_type,
            "date_of_loss": date_of_loss,
            "status": case_status,
            "claimant_on_file": claimant_on_file,
            "documents": documents,
            "doc_index": doc_index,
        }

        logger.info(
            "InputValidateNode: type=%s docs=%d doc_types=%d",
            claim_type,
            len(documents),
            len(doc_index),
        )
        emit_trace_event(
            "input_validate_complete",
            {
                "claim_reference": claim_reference,
                "claim_type": claim_type,
                "document_count": len(documents),
                "document_types": sorted(doc_index.keys()),
            },
            state,
        )

        return {
            "claim_case": to_json(claim_case),
            "claim_reference": claim_reference,
            "status": AgentStatus.SUCCESS.value,
        }
