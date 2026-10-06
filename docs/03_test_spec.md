# Test Specification — INS-C2-004 Insurance Claim Case Summarization Agent

## 1. Test strategy

- **Agent**: INS-C2-004 — Insurance Claim Case Summarization Agent (Cat 2, two-layer nested
  graph: outer `AgentBaseGraph` backbone + inner `DomainWorkflowGraph`).
- **Coverage target**: ≥ 90% of `src/nodes/` + `src/graph/` branches.
- **Test types**: unit (per node, per boundary contract, graph wiring) · proof-of-boundary
  (framework security/serialisation contracts, backbone invoke order, end-to-end `/invoke`) ·
  integration (compiled outer graph result surfacing).
- **Framework provisioning**: `framework` (`agenticstar-agentcore`) is supplied by the CI
  environment. Tests import the real modules; there are no stub nodes.
- **Audit events**: `emit_trace_event` is patched at the node module level in unit tests to avoid
  audit-backend calls, never via a `sys.modules` stub (which would break the real `shared`
  package the framework loads at import time).
- **Node invocation**: every unit test invokes a node through `BaseNode.__call__` (`node(state)`),
  not `execute()` directly, so the framework's trust / input / output gates run exactly as they
  do in production.

### Test file map

| File | Tests | Scope |
|------|------:|-------|
| `tests/unit/test_nodes.py` | 114 | All 7 domain/backbone nodes + outer & inner graph wiring |
| `tests/unit/test_caller_data_and_output_schema.py` | 191 | Caller-data contract, damages arithmetic, config plumbing, output-boundary schema |
| `tests/unit/test_main_node.py` | 3 | Retained `MainNode` stub — `execute()` contract kept green |
| `tests/proof_of_boundary/test_invoke_e2e.py` | 48 | End-to-end business behaviour through the real ASGI `POST /invoke` |
| `tests/proof_of_boundary/test_pb_invoke_order.py` | 7 | PB-6 per-node + backbone invoke order, trust gate, request-payload alignment |
| `tests/proof_of_boundary/test_import_isolation.py` | 1 | PB-4 platform-SDK import isolation (AST scan) |
| `tests/proof_of_boundary/test_state_safety.py` | 1 | PB-2/PB-5 State serialisation/credential safety (AST scan) |
| `tests/proof_of_boundary/test_pb7_hitl_interrupt_propagation.py` | 1 | PB-7 HITL interrupt propagation (skip stub — no cross-boundary HITL) |
| `tests/integration/test_outer_invoke_returns_domain_result.py` | 3 | Compiled outer graph surfaces the domain result; every representation on-grid |

### Node pipeline under test

```
Outer backbone (AgentBaseGraph):
  InitializeNode → PreProcessNode (trust gate, VERIFIED_EXTERNAL)
    → ClaimSummaryGraphNode (main slot, GraphNode → inner DomainWorkflowGraph)
    → PostProcessNode (output boundary, ANONYMOUS) → FinalizeNode

Inner DomainWorkflowGraph (BaseGraph, all ANONYMOUS):
  input_validate → build_context → generate_response → pii_redact → output_format
```

### Canonical request (PB-6 `_VALID_PAYLOAD` + `_VALID_CONTEXT`)

The success-yielding request used by the backbone invoke test and by
`deploy/invoke_payload.json` — the two must stay identical, asserted by
`test_invoke_payload_matches_pb6`. The FNOL document deliberately carries a phone number and an
email so personal-data handling is exercised end to end; the caller context carries a full
estimate table so the damages aggregates are exercised too.

```json
{
  "input": "{\"claim_id\": \"CLM-2026-004567\", \"claim_type\": \"auto\", \"date_of_loss\": \"2026-06-15\", \"status\": \"open\", \"claimant\": {…}, \"documents\": [ …FNOL with 090-1234-5678 / witness@example.com…, adjuster_note, medical_report, estimate, photo ]}",
  "input_context": {
    "channel": "claims_portal",
    "deductible": 50000,
    "policy_limit": 5000000,
    "reserve_amount": 300000,
    "estimate_lines": [{"label": "parts", "amount": 210000}, {"label": "labour", "amount": 150000}, {"label": "paint", "amount": 20000}]
  }
}
```

Required (PreProcessNode): `claim_id` + non-empty `documents`. Downstream inner nodes normalise,
summarise, mask personal data and assemble the final summary ⇒ `status = success`.

## 2. Framework compliance tests

