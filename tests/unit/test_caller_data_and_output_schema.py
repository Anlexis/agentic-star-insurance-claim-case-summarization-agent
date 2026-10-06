# INS-C2-004 — Unit tests: caller-data input contract + external output schema.
#
# Covers the behaviours the two boundaries are responsible for:
#   1) PreProcessNode validates every declared input_context field against
#      explicit bounds (identifier alphabet, finite numeric ranges, entry
#      caps), fails CLOSED on an invalid field, and never echoes the value.
#   2) BuildContextNode computes the published damages aggregates from the
#      validated data, classifies exposure, and degrades to the document
#      baseline when no damages data is supplied.
#   3) A malformed operator configuration can never weaken the classification.
#   4) The declared runtime configuration (config/config.yaml) reaches the
#      inner graph's node constructors, and the caller's input_context reaches
#      inner nodes through the full nested graph (the context bridge).
#   5) The output boundary enforces the documented schema on every monetary
#      representation (both ways: every leak form snaps; structural tokens
#      stay byte-identical), keeps its pattern scans ahead of the numeric
#      rewrite, and holds the structural claim reference out of the grid.

import json

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from src.nodes.build_context_node import (
    _resolved_thresholds,
    classify_severity,
    compute_damages,
    snap_to_grid,
)
from src.nodes.post_process_node import (
    _NUM_TOKEN_RE,
    _enforce_precision,
    _enforce_precision_outside_reference,
    apply_output_gate,
)
from src.nodes.pre_process_node import validate_input_context
from src.schemas.state import finite_in_range, from_json

_DEFAULT_BANDS = {"moderate": 100_000.0, "high": 1_000_000.0, "severe": 10_000_000.0}

# The full non-finite / out-of-contract matrix every caller number must refuse.
# An explicit null is NOT in it: for an optional field, null means "not
# supplied" (asserted separately below).
_NON_FINITE = ["NaN", "Infinity", "-Infinity", float("nan"), float("inf"), float("-inf"), True, "abc", -1, [1]]
_NON_FINITE_IDS = [
    "str-nan",
    "str-inf",
    "str-neginf",
    "raw-nan",
    "raw-inf",
    "raw-neginf",
    "bool",
    "text",
    "negative",
    "array",
]


# ── input_context contract ────────────────────────────────────────────────────


class TestInputContextValidation:
    def test_absent_context_defaults_channel_unknown(self):
        context, error = validate_input_context(None)
        assert error is None
        assert context == {"channel": "unknown"}

    def test_valid_full_context(self):
        context, error = validate_input_context(
            {
                "channel": "portal",
                "deductible": 50_000,
                "policy_limit": 5_000_000,
                "reserve_amount": 300_000,
                "estimate_lines": [{"label": "parts", "amount": 210_000}],
            }
        )
        assert error is None
        assert context["channel"] == "portal"
        assert context["deductible"] == 50_000
        assert context["estimate_lines"] == [{"label": "parts", "amount": 210_000.0}]

    def test_unknown_keys_are_ignored(self):
        context, error = validate_input_context({"channel": "portal", "not_a_field": "whatever"})
        assert error is None
        assert set(context) == {"channel"}

    def test_non_mapping_context_is_rejected(self):
        _, error = validate_input_context(["portal"])
        assert error is not None and "input_context" in error

    @pytest.mark.parametrize(
        "bad_channel",
        ["Portal", "claims-desk!", "a" * 33, "", 7, {"a": 1}],
        ids=["uppercase", "symbol", "too-long", "empty", "int", "mapping"],
    )
    def test_invalid_channel_fails_closed(self, bad_channel):
        context, error = validate_input_context({"channel": bad_channel})
        assert error is not None and "channel" in error
        assert context == {}

    def test_rejected_channel_value_is_never_echoed(self):
        _, error = validate_input_context({"channel": "SECRET-INTERNAL-DESK"})
        assert "SECRET-INTERNAL-DESK" not in (error or "")

    @pytest.mark.parametrize("field", ["deductible", "policy_limit", "reserve_amount"])
    @pytest.mark.parametrize("bad_value", _NON_FINITE, ids=_NON_FINITE_IDS)
    def test_every_numeric_field_refuses_the_non_finite_matrix(self, field, bad_value):
        context, error = validate_input_context({field: bad_value})
        assert error is not None and field in error, f"{field}={bad_value!r} was accepted"
        assert context == {}

    @pytest.mark.parametrize("field", ["deductible", "policy_limit", "reserve_amount"])
    def test_explicit_null_means_not_supplied(self, field):
        context, error = validate_input_context({field: None})
        assert error is None
        assert field not in context

    def test_rejected_numeric_value_is_never_echoed(self):
        _, error = validate_input_context({"deductible": "1234567890123456"})
        assert "1234567890123456" not in (error or "")

    def test_over_magnitude_amount_is_refused(self):
        _, error = validate_input_context({"policy_limit": 1e13})
        assert error is not None and "policy_limit" in error

    def test_estimate_lines_entry_cap(self):
        lines = [{"label": "parts", "amount": 1000} for _ in range(21)]
        _, error = validate_input_context({"estimate_lines": lines})
        assert error is not None and "at most 20" in error

    def test_estimate_lines_must_be_an_array(self):
        _, error = validate_input_context({"estimate_lines": {"label": "parts", "amount": 1}})
        assert error is not None and "array" in error

    @pytest.mark.parametrize(
        "bad_label",
        ["Parts", "parts!", "", "a" * 33, 5, None],
        ids=["uppercase", "symbol", "empty", "too-long", "int", "null"],
    )
    def test_estimate_line_label_must_be_inert(self, bad_label):
        _, error = validate_input_context({"estimate_lines": [{"label": bad_label, "amount": 1000}]})
        assert error is not None and "label" in error

    @pytest.mark.parametrize("bad_value", _NON_FINITE + [None], ids=_NON_FINITE_IDS + ["missing"])
    def test_estimate_line_amount_refuses_the_non_finite_matrix(self, bad_value):
        """Every line needs a real amount — a missing one is a rejection, not a default."""
        _, error = validate_input_context({"estimate_lines": [{"label": "parts", "amount": bad_value}]})
        assert error is not None and "amount" in error


