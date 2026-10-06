"""AgentCore Platform v1.0"""

# INS-C2-004 — PostProcessNode
# Outer backbone post_process slot: the external-output boundary for the
# claim-case summary. Four layers, in a deliberate order:
#
#   (1) credential scan — API keys, JWTs, Bearer tokens or password
#       assignments anywhere in the assembled document withhold the summary
#       entirely (sanitised stub, status=ERROR);
#   (2) residual PII sweep — the same pattern set the domain redaction step
#       applies, re-run at the boundary so anything assembled after that step
#       is still masked;
#   (3) monetary precision grid — the documented external schema expresses
#       monetary figures in units of 1,000; every monetary-form token is
#       snapped onto that grid (an off-grid value is a full-precision figure
#       leaking to the external surface), with an audit event;
#   (4) re-scan — layers 1 and 2 run again over the snapped text, so the
#       numeric rewrite can never be the last word on what ships.
#
# LAYER ORDER IS LOAD-BEARING. Every PATTERN scan runs BEFORE the numeric
# snap. The snap reads a standalone three-letter upper-case word as a currency
# marker, so it would rewrite "SSN 123-45-6789" to "SSN 0-45-6789" — mangling
# the very shape the PII pattern recognises and shipping the rest of the
# number. Scanning first (and again afterwards) keeps the secret redacted
# rather than mangled.
#
# The output gate is the module-level function `_security_gate_output` called
# from inside execute() — NOT an instance method on the node class (the
# framework auto-wraps node instance methods on the real invoke path, which
# would raise AttributeError). The agent class exposes the same scanner as the
# canonical output-gate entry point and delegates to this module.
#
# Returns ONLY the state keys this node writes (partial-dict contract).

import logging
import re
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from framework.security.credential_detector import detect_credentials
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import EMPTY_INPUT, INPUT_REJECTED, INVALID_VALUE, TOO_LONG

from src.nodes.pii_redact_node import redact_pii

logger = logging.getLogger(__name__)

# Credential patterns that MUST NOT appear in the formatted claim summary.
_CREDENTIAL_PATTERNS: List[Tuple[str, str]] = [
    (r"(?:sk|pk|ak)-[A-Za-z0-9]{16,}", "api_key_pattern"),
    (r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "jwt_pattern"),
    (r"Bearer\s+[A-Za-z0-9_\-\.]{8,}", "bearer_token"),
    (
        r"(?:password|passwd|secret|api_key|token|access_key|private_key)" r"\s*[:=]\s*\S{8,}",
        "credential_assignment",
    ),
]

# Approved external precision: monetary figures are expressed in units of
# 1,000 (must match the schema note rendered by
# src/nodes/output_format_node.py — the document RENDERS on this grid, this
# gate ENFORCES it).
_EXTERNAL_ROUND_UNIT = 1000
# EXPLICIT output schema — monetary values are identified by FORM and by
# CURRENCY CONTEXT, never by magnitude:
#   form:    comma-grouped numbers (9,999 / 1,234,567) and unformatted runs
#            of 5+ digits (a rendering-regression leak);
#   context: any bare 1-4 digit number associated with a currency marker is
#            monetary even though short — SYMMETRICALLY: a currency code or a
#            currency symbol (incl. fullwidth ￥ and 円/₩), before or after the
#            value, attached or separated, signed or unsigned.
# Structural tokens stay untouched: page references ("p.21"), version tags
# ("v12"), bare counts, years without currency adjacency ("in 2026").
# ALL matched tokens, at ANY magnitude, must sit on the rounding grid;
# off-grid = a full-precision figure reaching the external surface,
# snapped + audited.
# Grammar (group-based; no lookbehinds inside the marker, so the delimiter can
# be arbitrary horizontal whitespace). Every value accepts an optional explicit
# +/- sign. Branch order matters: currency-context branches first, then
# form-based.
_SYMBOL = r"[¥￥$€£円₩]"
# A standalone three-letter upper-case word. `(?![A-Za-z])` — not `\b` — so a
# longer acronym cannot donate its first three letters ("STAR 2026" is
# structural, and "NAIC-12345" is not read as marker "AIC").
_ANY_CODE = r"[A-Z]{3}(?![A-Za-z])"

