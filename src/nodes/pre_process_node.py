"""AgentCore Platform v1.0"""

# INS-C2-004 — PreProcessNode
# Outer backbone pre_process slot (trust gate + caller-data validation).
#
# Responsibilities:
#   - Enforce VERIFIED_EXTERNAL trust (required_trust_level — the trust gate)
#   - Reject empty / over-long / non-JSON claim payloads early (fail-fast)
#   - Strip control characters before parsing
#   - Confirm the required claim-case fields are present
#   - Refuse instruction-override (prompt-injection) content anywhere in the
#     decoded claim payload (template-owned; NOT delegated to the framework
#     input gate — see the screen's comment for why)
#   - Validate every declared input_context field against explicit bounds
#     (fail CLOSED on an invalid field; never echo the rejected value)
#   - Write validated_input (normalised JSON string) + enriched_context
#   - Emit an audit event for every validation decision
#
# Returns ONLY the state keys this node writes (partial-dict contract).

import json
import logging
import re
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.progress import emit_progress
from src.services.failure_message import EMPTY_INPUT, INPUT_REJECTED, TOO_LONG

from src.schemas.state import finite_in_range, to_json

logger = logging.getLogger(__name__)

# Required top-level keys for a valid claim-case payload.
# claim_id is the mandatory identifier; documents is the minimum needed to
# generate a claim-case summary.
_REQUIRED_CLAIM_KEYS = frozenset({"claim_id", "documents"})

# Maximum accepted payload length. A complete claim file with several long
# documents fits comfortably; anything larger is refused at the boundary.
_MAX_PAYLOAD_CHARS = 200_000

# Control characters (except tab/newline) are stripped before parsing so no
# caller can smuggle them into the rendered summary.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# ── Template-owned instruction-override screen ────────────────────────────────
# Claim documents are caller-supplied prose, and prose addressed to the MODEL
# ("Ignore all previous instructions...") must never produce a summary. The
# framework's input gate is NOT a substitute for this screen: it exists only on
# 1.0.1+ hosts, covers only user_input/validated_input, and rejects only
# HIGH-confidence findings — on a 1.0.0 host an injected document reaches the
# summary path and returns success (fails OPEN). This screen makes the refusal
# the template's own guarantee.
#
# Deliberately narrow, anchored to whole words and full phrase shapes — never
# bare substrings. Insurance claim prose legitimately says things like
# "override the initial reserve", "act as an agent of record", "ignore the
# previous estimate", and "impact assessment" (which CONTAINS the letters
# "act as" across its word boundary): every alternative therefore requires
# both a directive verb in the imperative/infinitive AND an instruction-noun
# object ("...instructions", "...system prompt", a model role), so those
# sentences pass while "Ignore all previous instructions" does not. Past-tense
# reported speech ("the claimant ignored previous instructions") never matches:
# the verbs are anchored as whole words, so "ignored" is not "ignore".
_INJECTION_RE = re.compile(
    # ignore/disregard/forget + [determiners] + temporal/system qualifier
    # + an instruction-noun (never "estimate", "reserve", "figure", ...)
    r"\b(?:ignore|disregard|forget)\s+(?:all\s+|any\s+|the\s+|your\s+|my\s+)*"
    r"(?:previous|prior|above|earlier|preceding|original|system)\s+"
    r"(?:instruction|instructions|prompt|prompts|rule|rules|direction|directions)\b"
    # exfiltration of the system/initial prompt ("the instructions" alone is
    # legitimate claim-form prose; "the" therefore requires the system/initial/
    # hidden qualifier, while second-person "your instructions" is directive)
    r"|\b(?:reveal|show|print|repeat|output|disclose|display)\s+(?:me\s+)?"
    r"(?:your\s+(?:system\s+|initial\s+|hidden\s+)?(?:prompt|prompts|instructions)"
    r"|the\s+(?:system|initial|hidden)\s+(?:prompt|prompts|instructions|message))\b"
    # role-reassignment: requires a MODEL role ("you are now a member of our
    # preferred network" / "you are now able to track your claim" pass)
    r"|\byou\s+are\s+now\s+(?:a\s+|an\s+)?(?:different\s+|unrestricted\s+|new\s+)?"
    r"(?:assistant|ai|chatbot|language\s+model|llm|dan)\b"
    # privileged-mode role-play: requires developer/admin/root + "mode"
    # ("act as an agent of record" passes)
    r"|\bact\s+as\s+(?:if\s+you\s+are\s+)?(?:a\s+|an\s+)?(?:developer|admin|root|jailbroken)\s+mode\b"
    # rule-override: requires an instruction-noun ("override the initial
    # reserve" / "override the deductible" pass)
    r"|\boverride\s+(?:your|the)\s+(?:instruction|instructions|rule|rules|safety|guardrail|guardrails)\b",
    re.IGNORECASE,
)