class TestPreProcessNodeContract:
    """The ingest boundary refuses invalid caller data before any domain work."""

    @pytest.fixture(autouse=True)
    def patch_emit(self, monkeypatch):
        monkeypatch.setattr("src.nodes.pre_process_node.emit_trace_event", lambda *a, **k: None)

    def _call(self, input_context):
        from src.nodes.pre_process_node import PreProcessNode

        payload = json.dumps({"claim_id": "CLM-1", "documents": [{"type": "fnol", "content": "x"}]})
        return PreProcessNode()(
            {
                "user_input": payload,
                "input_context": input_context,
                "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
            }
        )

    def test_invalid_channel_errors_naming_field_only(self):
        result = self._call({"channel": "Claims-Desk!"})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert any("input_context.channel" in e for e in result["error_log"])
        assert "Claims-Desk!" not in " ".join(result["error_log"])
        assert "validated_input" not in result

    def test_non_finite_deductible_errors_before_domain_runs(self):
        result = self._call({"deductible": "NaN"})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert any("deductible" in e for e in result["error_log"])
        assert "validated_input" not in result


# ── finite_in_range (the caller-numeric parser) ───────────────────────────────


class TestFiniteInRange:
    @pytest.mark.parametrize(
        "value",
        ["NaN", "Infinity", "-Infinity", float("nan"), float("inf"), True, False, None, [], {}, "x", 1e13],
    )
    def test_rejects_non_finite_and_out_of_range(self, value):
        assert finite_in_range(value, 0.0, 1e12) is None

    @pytest.mark.parametrize("value,expected", [(0, 0.0), ("42", 42.0), (1.5, 1.5), (1e12, 1e12)])
    def test_accepts_finite_in_range(self, value, expected):
        assert finite_in_range(value, 0.0, 1e12) == expected


# ── Damages arithmetic + severity classification ──────────────────────────────


