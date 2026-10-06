# PB-6: Invoke Execution Order Verification
# Verifies BaseNode.__call__() enforces: trust gate -> node_start -> input gate
# -> execute() -> output gate -> node_complete, for every concrete node under
# src/nodes/.
#
# Also verifies the full backbone invoke order for the outer
# InsuranceClaimCaseSummarizationAgent (Cat 2 two-layer nested graph):
#   InitializeNode -> PreProcessNode (pre_process) -> ClaimSummaryGraphNode (main)
#   -> PostProcessNode (post_process) -> FinalizeNode
#
# PB-6 invoke uses VERIFIED_EXTERNAL caller trust (the real external path) —
# NEVER for_internal(). A VERIFIED_EXTERNAL invocation context exercises the
# same code path a real deployed caller uses: it clears the outer PreProcessNode
# trust gate (required_trust_level = VERIFIED_EXTERNAL) AND passes through the
# inner ANONYMOUS domain nodes. for_internal() (INTERNAL) would not represent a
# real external caller, so it is deliberately not used.

import importlib
import inspect
import json
import pkgutil
from pathlib import Path

import pytest

# ── Template-specific constants ───────────────────────────────────────────────

# Class name of the node in the `main` backbone slot.
_MAIN_SLOT_NODE = "ClaimSummaryGraphNode"

# A SUCCESS-yielding insurance claim-case payload for the backbone invoke test.
# All PreProcessNode + InputValidateNode required fields present (claim_id plus
# a non-empty documents list). The FNOL document deliberately carries a phone
# number and an email so the personal-data redaction is exercised end to end.
#
# CONTRACT: deploy/invoke_payload.json MUST carry this exact input string and
# this exact input_context — the deployed first-invoke evidence and the PB-6
# test must exercise the identical request. test_invoke_payload_matches_pb6
# below asserts that equality so the two can never drift.
_VALID_PAYLOAD = json.dumps(
    {
        "claim_id": "CLM-2026-004567",
        "claim_type": "auto",
        "date_of_loss": "2026-06-15",
        "status": "open",
        "claimant": {
            "name": "Test Claimant",
            "contact": "claims-desk@example-insurer.co.jp",
        },
        "documents": [
            {
                "type": "fnol",
                "content": (
                    "First Notice of Loss: rear-end collision at the Shinjuku 3-chome "
                    "intersection on 2026-06-15. Insured vehicle was stationary at a red "
                    "light when struck from behind. No airbag deployment; both vehicles "
                    "drivable. Reporting party contact on file: 090-1234-5678 and "
                    "witness@example.com."
                ),
            },
            {
                "type": "adjuster_note",
                "content": (
                    "Adjuster assessment: rear bumper, trunk lid and left tail-lamp "
                    "assembly damaged. Estimated repair cost 380,000 JPY. No total-loss "
                    "indicators; salvage not applicable."
                ),
            },
            {
                "type": "medical_report",
                "content": (
                    "Claimant reported neck stiffness consistent with mild whiplash "
                    "(WAD grade 1). Conservative treatment prescribed; two-week "
                    "follow-up advised."
                ),
            },
            {
                "type": "estimate",
                "content": ("Body-shop repair estimate: parts 210,000 JPY, labour 150,000 JPY, paint 20,000 JPY."),
            },
            {"type": "photo", "caption": "Photograph of rear bumper impact damage"},
        ],
    }
)

# The caller's damages data for the same request: the aggregates the summary
# reports are computed from these validated figures.
_VALID_CONTEXT = {
    "channel": "claims_portal",
    "deductible": 50000,
    "policy_limit": 5000000,
    "reserve_amount": 300000,
    "estimate_lines": [
        {"label": "parts", "amount": 210000},
        {"label": "labour", "amount": 150000},
        {"label": "paint", "amount": 20000},
    ],
}

# The inert, normalised form of the payload's claim_id.
_REFERENCE = "clm-2026-004567"

# ─────────────────────────────────────────────────────────────────────────────


def _discover_node_classes() -> list[type]:
    """Import every module under src/nodes/ and collect concrete BaseNode subclasses."""
    from framework.nodes.base_node import BaseNode

    try:
        pkg = importlib.import_module("src.nodes")
    except ImportError:
        return []

    discovered = []
    for _, modname, _ in pkgutil.walk_packages(pkg.__path__, prefix="src.nodes."):
        module = importlib.import_module(modname)
        for attr in vars(module).values():
            if (
                isinstance(attr, type)
                and issubclass(attr, BaseNode)
                and attr is not BaseNode
                and attr.__module__ == modname
                and not inspect.isabstract(attr)
            ):
                discovered.append(attr)
    return discovered