def contains_instruction_override(payload: Any) -> bool:
    """True when any decoded string in the payload carries override phrasing.

    Walks the PARSED payload (keys and values, at any depth), not the raw JSON
    text, so JSON backslash-u escaping cannot smuggle a phrase past the screen. The
    walk is iterative — payload depth is bounded only by what json.loads
    accepted, and this must not be the piece that falls over first.
    """
    stack: List[Any] = [payload]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if _INJECTION_RE.search(item):
                return True
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return False


# ── Caller-data (input_context) validation bounds ─────────────────────────────
# input_context is caller-supplied and untrusted: every declared field is
# bounds-checked before it can influence the run. Violations return
# status=ERROR naming the offending FIELD only — never the offending VALUE
# (caller data must not round-trip into error logs). Undeclared keys are
# ignored; the /invoke adapter separately caps the serialized size.
#
# The contract carries NO free text: channel and estimate-line labels are
# locked to an inert identifier alphabet and every other field is a number,
# so there is no context-channel prose for the summary's redaction step to
# miss.
_IDENTIFIER_RE = re.compile(r"^[a-z0-9_]{1,32}$")

# Monetary bounds. The upper bound is deliberately far above any realistic
# single-claim figure while still refusing absurd magnitudes outright.
_AMOUNT_MIN = 0.0
_AMOUNT_MAX = 1_000_000_000_000.0

# Structural cap on the caller-supplied estimate table.
_MAX_ESTIMATE_LINES = 20

# Numeric input_context fields: name -> (lo, hi).
_NUMERIC_FIELDS: Dict[str, Tuple[float, float]] = {
    "deductible": (_AMOUNT_MIN, _AMOUNT_MAX),
    "policy_limit": (_AMOUNT_MIN, _AMOUNT_MAX),
    "reserve_amount": (_AMOUNT_MIN, _AMOUNT_MAX),
}