| TC-ID | Test | Expected result | Where |
|-------|------|----------------|-------|
| TC-01 | State contract: flat `TypedDict`, domain fields `Optional[str]`, no Pydantic/dataclass | AST scan: 0 violations | `test_state_safety.py` |
| TC-02 | Invalid / empty / oversized / non-JSON input declined at PreProcessNode | `status=success` carrying the correctable reason; error_log populated; no summary produced | `TestPreProcessNode` |
| TC-02b | Instruction-override content refused at PreProcessNode | `status=error`; nothing carried forward; attack text never echoed | `TestPreProcessInjectionRefusal` |
| TC-03 | No credential-shaped field in State | AST scan: 0 violations | `test_state_safety.py` |
| TC-04 | `execute(self, state)` contract — no `_invoke_impl` | signature `(self, state)`, `_invoke_impl` absent | `test_execute_signature_is_state_first`, `test_main_node.py` |
| TC-05 | An audit event is emitted inside each node `execute()` | ≥ 1 domain event per node | exercised through every node test |
| TC-06 | Trust gate enforced in `__call__` before `execute()` | ANONYMOUS caller refused; VERIFIED_EXTERNAL admitted | `TestTrustGate` (unit + PB) |
| TC-07 | Outer `PreProcessNode` = VERIFIED_EXTERNAL; inner nodes + post_process = ANONYMOUS | trust levels asserted per node | `test_trust_level_*` |
| TC-08 | Output boundary on post_process | credential pattern → withheld + `status=error`; clean → pass | `TestPostProcessNode`, `TestGateLayerOrder` |
| TC-09 | Entry-point auth on the standalone adapter | missing / wrong / non-ASCII Bearer → 401; oversized context → 413 | `TestEntryPointAuth` |
| TC-10 | Prompt-injection content in a source document | refused end to end; nothing published (behaviour asserted, never a refusal message) | `test_prompt_injection_in_a_document_is_refused` |

## 3. Proof-of-boundary tests

| PB-ID | Boundary | Test | Expected result | Where |
|-------|----------|------|----------------|-------|
| PB-2 | State serialization | AST scan of `src/schemas/state.py` | primitives only; no Pydantic/dataclass | `test_state_safety.py` |
| PB-4 | Import isolation | AST scan of `src/` | 0 platform-SDK imports | `test_import_isolation.py` |
| PB-5 | Checkpoint safety | no credential-named fields / prohibited types in State | inspection pass | `test_state_safety.py` |
| PB-6 | Invoke execution order (per node) | `__call__`: node_start → trust gate → input gate → `execute()` → output gate → node_complete | order verified for every `src/nodes/` class | `TestInvokeOrder` |
| PB-6b | Backbone invoke order | full `Graph().invoke(_VALID_PAYLOAD, ctx=VERIFIED_EXTERNAL, input_context=_VALID_CONTEXT)` | `status=success`; node_history = `[Initialize, PreProcess, ClaimSummaryGraphNode, PostProcess, Finalize]` | `TestBackboneInvokeOrder` |
| PB-6c | Real external caller | `InvocationContext(caller_trust_level=VERIFIED_EXTERNAL)` — **never** `for_internal()` | inner ANONYMOUS nodes accept the passthrough trust; SUCCESS end to end | `TestBackboneInvokeOrder` |
| PB-6d | Request alignment | `deploy/invoke_payload.json` input **and** input_context equal the PB-6 constants | the deployed first invoke exercises the PB-6 request | `test_invoke_payload_matches_pb6` |
| PB-7 | HITL interrupt propagation | skip stub — `propagate_hitl=False`, no cross-boundary `interrupt()` checkpoint | skipped with reason | `test_pb7_hitl_interrupt_propagation.py` |
| PB-E2E | Real ASGI `POST /invoke` | auth boundary, real summary, real aggregates, every severity path, validation rejections, prompt-injection refusal, redaction, on-grid scan; OUTPUT-GATE CONTAINMENT — a clean-path control proves the same request does produce the full assessment, then a gate block and a skipped gate (non-success inner result forwarded by `merge_output`) each return an envelope carrying no summary, claim reference, monetary figure, refused caller content, traceback or source path, with `PostProcessNode` present in `node_history` on the block and absent on the skip | 48 cases | `test_invoke_e2e.py` |

## 4. Business logic tests