class TestDamagesArithmetic:
    def _context(self, **overrides):
        context = {
            "estimate_lines": [
                {"label": "parts", "amount": 210_000},
                {"label": "labour", "amount": 150_500},
                {"label": "paint", "amount": 20_000},
            ],
            "deductible": 50_000,
        }
        context.update(overrides)
        return context

    def test_no_estimate_lines_degrades_to_none(self):
        assert compute_damages({"channel": "portal"}, _DEFAULT_BANDS) is None

    def test_aggregates_are_computed_and_published_on_the_grid(self):
        damages = compute_damages(self._context(), _DEFAULT_BANDS)
        # exact total 380,500 -> published on the 1,000 grid
        assert damages["estimated_total"] == 380_000
        assert damages["net_exposure"] == 330_000  # exact 330,500 snapped
        assert damages["line_count"] == 3
        assert damages["line_labels"] == ["labour", "paint", "parts"]

    def test_line_amounts_are_never_published(self):
        published = json.dumps(compute_damages(self._context(), _DEFAULT_BANDS))
        assert "210000" not in published and "150500" not in published

    def test_policy_limit_caps_the_net_exposure(self):
        damages = compute_damages(self._context(policy_limit=100_000), _DEFAULT_BANDS)
        assert damages["net_exposure"] == 100_000
        assert damages["limit_applied"] is True

    def test_deductible_never_drives_exposure_negative(self):
        damages = compute_damages(self._context(deductible=1_000_000), _DEFAULT_BANDS)
        assert damages["net_exposure"] == 0

    def test_reserve_variance_is_reported_when_a_reserve_is_supplied(self):
        damages = compute_damages(self._context(reserve_amount=300_000), _DEFAULT_BANDS)
        assert damages["reserve_variance"] == 80_000  # exact 80,500 snapped
        assert compute_damages(self._context(), _DEFAULT_BANDS)["reserve_variance"] is None

    @pytest.mark.parametrize(
        "exposure,expected",
        [
            (0, "low"),
            (99_999, "low"),
            (100_000, "moderate"),
            (999_999, "moderate"),
            (1_000_000, "high"),
            (9_999_999, "high"),
            (10_000_000, "severe"),
        ],
    )
    def test_severity_bands(self, exposure, expected):
        assert classify_severity(exposure, _DEFAULT_BANDS) == expected

    def test_snap_to_grid_rounds_to_the_nearest_unit(self):
        assert snap_to_grid(380_500) == 380_000
        assert snap_to_grid(380_501) == 381_000
        assert snap_to_grid(0) == 0


class TestSeverityBandFailSafe:
    """A malformed operator configuration must never reclassify a claim."""

    def test_absent_config_keeps_defaults(self):
        assert _resolved_thresholds(None) == _DEFAULT_BANDS
        assert _resolved_thresholds({}) == _DEFAULT_BANDS

    @pytest.mark.parametrize("bad", ["NaN", float("inf"), float("nan"), True, "big", -1, 1e13])
    def test_malformed_band_keeps_defaults(self, bad):
        config = {"moderate_threshold": bad, "high_threshold": 1_000_000, "severe_threshold": 10_000_000}
        assert _resolved_thresholds(config) == _DEFAULT_BANDS

    def test_non_ascending_bands_are_rejected_wholesale(self):
        config = {"moderate_threshold": 5_000_000, "high_threshold": 1_000_000, "severe_threshold": 10_000_000}
        assert _resolved_thresholds(config) == _DEFAULT_BANDS

    def test_valid_bands_are_adopted(self):
        config = {"moderate_threshold": 1, "high_threshold": 2, "severe_threshold": 3}
        assert _resolved_thresholds(config) == {"moderate": 1.0, "high": 2.0, "severe": 3.0}

    def test_a_non_finite_band_cannot_downgrade_a_severe_claim(self):
        from src.nodes.build_context_node import BuildContextNode

        node = BuildContextNode(config={"moderate_threshold": float("nan")})
        assert classify_severity(50_000_000, node._thresholds) == "severe"


# ── Runtime configuration reaches the graph ───────────────────────────────────