def _patch_domain_emit(monkeypatch):
    """Patch emit_trace_event in every domain node module (avoids audit-backend calls)."""
    for mod_suffix in (
        "pre_process_node",
        "input_validate_node",
        "build_context_node",
        "generate_response_node",
        "pii_redact_node",
        "output_format_node",
        "post_process_node",
        "main_node",
    ):
        try:
            monkeypatch.setattr(
                f"src.nodes.{mod_suffix}.emit_trace_event",
                lambda *a, **k: None,
            )
        except AttributeError:
            pass  # module not yet imported / no emit symbol; fine


class TestInvokeOrder:
    """PB-6: __call__ must run trust gate -> node_start -> input gate -> execute()
    -> output gate -> node_complete."""

    def test_call_order_for_every_node(self, monkeypatch):
        node_classes = _discover_node_classes()
        if not node_classes:
            pytest.skip("no concrete BaseNode subclasses found under src/nodes/")

        import framework.nodes.base_node as base_node_module

        failures: list[str] = []
        for node_cls in node_classes:
            order: list[str] = []
            monkeypatch.setattr(
                base_node_module,
                "emit_trace_event",
                lambda event_type, _payload, _state, _o=order: _o.append(f"event:{event_type}"),
            )

            for method_name, label in (
                ("_security_gate_input", "security_gate_input"),
                ("execute", "execute"),
                ("_security_gate_output", "security_gate_output"),
            ):
                original = getattr(node_cls, method_name)

                def spy(self, arg, _o=order, _label=label, _orig=original):
                    _o.append(_label)
                    return _orig(self, arg)

                monkeypatch.setattr(node_cls, method_name, spy)

            instance = node_cls()
            # caller trust == the node's required level so the trust gate always
            # passes here; the denial branch is asserted separately below.
            state = {
                "caller_trust_level": node_cls.required_trust_level.value,
                "correlation_id": "pb6-invoke-order-test",
            }
            instance(state)

            expected = [
                "event:node_start",
                "security_gate_input",
                "execute",
                "security_gate_output",
                "event:node_complete",
            ]
            if order != expected:
                failures.append(
                    f"{node_cls.__name__}: invoke order violation.\nexpected: {expected}\nactual:   {order}"
                )

        assert not failures, "\n\n".join(failures)