# Currency codes recognised when the marker is ATTACHED to the value with no
# whitespace between them. This is a currency list, not a list of identifiers
# to exclude: attached "<3 upper-case letters>-<digits>" is the shape this
# domain's record references take (POL-2026-0012345, ADJ-4821, NAIC-12345),
# and by form alone it is indistinguishable from an attached negative amount
# (JPY-9999). Naming the currencies resolves the ambiguity in the direction
# that keeps claim records intact, and the list is closed and stable, whereas
# an identifier list never could be. SEPARATED markers stay unrestricted
# ("ADJ 4821" still snaps), so the gate still fails safe on the ambiguous case.
_ISO_CODE = (
    r"(?:JPY|USD|EUR|GBP|CNY|CNH|KRW|AUD|CAD|CHF|HKD|SGD|TWD|THB|INR|IDR|MYR|PHP|VND"
    r"|BRL|MXN|SEK|NOK|DKK|PLN|CZK|TRY|NZD|RUB|ZAR|SAR|AED|ILS)(?![A-Za-z])"
)

# Delimiter between a currency marker and its value: horizontal whitespace and at
# most ONE newline — never a paragraph break. A plain `\s*` spans blank lines, so a
# 3-letter uppercase word ending a line would bind to the number that opens the next
# block and rewrite it ("Currency: JPY\n\n3. Cash Position" -> "0. Cash Position").
# Every enumerated leak form (spaces, tabs, single newline, signed, symmetric,
# comma-grouped) still matches.
_GATE_DELIM = r"[ \t]*(?:\n[ \t]*)?"
# The same delimiter with at least one whitespace character present.
_GATE_DELIM_WS = r"(?:[ \t]+|[ \t]*\n[ \t]*)"

# A currency CODE must start at an identifier boundary, or a longer acronym
# donates its tail: `STAR 2026` is structural, but an unguarded three-letter
# window reads "TAR" as a marker and rewrites the year. A SYMBOL needs no such
# guard - it is never part of an identifier - and must not have one, or the
# common "US$9999" / "HK$1,234" forms stop snapping.
#
# The class is `\w` (Unicode) PLUS the ASCII joiners, not an ASCII literal
# set. This agent renders Japanese claim documents, and 万 円 条 番 号 are all
# word characters: an ASCII-only guard happily enters the digit run in
# 第12345条 and reports an article number that does not exist — exactly the
# failure the guard was added to stop. `\w` already covers [A-Za-z0-9_], so
# this is a strict widening of the ASCII class; `/`, `.` and `-` are the
# joiners it does not cover.
_CODE_LEAD = r"(?<![\w/.\-])"

# Marker BEFORE the value: any 3-letter upper-case word when separated by
# whitespace; only a currency code when attached; a symbol either way.
_CURRENCY_MARKER = (
    rf"(?:{_CODE_LEAD}{_ANY_CODE}{_GATE_DELIM_WS}|{_CODE_LEAD}{_ISO_CODE}{_GATE_DELIM}|{_SYMBOL}{_GATE_DELIM})"
)
# Marker AFTER the value, same rule mirrored. No left guard is needed here:
# the matched value is itself the marker's left neighbour.
_CURRENCY_MARKER_POST = rf"(?:{_GATE_DELIM_WS}{_ANY_CODE}|{_GATE_DELIM}{_ISO_CODE}|{_GATE_DELIM}{_SYMBOL})"

# Every value alternative absorbs its decimal fraction into the SAME token.
# Without that, the fraction of "8.512345" is a standalone 5+-digit run in its
# own right and gets rewritten ("8.512,000"), a percentage becomes a number the
# document never contained ("9999.99999%" -> "9999.100,000%"), and a decimal
# amount snaps its integer part while the fraction dangles ("JPY 1234.56" ->
# "JPY 1,000.56") — neither the true figure nor a grid value.
#
# The `(?!\.\d)` arm is what makes the absorption stick. A plain `(?:\.\d+)?`
# lets the engine backtrack out of the fraction and re-match the integer part
# alone whenever the text right after the fraction fails the trailing guard, so
# "JPY 1234.56m" would go back to matching "JPY 1234" and the dangling-fraction
# bug returns. Either the fraction is taken whole, or there is none there.
_VAL_FRACTION = r"(?:\.\d+|(?!\.\d))"