class TestRuntimeConfigPlumbing:
    def test_runtime_config_reads_repo_config(self):
        from src.graph.graph import _runtime_config

        cfg = _runtime_config()
        assert cfg["max_retry"] == 3
        assert cfg["timeout_s"] == 30
        assert cfg["damages"]["high_threshold"] == 1_000_000

    def test_missing_config_file_degrades_to_empty(self, monkeypatch, tmp_path):
        import src.graph.graph as graph_module

        monkeypatch.setattr(graph_module, "_RUNTIME_CONFIG_PATH", tmp_path / "absent.yaml")
        assert graph_module._runtime_config() == {}

    def test_declared_config_reaches_inner_node_constructors(self):
        from src.graph.domain_workflow_graph import DomainWorkflowGraph
        from src.graph.graph import ClaimSummaryGraphNode

        declared = ClaimSummaryGraphNode()._parent_config()
        assert declared["high_threshold"] == 1_000_000
        assert declared["system_prompt_template"] == "prompts/claim_summary.j2"

        graph = DomainWorkflowGraph(config=declared)
        graph.register_nodes()
        assert graph._nodes["build_context"]._thresholds["high"] == 1_000_000
        assert graph._nodes["generate_response"]._settings["max_tokens"] == 4000

    def test_malformed_config_values_are_not_forwarded(self, monkeypatch, tmp_path):
        import src.graph.graph as graph_module

        bad = tmp_path / "config.yaml"
        bad.write_text(
            "damages:\n  moderate_threshold: .nan\n  high_threshold: not-a-number\n"
            "llm:\n  temperature: 99\n  max_tokens: 0\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(graph_module, "_RUNTIME_CONFIG_PATH", bad)
        declared = graph_module.ClaimSummaryGraphNode()._parent_config()
        assert "moderate_threshold" not in declared
        assert "high_threshold" not in declared
        assert "temperature" not in declared
        assert "max_tokens" not in declared

    def test_caller_damages_reach_the_inner_graph_end_to_end(self, monkeypatch):
        """The context bridge: input_context set on the OUTER invoke must drive
        the inner Damages Assessment (GraphNode does not forward it itself)."""
        for mod in (
            "pre_process_node",
            "input_validate_node",
            "build_context_node",
            "generate_response_node",
            "pii_redact_node",
            "output_format_node",
            "post_process_node",
        ):
            monkeypatch.setattr(f"src.nodes.{mod}.emit_trace_event", lambda *a, **k: None)

        from framework.schemas.invocation_context import InvocationContext, TrustLevel as CtxTrustLevel
        from src.graph.graph import Graph

        payload = json.dumps(
            {
                "claim_id": "CLM-2026-004567",
                "documents": [{"type": "fnol", "content": "Rear-end collision."}],
            }
        )
        agent = Graph()
        agent.compile()
        result = agent.invoke(
            payload,
            ctx=InvocationContext(caller_trust_level=CtxTrustLevel.VERIFIED_EXTERNAL),
            input_context={"estimate_lines": [{"label": "parts", "amount": 12_000_000}]},
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        damages = from_json(result["damages_assessment"])
        assert damages["estimated_total"] == 12_000_000
        assert damages["severity"] == "severe"
        assert "major-loss unit" in result["formatted_output"]


# ── External output schema enforcement ────────────────────────────────────────


class TestPrecisionGate:
    @pytest.mark.parametrize(
        "leak,expected",
        [
            ("JPY 9999", "JPY 10,000"),
            ("9999 JPY", "10,000 JPY"),
            ("JPY 1,234", "JPY 1,000"),
            ("1,234 JPY", "1,000 JPY"),
            ("JPY 1,234,567", "JPY 1,235,000"),
            ("¥9999", "¥10,000"),
            ("￥9999", "￥10,000"),
            ("9999円", "10,000円"),
            ("JPY -9999", "JPY -10,000"),
            ("JPY +9999", "JPY +10,000"),
            ("+9999 JPY", "+10,000 JPY"),
            ("JPY\t9999", "JPY\t10,000"),
            ("JPY  9999", "JPY  10,000"),
            ("JPY\n9999", "JPY\n10,000"),
            ("9,999", "10,000"),
            ("123456", "123,000"),
            ("-12345", "-12,000"),
            ("1,234,567", "1,235,000"),
        ],
        ids=[
            "marker-value",
            "value-marker",
            "marker-grouped",
            "grouped-marker",
            "marker-grouped-millions",
            "symbol",
            "fullwidth-symbol",
            "yen-suffix",
            "signed-neg",
            "signed-pos",
            "signed-pos-before",
            "tab",
            "double-space",
            "newline",
            "grouped-bare",
            "long-run",
            "signed-run",
            "grouped-millions",
        ],
    )
    def test_every_leak_form_snaps(self, leak, expected):
        sanitised, redactions = _enforce_precision(leak)
        assert sanitised == expected
        assert redactions == 1

    @pytest.mark.parametrize(
        "structural",
        [
            "p.21",
            "guidelines v12",
            "in 2026",
            "STAR 2026",
            "WAD grade 1",
            "90d",
            "10,000",
            "JPY 1,000",
            "3 documents",
            "2026-06-15",
            "Documents on File: 5",
        ],
    )
    def test_structural_and_on_grid_tokens_byte_identical(self, structural):
        sanitised, redactions = _enforce_precision(structural)
        assert sanitised == structural
        assert redactions == 0


class TestDecimalsAndClaimIdentifiers:
    """The grid applies to AMOUNTS — not to fractions, and not to record references.

    A claim summary carries policy numbers, adjuster codes, diagnosis codes and
    VINs beside its figures, and it quotes ratios and percentages that were
    never monetary. The grammar used to read the fraction of a decimal as a
    standalone 5+-digit run and any 3-letter upper-case word as a currency
    marker, so it rewrote all of them: "8.512345" -> "8.512,000",
    "POL-2026-0012345" -> "POL-2,000-12,000", "ICD-10" -> "ICD0". A summary
    that reports a policy number the insurer has never issued is worse than one
    that reports an off-grid figure.

    Both directions are pinned: every leak form still snaps (above), every
    identifier and decimal comes back byte-identical (here).
    """

    @pytest.mark.parametrize(
        "text",
        [
            "8.512345",
            "9999.99999%",
            "ratio 0.123456",
            "liability share 92.34567%",
            "reserve factor 1.0825",
            "deductible 12.5%",
        ],
        ids=["fraction-6-digits", "percentage-long-fraction", "bare-decimal", "apportionment", "ldf", "deductible"],
    )
    def test_a_decimal_that_is_not_an_amount_is_byte_identical(self, text):
        sanitised, redactions = _enforce_precision(text)
        assert sanitised == text
        assert redactions == 0

    @pytest.mark.parametrize(
        "text",
        [
            "POL-2026-0012345",
            "POL/2026/0012345",
            "ADJ-4821",
            "NAIC-12345",
            "CPT-99213",
            "ICD-10 S72.001A",
            "VIN 1HGCM82633A004352",
            "sku_48210",
            "clm-2026-004567",
            "INS-C2-004",
            "date of loss 2026-07-01",
        ],
        ids=[
            "policy-hyphen",
            "policy-slash",
            "adjuster",
            "naic",
            "cpt-attached",
            "icd10",
            "vin",
            "estimate-line-label",
            "claim-reference",
            "template-id",
            "iso-date",
        ],
    )
    def test_a_claim_identifier_is_byte_identical(self, text):
        sanitised, redactions = _enforce_precision(text)
        assert sanitised == text
        assert redactions == 0

    @pytest.mark.parametrize(
        "text,expected",
        [
            # An off-grid amount in currency context snaps as ONE number — the
            # fraction is absorbed into the token, never left dangling.
            ("JPY 1234.56", "JPY 1,000"),
            ("USD 12500.75", "USD 13,000"),
            # ...and the trailing-suffix form cannot backtrack out of the
            # fraction and re-snap the integer part ("JPY 1,000.56m").
            ("JPY 1234.56m", "JPY 1234.56m"),
            # Attached ISO code: still an amount, still snapped.
            ("JPY-9999", "JPY-10,000"),
            # A symbol preceded by letters is still a currency marker.
            ("US$9999", "US$10,000"),
            # An amount that ends a sentence must not escape the grid.
            ("the reserve totals JPY 9999.", "the reserve totals JPY 10,000."),
            ("JPY 1,234/day", "JPY 1,000/day"),
            # Structural: a 3-letter code ending a line must not bind to the
            # number that opens the next block.
            ("Currency: JPY\n\n3. Documentation Summary", "Currency: JPY\n\n3. Documentation Summary"),
            # A longer acronym does not donate its first three letters.
            ("STAR 2026", "STAR 2026"),
        ],
        ids=[
            "decimal-amount",
            "decimal-amount-usd",
            "decimal-suffix",
            "attached-iso",
            "symbol-after-letters",
            "sentence-end",
            "rate",
            "paragraph-break",
            "acronym",
        ],
    )
    def test_amount_forms_resolve_as_documented(self, text, expected):
        sanitised, _ = _enforce_precision(text)
        assert sanitised == expected

    def test_a_bare_five_digit_run_is_still_monetary_by_form(self):
        """The residual, deliberately left as a FAIL-SAFE.

        A 5+-digit run with nothing joined to it is monetary by form under this
        agent's stated output schema, so an unpunctuated code written that way
        ("CPT 99213") is snapped. There is no delimiter to guard on, and
        exempting bare runs would re-open the rendering-regression leak the
        form rule exists to catch. Codes that must survive are rendered joined
        ("CPT-99213", asserted above); the one caller-supplied identifier this
        agent renders verbatim - the claim reference - is joined by
        construction. Pinned so the trade-off is a decision, not a surprise.
        """
        assert _enforce_precision("CPT 99213")[0] == "CPT 99,000"
        assert _enforce_precision("CPT-99213")[0] == "CPT-99213"

    @pytest.mark.parametrize(
        "text",
        [
            "第12345条",
            "受付番号12345番",
            "契約者12345名",
            "支払件数 380,500件",
        ],
        ids=["article-no", "receipt-no", "counted-persons", "item-count"],
    )
    def test_a_japanese_structural_number_is_byte_identical(self, text):
        """An ASCII guard class is not a boundary in Japanese prose.

        This agent summarises Japanese claim documents, and 条 番 名 件 are all
        `\\w` characters. A guard written as `[A-Za-z0-9_-]` walks straight into
        the digit run of 第12345条 and reports an article number the policy does
        not contain; the same for a count written 380,500件, which was never an
        amount. The guards are `\\w`-based for exactly this reason.
        """
        sanitised, redactions = _enforce_precision(text)
        assert sanitised == text
        assert redactions == 0

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("12345円", "12,000円"),
            ("380,500円", "380,000円"),
            ("1,234,567円", "1,235,000円"),
            ("修理費用 380,500 円", "修理費用 380,000 円"),
            ("￥9999", "￥10,000"),
        ],
        ids=["bare-yen", "grouped-yen", "millions-yen", "spaced-yen", "fullwidth"],
    )
    def test_a_japanese_amount_still_snaps(self, text, expected):
        """The other direction, and the reason the trailing guard exempts symbols.

        円 and ₩ are word characters, so a plain `(?!\\w)` trailing guard refuses
        the whole grouped token in "380,500円" — and the engine then re-enters
        the run after the comma and snaps the tail alone, yielding "380,0円": a
        worse corruption than the one the guard closes. A currency symbol on the
        right is the signal that the digits ARE an amount, so it never blocks.
        """
        sanitised, _ = _enforce_precision(text)
        assert sanitised == expected

    def test_a_policy_number_and_an_amount_on_the_same_line(self):
        text = "Policy POL-2026-0012345 pays JPY 1234.56 per day"
        sanitised, redactions = _enforce_precision(text)
        assert sanitised == "Policy POL-2026-0012345 pays JPY 1,000 per day"
        assert redactions == 1