class TestTrustGate:
    """PB-6: the trust gate in BaseNode.__call__ runs BEFORE execute() and denies
    a caller whose trust is below the node's required_trust_level."""

    def test_pre_process_denies_anonymous_caller(self, monkeypatch):
        """PreProcessNode (required VERIFIED_EXTERNAL) must refuse an ANONYMOUS caller."""
        _patch_domain_emit(monkeypatch)
        from framework.schemas.agent_status import AgentStatus
        from framework.schemas.trust_level import TrustLevel
        from src.nodes.pre_process_node import PreProcessNode

        node = PreProcessNode()
        result = node(
            {
                "caller_trust_level": TrustLevel.ANONYMOUS.value,
                "user_input": _VALID_PAYLOAD,
                "correlation_id": "pb6-trust-denial",
            }
        )
        assert result["status"] == AgentStatus.ERROR.value
        assert any(
            "trust gate" in e.lower() for e in result.get("error_log", [])
        ), f"expected a trust-gate denial, got error_log={result.get('error_log')}"

    def test_pre_process_admits_verified_external_caller(self, monkeypatch):
        """The same node admits a VERIFIED_EXTERNAL caller and runs execute() to SUCCESS."""
        _patch_domain_emit(monkeypatch)
        from framework.schemas.agent_status import AgentStatus
        from framework.schemas.trust_level import TrustLevel
        from src.nodes.pre_process_node import PreProcessNode

        node = PreProcessNode()
        result = node(
            {
                "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
                "user_input": _VALID_PAYLOAD,
                "input_context": {},
                "correlation_id": "pb6-trust-admit",
            }
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["validated_input"] is not None


class TestBackboneInvokeOrder:
    """PB-6 backbone: a full Graph().invoke() runs the 5-node backbone in order.

    Backbone order: InitializeNode -> PreProcessNode (pre_process) ->
                    ClaimSummaryGraphNode (main) ->
                    PostProcessNode (post_process) -> FinalizeNode

    Uses VERIFIED_EXTERNAL caller trust — the real external path.
    InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL) is mandatory;
    NEVER use for_internal(), which would not represent a real external caller.
    """

    def _invoke(self, monkeypatch):
        _patch_domain_emit(monkeypatch)
        from framework.schemas.invocation_context import InvocationContext, TrustLevel
        from src.graph.graph import Graph

        agent = Graph()
        agent.compile()
        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        return agent.invoke(_VALID_PAYLOAD, ctx=ctx, input_context=_VALID_CONTEXT)

    def test_backbone_invoke_succeeds_and_returns_output(self, monkeypatch):
        from framework.schemas.agent_status import AgentStatus

        result = self._invoke(monkeypatch)
        assert result.get("status") == AgentStatus.SUCCESS.value, (
            f"Expected status={AgentStatus.SUCCESS.value!r}, got: {result.get('status')!r}\n"
            f"error_log: {result.get('error_log')}"
        )
        assert result.get("output") is not None, "output must be set after a successful invoke"
        # The assembled claim-case summary must be present in the surfaced output.
        assert "INSURANCE CLAIM CASE SUMMARY" in result["output"]
        assert _REFERENCE in result["output"]
        # Personal data must never leak end to end. The framework's input gate
        # masks the FNOL phone/email to [MASKED] BEFORE the domain nodes run, so
        # the raw values never reach the output (the domain redaction path with
        # its [REDACTED:<kind>] markers is exercised directly at the unit level
        # in test_nodes.py, where execute() runs without the input gate).
        assert "090-1234-5678" not in result["output"]
        assert "witness@example.com" not in result["output"]
        assert "[MASKED]" in result["output"]

    def test_backbone_node_history_matches_expected_order(self, monkeypatch):
        result = self._invoke(monkeypatch)
        history = result.get("node_history", [])
        assert history == [
            "InitializeNode",
            "PreProcessNode",
            "ClaimSummaryGraphNode",
            "PostProcessNode",
            "FinalizeNode",
        ], f"unexpected backbone node_history: {history}"

    def test_main_slot_is_claim_summary_graph_node(self):
        """The `main` backbone slot must be ClaimSummaryGraphNode (a GraphNode — Cat 2)."""
        from framework.nodes.graph_node import GraphNode
        from src.graph.graph import ClaimSummaryGraphNode, InsuranceClaimCaseSummarizationAgent

        agent = InsuranceClaimCaseSummarizationAgent()
        agent.compile()
        main_node = agent._nodes.get("main")
        assert main_node is not None, "main slot must be registered"
        assert isinstance(
            main_node, ClaimSummaryGraphNode
        ), f"main slot must be ClaimSummaryGraphNode, got {type(main_node).__name__}"
        assert isinstance(main_node, GraphNode), "main slot node must subclass GraphNode (Cat 2 contract)"
        assert main_node.__class__.__name__ == _MAIN_SLOT_NODE

    def test_invoke_payload_matches_pb6(self):
        """deploy/invoke_payload.json must carry the PB-6 request verbatim.

        The deployed first-invoke evidence POSTs invoke_payload.json as the
        request body, so it must exercise the same request PB-6 asserts yields
        SUCCESS — both the claim payload and the caller's damages data.
        """
        repo_root = Path(__file__).resolve().parents[2]
        payload_file = repo_root / "deploy" / "invoke_payload.json"
        assert payload_file.exists(), "deploy/invoke_payload.json is required for the deployed first invoke"
        body = json.loads(payload_file.read_text())
        assert body.get("input") == _VALID_PAYLOAD, "deploy/invoke_payload.json['input'] must equal the PB-6 payload"
        assert (
            body.get("input_context") == _VALID_CONTEXT
        ), "deploy/invoke_payload.json['input_context'] must equal the PB-6 caller context"
        # And the payload the deployed server forwards to agent.invoke() must
        # itself be a valid, PreProcessNode-parseable claim JSON object.
        claim = json.loads(body["input"])
        for required in ("claim_id", "documents"):
            assert required in claim, f"invoke_payload input missing required field: {required}"