# Identifier guards, widened to THIS template's render alphabet. A claim
# summary carries record references beside its figures, and every one of them
# is a digit run joined to something else:
#
#   clm-2026-004567          the claim reference (validated [a-z0-9_-])
#   POL-2026-0012345         policy number, hyphen-joined
#   POL/2026/0012345         the same reference in slash-joined house style
#   ADJ-4821 / NAIC-12345    adjuster and insurer codes
#   1HGCM82633A004352        VIN — digit runs bounded by letters
#   S72.001A                 ICD-10 diagnosis code
#   sku_48210                underscore-joined estimate-line label
#
# Without these characters in the class a policy number is read as a monetary
# figure and rewritten, and the summary reports a claim record that does not
# exist. `.` and `/` are in the LEADING guard only: in the trailing guard `.`
# would let an amount ending a sentence escape the grid ("the reserve totals
# JPY 9999.") and `/` would exempt a rate ("JPY 1,234/day").
#
# The guard sits before the UNMARKED value alternatives, not before the whole
# match. Identifier corruption is always a value entered part-way through a
# digit run, so that is where the guard belongs. Applying it to the whole match
# instead costs two live leak forms: a symbol that follows letters would be
# refused ("US$9999", "HK$1,234"), and after a consumed marker the guard would
# see the marker's own last letter and refuse the attached form the grid exists
# to catch ("JPY-9999"). The marker carries its own left boundary (_CODE_LEAD)
# and its delimiter bounds the value that follows it.
#
# The class is `\w` (Unicode) PLUS the ASCII joiners, not an ASCII literal
# set. This agent renders Japanese claim documents, and 万 円 条 番 号 are all
# word characters: an ASCII-only guard happily enters the digit run in
# 第12345条 and reports an article number that does not exist — exactly the
# failure the guard was added to stop. `\w` already covers [A-Za-z0-9_], so
# this is a strict widening of the ASCII class; `/`, `.` and `-` are the
# joiners it does not cover.
_LEAD_GUARD = r"(?<![\w/.\-])"
# ...but a currency SYMBOL is exempt from the trailing guard, because 円 and ₩
# are themselves word characters. Without the exemption "12345円" stops
# matching as a whole, and the engine then re-enters the run after the comma
# in "380,500円" and snaps the tail alone ("380,0円") - a worse corruption
# than the one the guard was closing. A symbol on the right is the very
# signal that the digits ARE an amount, so it may never block the match.
_TRAIL_GUARD = r"(?:(?=[¥￥$€£円₩])|(?![\w\-]))"

_NUM_TOKEN_RE = re.compile(
    # marker THEN value: "JPY 9999", "JPY  -9999", "JPY\t9999", "¥9999", "USD\n+9999".
    # The value alternatives accept a comma-grouped form FIRST: the regex is
    # leftmost-first, so without it "JPY 1,234" would match as marker + "1"
    # (mangling the number on the snap) instead of as the whole grouped value
    # — an on-grid "JPY 1,000" must stay byte-identical, and an off-grid
    # "JPY 1,234" must snap as 1234, not as 1.
    rf"(?:(?P<pre>{_CURRENCY_MARKER})"
    rf"(?P<val_after>[+-]?\d{{1,3}}(?:,\d{{3}})+{_VAL_FRACTION}|[+-]?\d{{1,4}}{_VAL_FRACTION})"
    # value THEN marker: "9999 JPY", "-9999\tJPY", "9999円", "+9999  $"
    rf"|{_LEAD_GUARD}(?P<val_before>[+-]?\d{{1,4}}{_VAL_FRACTION})(?P<post>{_CURRENCY_MARKER_POST})"
    # form-based, standalone at any magnitude: comma-grouped or 5+-digit runs
    rf"|{_LEAD_GUARD}(?P<val_form>[+-]?\d{{1,3}}(?:,\d{{3}})+{_VAL_FRACTION}|[+-]?\d{{5,}}{_VAL_FRACTION}))"
    + _TRAIL_GUARD
)

# Placeholder used while the claim reference is held out of the numeric snap.
# It carries no digits and no currency marker, so it cannot itself match the
# monetary grammar; control characters are stripped from every caller string
# at ingest, so no document body can contain it.
_REFERENCE_PLACEHOLDER = "\x00CLAIM-REFERENCE\x00"


def _enforce_precision(result: str) -> tuple[str, int]:
    """Snap every monetary-form token onto the approved external grid.

    Returns (sanitised_result, redaction_count). A redaction means a
    full-precision monetary figure reached the external surface — the gate
    rounds it onto the approved grid. The currency marker, the original
    delimiter whitespace, and the explicit sign of the original token are all
    preserved on the snapped replacement.
    """
    redactions = 0

    def _snap(match: re.Match[str]) -> str:
        nonlocal redactions
        pre = match.group("pre") or ""
        post = match.group("post") or ""
        token = match.group("val_after") or match.group("val_before") or match.group("val_form")
        # float(), not int(): the token absorbs its decimal fraction, and the
        # whole amount — not just its integer part — is what sits on the grid.
        value = float(token.replace(",", ""))  # float() understands leading +/-
        if value % _EXTERNAL_ROUND_UNIT == 0:
            return match.group(0)
        redactions += 1
        snapped = round(value / _EXTERNAL_ROUND_UNIT) * _EXTERNAL_ROUND_UNIT
        plus = "+" if token.startswith("+") and snapped >= 0 else ""
        return f"{pre}{plus}{snapped:,d}{post}"

    return _NUM_TOKEN_RE.sub(_snap, result), redactions