class TestStructuralReferenceHoldOut:
    """The claim reference is an identifier, not an amount."""

    REFERENCE = "clm-2026-004567"

    def test_the_raw_grid_no_longer_touches_the_reference(self):
        """The identifier guards make the hold-out belt-and-braces, not load-bearing.

        This assertion used to run the other way: the grid DID corrupt the
        reference ("clm-2026-004567" -> "clm-2026-004,000"), and the hold-out
        was the only thing standing between a claim record and a rewritten
        digit run. The grammar now refuses to enter a digit run that is joined
        to anything, so the reference survives the grid on its own - and the
        hold-out has a second, independent layer under it rather than being the
        single point of failure.
        """
        text = f"Claim Reference: {self.REFERENCE}"
        raw, redactions = _enforce_precision(text)
        assert raw == text
        assert redactions == 0

    def test_the_hold_out_is_not_what_keeps_the_reference_intact(self):
        """Pins WHICH layer is doing the work, so the guards stay falsifiable.

        `_enforce_precision_outside_reference` only substitutes a placeholder
        when the grammar would actually match the reference. No value the
        ingest boundary accepts (`^[a-z][a-z0-9_-]{1,31}$`) can match it now:
        every digit run inside one is preceded by a letter, a digit, `-` or
        `_`. If a future grammar change re-opens that, this fails here rather
        than silently hiding behind the hold-out.
        """
        assert _NUM_TOKEN_RE.search(self.REFERENCE) is None
        assert _NUM_TOKEN_RE.search("clm_2026_004567") is None
        assert _NUM_TOKEN_RE.search("a-1234567") is None

    def test_reference_survives_byte_identical(self):
        text = f"Claim Reference: {self.REFERENCE}"
        held, redactions = _enforce_precision_outside_reference(text, self.REFERENCE)
        assert held == text
        assert redactions == 0

    def test_money_next_to_the_reference_still_snaps(self):
        text = f"{self.REFERENCE} total 380,500 and JPY 1,234"
        held, redactions = _enforce_precision_outside_reference(text, self.REFERENCE)
        assert self.REFERENCE in held
        assert "380,000" in held and "JPY 1,000" in held
        assert redactions == 2

    def test_a_reference_the_grammar_ignores_needs_no_hold_out(self):
        text = "clm-1 total 380,500"
        held, redactions = _enforce_precision_outside_reference(text, "clm-1")
        assert held == "clm-1 total 380,000"
        assert redactions == 1

    def test_no_reference_supplied_still_enforces_the_grid(self):
        held, redactions = _enforce_precision_outside_reference("total 380,500", None)
        assert held == "total 380,000"
        assert redactions == 1


