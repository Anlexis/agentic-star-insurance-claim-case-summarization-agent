# INS-C2-004 — Integration: the compiled OUTER graph's success path must return
# the domain claim-case summary result to the caller.
#
# AgentBaseGraph.get_output() surfaces only the {output, status, ...} envelope,
# so without the override in src/graph/graph.py a successful agent.invoke()
# would drop every structured domain field (formatted_output / result /
# claim_summary / redacted_sections / pii_findings / damages_assessment) even
# though PostProcessNode and the inner DomainWorkflowGraph populated them on
# the internal state — and an ["output"]-only test would never notice.
#
# These tests invoke the COMPILED OUTER graph (Graph().compile().invoke(...))
# with a valid VERIFIED_EXTERNAL claim-case payload and assert the domain
# result is surfaced. A second case proves the output boundary is not weakened:
# a credential inside a source document blocks the invoke and the structured
# domain fields are withheld (fail-closed).

import json

from framework.schemas.agent_status import AgentStatus

# A valid VERIFIED_EXTERNAL claim-case payload that runs the full inner
# pipeline to a clean pass.
_VALID_PAYLOAD = json.dumps(
    {
        "claim_id": "CLM-2026-004567",
        "claim_type": "auto",
        "date_of_loss": "2026-06-15",
        "status": "open",
        "claimant": {"name": "Test Claimant", "contact": "claims-desk@example-insurer.co.jp"},
        "documents": [
            {"type": "fnol", "content": "First Notice of Loss: rear-end collision; both vehicles drivable."},
            {"type": "adjuster_note", "content": "Estimated repair cost 380,000 JPY. No total-loss indicators."},
            {"type": "medical_report", "content": "Mild whiplash (WAD grade 1); conservative treatment."},
            {"type": "estimate", "content": "Parts 210,000 JPY, labour 150,000 JPY."},
            {"type": "photo", "caption": "Rear bumper impact damage"},
        ],
    }
)

_REFERENCE = "clm-2026-004567"

_VALID_CONTEXT = {
    "channel": "claims_portal",
    "deductible": 50000,
    "estimate_lines": [{"label": "parts", "amount": 210000}, {"label": "labour", "amount": 150000}],
}

# A claim whose FNOL document embeds a credential secret. It propagates into the
# generated Incident Facts section and thus into the assembled claim summary;
# the output boundary MUST withhold it: status -> ERROR, sanitised stub, raw
# secret never surfaced. The token is letters-only so no personal-data pattern
# touches it upstream.
_CREDENTIAL_PAYLOAD = json.dumps(
    {
        "claim_id": "CLM-2026-004568",
        "claim_type": "auto",
        "documents": [
            {
                "type": "fnol",
                "content": (
                    "First Notice of Loss: minor collision. "
                    "Internal portal token sk-ABCDEFGHIJKLMNOPQRSTUVWX recorded in the file."
                ),
            },
        ],
    }
)


def _patch_domain_emit(monkeypatch):
    """Patch emit_trace_event in every node module (avoids audit-backend calls)."""
    for mod_suffix in (
        "pre_process_node",
        "input_validate_node",
        "build_context_node",
        "generate_response_node",
        "pii_redact_node",
        "output_format_node",
        "post_process_node",
    ):
        try:
            monkeypatch.setattr(
                f"src.nodes.{mod_suffix}.emit_trace_event",
                lambda *a, **k: None,
            )
        except AttributeError:
            pass  # module not imported / no emit symbol; fine


def _invoke(monkeypatch, payload, input_context=None):
    """Compile and invoke the OUTER graph as a real VERIFIED_EXTERNAL caller."""
    _patch_domain_emit(monkeypatch)
    from framework.schemas.invocation_context import InvocationContext, TrustLevel
    from src.graph.graph import Graph

    agent = Graph()
    agent.compile()
    ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
    return agent.invoke(payload, ctx=ctx, input_context=input_context or {})


class TestOuterInvokeReturnsDomainResult:
    def test_success_invoke_surfaces_domain_result(self, monkeypatch):
        result = _invoke(monkeypatch, _VALID_PAYLOAD, _VALID_CONTEXT)

        assert (
            result.get("status") == AgentStatus.SUCCESS.value
        ), f"expected SUCCESS, got {result.get('status')!r}; error_log={result.get('error_log')}"

        formatted_output = result.get("formatted_output")
        claim_summary = result.get("claim_summary")
        assert formatted_output is not None, "formatted_output must be surfaced on a successful invoke"
        assert claim_summary is not None, "claim_summary must be surfaced on a successful invoke"
        assert result.get("result") is not None, "result must be surfaced on a successful invoke"

        # The surfaced summary must actually be the assembled claim-case document.
        for needle in ("INSURANCE CLAIM CASE SUMMARY", _REFERENCE):
            assert needle in formatted_output, f"{needle!r} missing from formatted_output"
            assert needle in claim_summary, f"{needle!r} missing from claim_summary"

        # The structured domain result is surfaced and carries the 6 sections.
        rendered = result.get("redacted_sections")
        assert rendered is not None, "redacted_sections must be surfaced"
        assert set(json.loads(rendered).keys()) == {
            "case_overview",
            "incident_facts",
            "documentation_summary",
            "injury_medical",
            "damages_assessment",
            "recommended_action",
        }
        assert result.get("pii_findings") is not None, "pii_findings must be surfaced"
        assert result.get("claim_reference") == _REFERENCE

        # The caller's damages data produced real aggregates.
        damages = json.loads(result["damages_assessment"])
        assert damages["estimated_total"] == 360_000
        assert damages["net_exposure"] == 310_000
        assert damages["severity"] == "moderate"

        # The framework envelope is preserved (backward compatible).
        assert result.get("output") is not None

    def test_every_surfaced_representation_is_on_the_documented_grid(self, monkeypatch):
        """The document and the structured sections must express the same,
        on-grid figures — a second representation is not a second schema."""
        import re

        result = _invoke(monkeypatch, _VALID_PAYLOAD, _VALID_CONTEXT)
        surfaces = [result["formatted_output"], result["redacted_sections"], result["damages_assessment"]]
        for surface in surfaces:
            probe = surface.replace(_REFERENCE, "")
            for token in re.findall(r"-?\d{1,3}(?:,\d{3})+|-?\d{5,}", probe):
                assert int(token.replace(",", "")) % 1_000 == 0, f"off-grid value leaked: {token}"

    def test_credential_block_withholds_structured_fields(self, monkeypatch):
        """The output boundary is not weakened: a credential in a source document
        blocks the invoke and the structured domain fields are withheld."""
        result = _invoke(monkeypatch, _CREDENTIAL_PAYLOAD)

        assert (
            result.get("status") == AgentStatus.ERROR.value
        ), f"expected the output boundary to block, got status={result.get('status')!r}"
        # The credential-bearing structured result is NOT surfaced.
        assert result.get("claim_summary") is None
        assert result.get("redacted_sections") is None
        assert result.get("pii_findings") is None
        assert result.get("damages_assessment") is None
        # The caller-facing output carries only the sanitised stub — never the raw secret.
        assert "sk-ABCDEFGHIJKLMNOPQRSTUVWX" not in (result.get("formatted_output") or "")
        assert "sk-ABCDEFGHIJKLMNOPQRSTUVWX" not in (result.get("result") or "")
        assert "sk-ABCDEFGHIJKLMNOPQRSTUVWX" not in (result.get("output") or "")