def _enforce_precision_outside_reference(result: str, claim_reference: Optional[str]) -> tuple[str, int]:
    """Apply the monetary grid to everything except the claim reference itself.

    The claim reference is a STRUCTURAL identifier, not a monetary figure, and
    a long digit run inside it ("clm-2026-004567") would otherwise be snapped
    and the case reference corrupted. It is held out by exact string — the
    value validated at ingest, never a pattern — and only when the monetary
    grammar would actually touch it, so nothing else can hide behind the
    hold-out. The reference cannot smuggle a figure past the grid either: it
    is validated to a lowercase identifier, so it carries no comma grouping,
    no currency symbol and no upper-case currency code, and therefore has no
    approved monetary representation to express.
    """
    if not claim_reference or _REFERENCE_PLACEHOLDER in result or not _NUM_TOKEN_RE.search(claim_reference):
        return _enforce_precision(result)
    held = result.replace(claim_reference, _REFERENCE_PLACEHOLDER)
    snapped, redactions = _enforce_precision(held)
    return snapped.replace(_REFERENCE_PLACEHOLDER, claim_reference), redactions


def _security_gate_output(content: str) -> Optional[str]:
    """Scan output for disallowed credential/secret patterns.

    Returns the first violation name, or None if the output is clean.
    Module-level function (not a node instance method) — the framework
    auto-wraps node instance methods on the real invoke path, so the gate
    must live at module level.

    Two pattern sets, and the order is deliberate. The DOMAIN set runs first:
    it is broader than the framework's in places this domain needs (a
    `password:`/`token=` assignment in a claim note is not a shape the
    framework recognises at all), and its names are the ones the audit trail
    and the withholding notice carry. The FRAMEWORK's own recogniser then runs
    over whatever the domain set let through.

    The framework arm is not defence in depth — it closes a BYPASS. The
    framework scans every node result with that same detector and RAISES on a
    hit, and a raise discards the node's whole return, taking the containment
    in execute() with it: the state would keep the pre-gate summary and the
    envelope would ship it. A shape the framework refuses and this gate missed
    is therefore worse than a narrower gate. Measured against the shipped
    domain set, four shapes fell in that gap — `AKIA…` (no domain pattern at
    all), `sk_live_…` (the domain key pattern requires a hyphen), a
    single-segment `eyJ…` (the domain JWT pattern requires three dot-separated
    parts) and `postgresql://…`. The two sets can no longer drift apart in the
    dangerous direction, by construction.

    Only the violation CLASS is returned — never the matched value. Echoing it
    would put the refused string back into this node's own result, where the
    framework's output-side scan raises and discards the clearing.
    """
    for pattern, name in _CREDENTIAL_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            return name
    findings = detect_credentials(content)
    if findings:
        return str(findings[0]["type"])
    return None


def apply_output_gate(content: str, claim_reference: Optional[str] = None) -> Tuple[str, Optional[str], Dict[str, int]]:
    """Run the full output boundary over an assembled summary.

    Returns (safe_text, violation, gate_counts). ``violation`` is non-None
    when a credential pattern was found — the caller must then withhold the
    summary entirely. ``gate_counts`` reports the work each layer did:
    ``pii`` (masked values) and ``precision`` (off-grid figures snapped).

    Order: credential scan → PII sweep → precision snap → both scans again.
    Pattern scans always precede the numeric rewrite; see the module header.
    """
    violation = _security_gate_output(content)
    if violation:
        return content, violation, {"pii": 0, "precision": 0}

    swept, pii_counts = redact_pii(content)
    pii_total = sum(pii_counts.values())

    snapped, precision_redactions = _enforce_precision_outside_reference(swept, claim_reference)

    # Re-scan: the snap rewrites digits, so both pattern layers get the last
    # word on the text that actually ships.
    violation = _security_gate_output(snapped)
    if violation:
        return snapped, violation, {"pii": pii_total, "precision": precision_redactions}

    snapped, residual_counts = redact_pii(snapped)
    pii_total += sum(residual_counts.values())

    return snapped, None, {"pii": pii_total, "precision": precision_redactions}