def _validate_estimate_lines(raw: Any) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """Validate the caller's estimate table: labels inert, amounts finite.

    Returns (lines, error_message). Error messages name the field only.
    """
    if not isinstance(raw, list):
        return None, "input_context.estimate_lines must be an array"
    if len(raw) > _MAX_ESTIMATE_LINES:
        return None, f"input_context.estimate_lines accepts at most {_MAX_ESTIMATE_LINES} entries"

    lines: List[Dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            return None, "input_context.estimate_lines entries must be objects"
        label = entry.get("label")
        if not isinstance(label, str) or not _IDENTIFIER_RE.match(label):
            return None, (
                "input_context.estimate_lines[].label must be a lowercase identifier (a-z, 0-9, _; 1-32 chars)"
            )
        amount = finite_in_range(entry.get("amount"), _AMOUNT_MIN, _AMOUNT_MAX)
        if amount is None:
            return None, (
                f"input_context.estimate_lines[].amount must be a finite number "
                f"between {int(_AMOUNT_MIN)} and {int(_AMOUNT_MAX)}"
            )
        lines.append({"label": label, "amount": amount})
    return lines, None


def validate_input_context(input_context: Any) -> Tuple[Dict[str, Any], Optional[str]]:
    """Validate the caller's input_context against the declared contract.

    Returns (normalised_context, error_message). Error messages name the
    field only — never the rejected value. Accepted fields:

        channel:        str matching ^[a-z0-9_]{1,32}$   (absent → "unknown")
        deductible:     finite number 0..1e12            (absent → not set)
        policy_limit:   finite number 0..1e12            (absent → not set)
        reserve_amount: finite number 0..1e12            (absent → not set)
        estimate_lines: array (max 20) of
                        {label: identifier, amount: finite 0..1e12}

    Any other key is ignored. A non-mapping input_context is rejected.
    This is the single definition of the contract: the inner
    BuildContextNode re-applies it so a direct inner-graph invocation gets
    exactly the same fail-closed rule.
    """
    if input_context is None:
        return {"channel": "unknown"}, None
    if not isinstance(input_context, dict):
        return {}, "input_context must be an object"

    normalised: Dict[str, Any] = {}

    channel = input_context.get("channel")
    if channel is None:
        normalised["channel"] = "unknown"
    elif isinstance(channel, str) and _IDENTIFIER_RE.match(channel):
        normalised["channel"] = channel
    else:
        return {}, "input_context.channel must be a lowercase identifier (a-z, 0-9, _; 1-32 chars)"

    for field, (lo, hi) in _NUMERIC_FIELDS.items():
        raw = input_context.get(field)
        if raw is None:
            continue
        parsed = finite_in_range(raw, lo, hi)
        if parsed is None:
            return {}, f"input_context.{field} must be a finite number between {int(lo)} and {int(hi)}"
        normalised[field] = parsed

    raw_lines = input_context.get("estimate_lines")
    if raw_lines is not None:
        lines, error = _validate_estimate_lines(raw_lines)
        if error is not None:
            return {}, error
        normalised["estimate_lines"] = lines

    return normalised, None


class PreProcessNode(FunctionNode):
    """Input validation for INS-C2-004.

    Validates the caller-supplied claim-case payload and the caller-supplied
    damages context before the domain workflow runs. Free-text payload content
    is screened for instruction-override (prompt-injection) phrasing here, in
    the template's own code — the framework input gate (1.0.1+, HIGH-confidence
    findings only) is defence-in-depth, never the guarantee. This is the outer
    backbone's pre_process slot — the only node with VERIFIED_EXTERNAL trust,
    so unauthenticated or anonymous callers are rejected here (fail-fast;
    inner domain nodes carry ANONYMOUS trust and never see untrusted input
    directly).

    Input state keys:
        user_input:    str   — caller-supplied JSON claim-case payload
        input_context: dict  — optional caller damages data (channel,
                               deductible, policy_limit, reserve_amount,
                               estimate_lines). Invalid values fail CLOSED.

    Output state keys (partial dict):
        validated_input:  str        — normalised JSON string (re-serialised)
        enriched_context: str        — JSON-serialised channel metadata
        status:           str        — AgentStatus.SUCCESS or ERROR
        error_log:        list[str]  — set only on ERROR
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        user_input = state.get("user_input", "")
        input_context = state.get("input_context")

        # ── Emptiness check ───────────────────────────────────────────────────
        if not user_input or not isinstance(user_input, str) or not user_input.strip():
            logger.warning("PreProcessNode: user_input is empty or missing")
            emit_trace_event("pre_process_validation_failed", {"reason": "empty_input"}, state)
            emit_progress(EMPTY_INPUT)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "EMPTY_INPUT",
                "error_log": ["PreProcessNode: user_input is empty or missing"],
            }

        if len(user_input) > _MAX_PAYLOAD_CHARS:
            logger.warning("PreProcessNode: payload exceeds the accepted size")
            emit_trace_event("pre_process_validation_failed", {"reason": "payload_too_large"}, state)
            emit_progress(TOO_LONG)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "QUESTION_TOO_LONG",
                "error_log": [f"PreProcessNode: user_input exceeds {_MAX_PAYLOAD_CHARS} characters"],
            }

        # ── JSON parse ────────────────────────────────────────────────────────
        cleaned = _CONTROL_CHARS_RE.sub("", user_input.strip())
        try:
            payload: Dict[str, Any] = json.loads(cleaned)
        except (json.JSONDecodeError, ValueError):
            logger.warning("PreProcessNode: JSON parse failed")
            emit_trace_event("pre_process_validation_failed", {"reason": "json_parse_error"}, state)
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": ["PreProcessNode: user_input is not valid JSON"],
            }

        if not isinstance(payload, dict):
            emit_trace_event("pre_process_validation_failed", {"reason": "payload_not_object"}, state)
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": ["PreProcessNode: JSON root must be an object"],
            }

        # ── Required field check ──────────────────────────────────────────────
        missing = _REQUIRED_CLAIM_KEYS - payload.keys()
        if missing:
            emit_trace_event(
                "pre_process_validation_failed",
                {"reason": "missing_required_fields", "missing": sorted(missing)},
                state,
            )
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": [f"PreProcessNode: missing required fields: {sorted(missing)}"],
            }

        # ── Instruction-override screen (template-owned, fail CLOSED) ─────────
        # Runs on the DECODED payload before anything is carried forward, so a
        # refusal leaves no validated_input and no enriched_context for any
        # downstream node. input_context needs no screen: its contract carries
        # no free text (identifiers and numbers only, validated below).
        if contains_instruction_override(payload):
            logger.warning("PreProcessNode: instruction-override content refused")
            emit_trace_event(
                "pre_process_validation_failed",
                {"reason": "instruction_override"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: claim payload refused - instruction-override content"],
            }

        # ── Caller-data contract (input_context) ──────────────────────────────
        context, context_error = validate_input_context(input_context)
        if context_error is not None:
            logger.warning("PreProcessNode: input_context validation failed")
            emit_trace_event(
                "pre_process_validation_failed",
                {"reason": "invalid_input_context"},
                state,
            )
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": [f"PreProcessNode: {context_error}"],
            }

        # ── Success ───────────────────────────────────────────────────────────
        normalised_json = json.dumps(payload, ensure_ascii=False)

        logger.info("PreProcessNode: validated claim payload with %d keys", len(payload))
        emit_trace_event(
            "pre_process_validated",
            {
                "payload_keys": sorted(payload.keys()),
                "context_keys": sorted(context.keys()),
            },
            state,
        )

        return {
            "validated_input": normalised_json,
            "enriched_context": to_json(
                {
                    "source": "InsuranceClaimCaseSummarizationAgent",
                    "channel": context.get("channel", "unknown"),
                }
            ),
            "status": AgentStatus.SUCCESS.value,
        }