class TestGateLayerOrder:
    """Pattern scans run BEFORE the numeric snap — and again after it.

    A numeric rewrite destroys the shape a pattern scan recognises: the secret
    is then mangled instead of redacted, the pattern no longer matches, and the
    remains of the number ship. The identifier guards removed the hyphenated
    collision (an SSN is no longer entered part-way through), but they cannot
    remove the class — a bare digit run carries no delimiter to guard on — so
    the ordering is still load-bearing and is pinned below on a form that still
    collides.
    """

    @pytest.mark.parametrize(
        "probe,secret",
        [
            ("Claimant SSN 123-45-6789 on record.", "123-45-6789"),
            ("TAX 987-65-4321 filed with the claim.", "987-65-4321"),
        ],
        ids=["ssn", "tax-id"],
    )
    def test_hyphenated_secret_survives_the_snap_and_is_still_redacted(self, probe, secret):
        """Both halves, in one place.

        This used to assert that the snap MANGLED the secret ("SSN 123-45-6789"
        -> "SSN 0-45-6789") — a rewrite of caller data that was never monetary.
        The guards now leave it byte-identical, and the gate still redacts it,
        so the secret is neither rewritten nor released.
        """
        snap_first, redactions = _enforce_precision(probe)
        assert snap_first == probe  # the snap no longer touches it at all
        assert redactions == 0

        gated, violation, counts = apply_output_gate(probe)
        assert violation is None
        assert "[REDACTED:ssn]" in gated  # the gate redacts it
        assert secret not in gated
        assert "0-45-6789" not in gated and "1,000-65-4321" not in gated
        assert counts["pii"] == 1

    def test_a_bare_digit_run_still_proves_the_ordering(self):
        """The collision the guards cannot close, so the order still matters.

        A payment-card number written without separators is a standalone
        digit run: monetary by form, with no delimiter for an identifier guard
        to key on. Snapping it first replaces it with a grouped number that the
        card pattern no longer recognises, so the PAN would ship mangled but
        unredacted. Scanning first redacts it.
        """
        pan = "4111111111111111"
        probe = f"Card on file {pan} for settlement."

        snap_first, _ = _enforce_precision(probe)
        assert pan not in snap_first  # the snap alone destroys the pattern...
        assert "[REDACTED:payment_card]" not in snap_first
        assert "4,111,111,111,111,000" in snap_first

        gated, violation, counts = apply_output_gate(probe)
        assert violation is None
        assert "[REDACTED:payment_card]" in gated  # ...the gate redacts it instead
        assert pan not in gated
        assert "4,111,111,111,111,000" not in gated
        assert counts["pii"] == 1

    def test_credential_scan_withholds_before_anything_is_rewritten(self):
        text = "summary token=sk-abcdefghij0123456789ABCDEF total 380,500"
        gated, violation, counts = apply_output_gate(text)
        assert violation == "api_key_pattern"
        assert counts == {"pii": 0, "precision": 0}
        assert gated == text  # untouched — the caller withholds it entirely

    def test_clean_text_gets_the_grid_and_nothing_else(self):
        gated, violation, counts = apply_output_gate("Estimated total 380,500 JPY")
        assert violation is None
        assert gated == "Estimated total 380,000 JPY"
        assert counts == {"pii": 0, "precision": 1}

    def test_email_and_phone_are_masked_at_the_boundary(self):
        gated, violation, counts = apply_output_gate("call 090-1234-5678 or mail a@b.com")
        assert violation is None
        assert "090-1234-5678" not in gated and "a@b.com" not in gated
        assert counts["pii"] == 2
