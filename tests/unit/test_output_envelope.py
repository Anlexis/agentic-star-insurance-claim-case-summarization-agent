# INS-C2-004 - Unit tests: the outer invoke() envelope
# (InsuranceClaimCaseSummarizationAgent.get_output)
#
# The envelope is the last thing between the graph state and the caller, and it
# is the second half of the output-gate contract. The framework base resolves
# its `output` key as `formatted_output or result` WITHOUT consulting status,
# and on this agent `result` is the PRE-GATE claim summary that
# ClaimSummaryGraphNode.merge_output() copied out of the inner graph before the
# output boundary ever ran. An envelope that forwards state verbatim therefore
# hands back the un-gated document inside a non-success envelope.
#
# Measured on the shipped code, on the real /invoke surface, with a non-success
# inner result forwarded by merge_output: post_process was skipped, yet both
# `output` and `result` carried the full claim summary - FNOL narrative,
# medical narrative, claim reference, monetary figures and exposure band.
#
# These call get_output() directly on a state dict so the resolution rule
# itself is pinned, independent of which node happened to produce that state.

import json

import pytest
from framework.schemas.agent_status import AgentStatus

from src.graph.graph import InsuranceClaimCaseSummarizationAgent

# What the inner workflow produces before the output gate has run.
_PRE_GATE_SUMMARY = (
    "INSURANCE CLAIM CASE SUMMARY\n"
    "Claim Reference: clm-2026-004567\n"
    "Derived from First Notice of Loss (FNOL): rear-end collision at Shinjuku.\n"
    "Net exposure: 310,000 - exposure band severe."
)
_GATE_OUTPUT = "INSURANCE CLAIM CASE SUMMARY\nClaim Reference: clm-2026-004567\nAll clear."

_NON_SUCCESS = [
    AgentStatus.ERROR.value,
    AgentStatus.TIMEOUT.value,
    AgentStatus.CANCELLED.value,
    AgentStatus.RETRY.value,
]


def _state(**overrides) -> dict:
    state = {
        "status": AgentStatus.SUCCESS.value,
        "result": _PRE_GATE_SUMMARY,
        "formatted_output": _GATE_OUTPUT,
        "claim_summary": _PRE_GATE_SUMMARY,
        "claim_reference": "clm-2026-004567",
        "redacted_sections": json.dumps({"case_overview": _PRE_GATE_SUMMARY}),
        "pii_findings": json.dumps([{"section": "case_overview", "kind": "email", "count": 1}]),
        "damages_assessment": json.dumps({"net_exposure": 310000, "severity": "severe"}),
        "trace_id": "envelope-test",
        "correlation_id": "envelope-test",
        "node_history": [],
    }
    state.update(overrides)
    return state


def _envelope(**overrides) -> dict:
    return InsuranceClaimCaseSummarizationAgent().get_output(_state(**overrides))


class TestSuccessEnvelope:
    """The control. Without it every containment assertion below would also pass
    on an envelope that returns nothing at all."""

    def test_success_surfaces_the_gated_summary_and_the_domain_result(self):
        envelope = _envelope()
        assert envelope["status"] == AgentStatus.SUCCESS.value
        assert envelope["output"] == _GATE_OUTPUT
        assert envelope["formatted_output"] == _GATE_OUTPUT
        assert envelope["claim_summary"] == _GATE_OUTPUT
        assert envelope["result"] == _PRE_GATE_SUMMARY
        assert envelope["claim_reference"] == "clm-2026-004567"
        assert json.loads(envelope["damages_assessment"])["severity"] == "severe"
        assert json.loads(envelope["redacted_sections"])["case_overview"]
        for key in ("trace_id", "correlation_id", "node_history"):
            assert key in envelope


class TestNonSuccessContainment:
    @pytest.mark.parametrize("status", _NON_SUCCESS)
    def test_the_pre_gate_summary_is_never_surfaced(self, status):
        envelope = _envelope(status=status)
        assert envelope["result"] is None, status
        assert _PRE_GATE_SUMMARY not in json.dumps(envelope), status

    @pytest.mark.parametrize("status", _NON_SUCCESS)
    def test_the_or_result_fallback_is_dead(self, status):
        """The hole in the base envelope: with no gate output, `output` falls
        through to the pre-gate `result`. On a non-success outcome an absent
        gate output must stay absent, never become the un-gated document."""
        envelope = _envelope(status=status, formatted_output=None)
        assert not envelope["output"], status
        blob = json.dumps(envelope)
        assert "Shinjuku" not in blob, status
        assert "310,000" not in blob, status
        assert "severe" not in blob, status

    @pytest.mark.parametrize("status", _NON_SUCCESS)
    def test_the_structured_domain_result_is_withheld(self, status):
        envelope = _envelope(status=status)
        for key in ("claim_summary", "claim_reference", "redacted_sections", "pii_findings", "damages_assessment"):
            assert envelope[key] is None, f"{key} released on {status}"

    def test_the_gate_notice_is_the_whole_error_surface(self):
        """What the gate itself produced is all the caller may see."""
        notice = "[CLAIM SUMMARY REDACTED: output contained a disallowed pattern (bearer_token).]"
        envelope = _envelope(status=AgentStatus.ERROR.value, formatted_output=notice)
        assert envelope["output"] == notice
        assert envelope["formatted_output"] == notice
        assert envelope["result"] is None