| BL-ID | Test | Input | Expected result | Where |
|-------|------|-------|----------------|-------|
| BL-01 | Happy-path summary generation | canonical request | 6-section summary; header + claim reference present | `test_claim_documents_produce_a_real_summary`, `TestInnerDomainGraph` |
| BL-02 | Claim-reference normalisation | `"CLM-2026-004567"` | `"clm-2026-004567"` (inert identifier) | `test_reference_is_normalised_to_an_inert_identifier` |
| BL-03 | Invalid claim reference declined | leading digit / space / symbol / length | `status=success` naming `claim_id`; value never echoed | `test_invalid_reference_fails_closed`, `test_rejected_reference_value_is_never_echoed` |
| BL-04 | Claim-type normalisation + closed vocabulary | `"motor"` / injected markup | `"auto"` / `"other"` | `test_claim_type_alias_normalised`, `test_unknown_claim_type_is_not_echoed` |
| BL-05 | Document typing + per-type index | `["first_notice_of_loss","med_report","invoice"]` | `["FNOL","medical_report","estimate"]` + `doc_index` | `test_document_types_and_index_normalised` |
| BL-06 | Document cap + long-document truncation | 101 documents / 5,000-char document | `status=success` naming the cap / `[truncated]` marker | `test_document_cap_is_enforced`, `test_long_document_is_truncated` |
| BL-07 | Claimant details reduced to presence | claimant object | `claimant_on_file: true`; no name in state | `test_claimant_details_are_reduced_to_presence` |
| BL-08 | 6-section synthesis + placeholders | assembled context, missing FNOL / medical report | all 6 keys; placeholder text | `TestGenerateResponseNode` |
| BL-09 | Damages aggregates | estimate table + deductible + limit + reserve | total / net exposure / reserve variance, all on-grid | `TestDamagesArithmetic`, `test_caller_damages_drive_real_aggregates` |
| BL-10 | Severity bands + routing | exposure across all four bands | `low`/`moderate`/`high`/`severe` + distinct routing sentence | `test_severity_bands`, `test_every_severity_path_is_reachable` |
| BL-11 | Degradation without caller data | no `estimate_lines` | document-derived narrative, manual-assessment recommendation | `test_absent_damages_degrades_to_the_document_baseline` |
| BL-12 | Policyholder PII redaction | email / phone / SSN / payment card | `[REDACTED:<kind>]`; per-section findings; clean text untouched | `TestPiiRedactNode` |
| BL-13 | Final summary assembly | rendered sections + findings | 6 headers + redaction trailer + schema note when money renders | `TestOutputFormatNode` |
| BL-14 | Graph key coupling | inner `get_output` ↔ outer `merge_output` | 6 coupled keys mapped; `merge_output` returns changed keys only | `TestOuterGraphComposition`, `TestInnerDomainGraph` |
| BL-15 | Context bridge | `input_context` on the OUTER invoke | reaches inner damages computation and changes the output | `test_caller_damages_reach_the_inner_graph_end_to_end` |

### Caller-data contract (fail-closed)

Every declared `input_context` field is bounds-checked at the ingest boundary and re-checked in
the inner `BuildContextNode`, so a direct inner-graph invocation gets the identical rule.

| Field | Accepted | Rejected (parametrized) |
|---|---|---|
| `channel` | `^[a-z0-9_]{1,32}$` | uppercase, symbols, empty, > 32 chars, non-string |
| `deductible` / `policy_limit` / `reserve_amount` | finite 0 … 1e12 | `"NaN"`, `"Infinity"`, `"-Infinity"`, raw `float("nan")`, raw `float("inf")`, raw `float("-inf")`, bool, text, negative, array |
| `estimate_lines[].label` | `^[a-z0-9_]{1,32}$` | uppercase, symbols, empty, > 32 chars, non-string, null |
| `estimate_lines[].amount` | finite 0 … 1e12 | the same non-finite matrix, plus a missing amount |
| `estimate_lines` | array, ≤ 20 entries | non-array, 21 entries |

An explicit `null` on an optional field means "not supplied" and is accepted. A rejection names
the field and never echoes the value (asserted).

### Output-schema enforcement (both directions)

| Case | Input fragment | Expected |
|---|---|---|
| every leak form snaps | `JPY 9999`, `9999 JPY`, `JPY 1,234`, `¥9999`, `9999円`, `JPY -9999`, `JPY\t9999`, `JPY  9999`, `JPY\n9999`, `9,999`, `123456`, `-12345`, `1,234,567` (18 forms) | snapped onto the 1,000 grid, marker / delimiter / sign preserved |
| structural tokens untouched | `p.21`, `v12`, `in 2026`, `STAR 2026`, `WAD grade 1`, `90d`, `10,000`, `JPY 1,000`, `2026-06-15`, `Documents on File: 5` | byte-identical, 0 redactions |
| claim reference held out | `clm-2026-004567` | byte-identical; money beside it still snaps |
| layer order | `SSN 123-45-6789`, `TAX 987-65-4321` | redacted (`[REDACTED:ssn]`), never mangled into `SSN 0-45-6789` |
| credential | `token=sk-…` | withheld entirely; nothing rewritten first |
| every surfaced representation | document + rendered sections + damages JSON | all monetary tokens on the grid |

### Negative / boundary cases