# ── Output-gate containment ───────────────────────────────────────────────────
# Blocking is not a status flip. The outer envelope resolves the caller-facing
# value as `formatted_output or result` with NO regard for status, and `result`
# carries the PRE-GATE summary that merge_output copied out of the inner graph
# — so returning ERROR while leaving these fields populated ships the very
# document the gate refused, inside the error envelope. Every field below
# carries assembled answer text or a structured payload and is therefore
# overwritten on a block.
#
# `claim_reference` is deliberately NOT here: it is an inert, validated
# identifier that carries no claim content, and the operator needs it to
# retrieve the withheld summary. The outer get_output() withholds it on any
# non-success outcome regardless.
_OUTPUT_BEARING_FIELDS = ("claim_summary", "redacted_sections", "pii_findings", "damages_assessment")


def _withheld_state(notice: str) -> Dict[str, Any]:
    """The full content-free replacement written over state when the gate blocks.

    `formatted_output` is deliberately NON-EMPTY. A falsy replacement ("" or
    {}) is exactly what re-opens the hole: the envelope's `formatted_output or
    result` fallback would step straight past it to the pre-gate value.
    """
    cleared: Dict[str, Any] = {field: None for field in _OUTPUT_BEARING_FIELDS}
    cleared["formatted_output"] = notice
    cleared["result"] = notice
    return cleared


# Reason code -> the sentence the caller reads. A code with no entry falls
# back to the generic one rather than leaking the code itself.
_DEGRADED_MESSAGES = {
    "EMPTY_INPUT": EMPTY_INPUT,
    "QUESTION_TOO_LONG": TOO_LONG,
    "INVALID_REQUEST": INVALID_VALUE,
}


class PostProcessNode(FunctionNode):
    """Apply the output boundary and expose the final claim-case summary.

    Outer backbone post_process slot.  Declared ANONYMOUS — trust was
    already enforced at PreProcessNode (VERIFIED_EXTERNAL).

    Input state keys:
        claim_summary:   str  — formatted summary from inner OutputFormatNode
        claim_reference: str  — inert reference held out of the numeric snap

    Output state keys (partial dict):
        formatted_output: str
        result:           str
        status:           str
        error_log:        list[str]  (only on ERROR)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        # A run declined upstream has nothing to format. Render the reason as
        # the caller-facing body and carry the marker onward.
        marker = state.get("error_code")
        if marker:
            message = _DEGRADED_MESSAGES.get(marker, INPUT_REJECTED)
            emit_trace_event("post_process_degraded", {"reason": marker}, state)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": marker,
                "result": message,
                "formatted_output": message,
            }
        claim_summary: str = state.get("claim_summary") or state.get("result") or ""

        # ── Fallback for empty summary ────────────────────────────────────────
        if not claim_summary.strip():
            logger.warning("PostProcessNode: claim_summary is empty — using fallback message")
            claim_summary = (
                "[Insurance Claim Case Summary] No summary content generated. " "Check error_log for upstream failures."
            )

        claim_reference = state.get("claim_reference")
        gated, violation, counts = apply_output_gate(claim_summary, claim_reference)

        if violation:
            logger.error("PostProcessNode: credential pattern detected in output — %s", violation)
            emit_trace_event("post_process_credential_violation", {"violation": violation}, state)
            sanitised = (
                f"[CLAIM SUMMARY REDACTED: output contained a disallowed pattern "
                f"({violation}). Contact the claims security team for the original summary.]"
            )
            # CONTAINMENT: overwrite every output-bearing field, not just the
            # status. See _OUTPUT_BEARING_FIELDS — leaving claim_summary,
            # redacted_sections, pii_findings or damages_assessment in state
            # hands the refused content to the envelope, which reads state
            # without consulting status.
            return {
                **_withheld_state(sanitised),
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PostProcessNode: credential pattern detected — {violation}"],
            }

        if counts["pii"]:
            logger.warning("PostProcessNode: %d residual personal value(s) masked at the boundary", counts["pii"])
            emit_trace_event("post_process_pii_redaction", {"redaction_count": counts["pii"]}, state)

        if counts["precision"]:
            logger.warning(
                "PostProcessNode: %d off-grid monetary token(s) snapped to the external grid",
                counts["precision"],
            )
            emit_trace_event("post_process_precision_redaction", {"redaction_count": counts["precision"]}, state)

        logger.info("PostProcessNode: output gate passed — length=%d", len(gated))
        emit_trace_event("post_process_complete", {"output_length": len(gated)}, state)

        return {
            "formatted_output": gated,
            "result": gated,
            "status": AgentStatus.SUCCESS.value,
        }