Per *Two ways to stop* in `docs/02_design.md`. The rows below are **node-scoped** — each asserts
what the named node returns from `node(state)`. The end-to-end status a caller receives for the
same request is pinned separately in `test_invoke_e2e.py` and listed under *End-to-end outcome*
below; the two differ for the inner-workflow rows, and both are asserted.

**Declined at the node — the caller can correct it (`status=success`)**

| Case | Node | Expected |
|------|------|----------|
| empty `user_input` | PreProcessNode | `status=success`, error_log "empty" |
| payload over the size cap | PreProcessNode | `status=success`, error_log "characters" |
| invalid JSON / root not an object | PreProcessNode | `status=success`, error_log names the shape |
| missing `documents` | PreProcessNode | `status=success`, error_log "documents" |
| invalid `input_context` field | PreProcessNode | `status=success` naming the field only |
| invalid claim reference | InputValidateNode | `status=success`, error_log "claim_id"; value never echoed |
| empty `documents` | InputValidateNode | `status=success`, error_log "documents" |
| more than 100 documents | InputValidateNode | `status=success`, error_log "at most" |

**Refused or precondition absent — terminates (`status=error`)**

| Case | Node | Expected |
|------|------|----------|
| instruction-override content in the payload | PreProcessNode | `status=error`; no `validated_input` / `enriched_context`; attack text never echoed |
| missing `claim_case` | BuildContextNode | `status=error` |
| missing `assembled_context` | GenerateResponseNode | `status=error` |
| missing `summary_sections` | PiiRedactNode | `status=error` |
| missing `redacted_sections` | OutputFormatNode | `status=error` |
| credential leak in output | PostProcessNode | withheld, `status=error`, every output-bearing field cleared |

**End-to-end outcome (`test_invoke_e2e.py`, real ASGI `POST /invoke`)**

| Request | Envelope `status` | Envelope `output` |
|---|---|---|
| empty input | `success` | the correctable sentence |
| invalid `input_context.channel` | `success` | the correctable sentence |
| invalid claim reference | `error` | `null` |
| instruction-override content in a document | `error` | `null` |

A decline settled at the ingest boundary reaches the caller as a sentence: `PreProcessNode` writes
no validated payload, the main slot skips the inner workflow, and `PostProcessNode` renders the
reason. The inner workflow's own validation stops before `claim_case` is written, so the run ends
at `BuildContextNode` and the envelope carries the failure status instead.

**Neither class — degradation and routing**

| Case | Node | Expected |
|------|------|----------|
| empty `claim_summary` | PostProcessNode | fallback message, `status=success` |
| non-success inner result forwarded by `merge_output` | backbone routing | `post_process` skipped; envelope releases nothing |
| malformed severity band in config | BuildContextNode | defaults kept wholesale; a severe claim stays severe |

### Output-schema and containment coverage (added 2026-08-31)

| Group | File | What it pins |
|---|---|---|
| `TestDecimalsAndClaimIdentifiers` | `test_caller_data_and_output_schema.py` | the grid applies to AMOUNTS, both directions: decimals/percentages/ratios and every claim identifier this agent renders (policy number hyphen- and slash-joined, adjuster and NAIC codes, CPT, ICD-10, VIN, estimate-line label, claim reference) come back byte-identical, while every leak form still snaps — including the decimal-with-suffix backtracking case and the attached ISO-code form; the bare-5+-digit-run residual is pinned as a deliberate fail-safe |
| Japanese cases in the same group | `test_caller_data_and_output_schema.py` | 第12345条 / 受付番号12345番 / 380,500件 survive (an ASCII guard class is not a boundary in Japanese prose), while 12345円 / 380,500円 / 1,234,567円 still snap |
| `TestGateLayerOrder` | `test_caller_data_and_output_schema.py` | scans still precede the rewrite — re-pinned on a bare 16-digit PAN, the collision the identifier guards cannot close |
| `TestPostProcessNode` containment + parity | `test_nodes.py` | a block clears every output-bearing field, the replacement is truthy so the envelope fallback cannot re-open the hole, the clearing inventory is pinned against `merge_output()`'s key set, violations name the class not the value, and the gate refuses every shape the framework's `detect_credentials()` refuses (with ordinary claim prose as the opposite-direction control) |
| `test_output_envelope.py` | new | `get_output()` driven directly on a state dict: success control surfaces the gated summary and the structured result; on ERROR / TIMEOUT / CANCELLED / RETRY `result` is `None`, the base `formatted_output or result` fallback is re-resolved, and every structured field is withheld |

## 5. Test execution summary

- Execution: `python -m pytest tests/ -v` against the real framework wheel.
- Total: **385 tests — 384 passed, 1 skipped** (PB-7 skip stub, by design).
- Coverage: every node exercised on success and error paths; both graphs exercised for
  composition, key coupling and a full backbone invoke; the deployed entry point exercised
  through its real ASGI interface.
