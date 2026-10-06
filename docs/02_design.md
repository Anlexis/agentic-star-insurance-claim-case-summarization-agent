# Template Design Specification — INS-C2-004 Insurance Claim Case Summarization Agent

## Position in the AgentCore architecture

| Item | Value |
|---|---|
| Agent class | `InsuranceClaimCaseSummarizationAgent` |
| L1 Base (framework base class) | `AgentBaseGraph` — direct framework inheritance |
| Pattern | Cat 2 — chat/summarization pipeline (two-layer nested workflow) |
| Entry point | `src.graph.graph.InsuranceClaimCaseSummarizationAgent` (`config/agent.yaml`) |

**Three-layer separation**

- **State** — flat `TypedDict` composition (never a Pydantic model: checkpoints are
  msgpack-serialised). Every dict/list-valued field is stored as a JSON-encoded
  `Optional[str]`.
- **Node** — framework inheritance, `execute(self, state) -> dict` override only, returning
  a partial dict of the keys the node writes.
- **Graph** — composition: `register_nodes()` for node substitution; the Cat 2 inner graph is
  reached through a `GraphNode`.

## Domain context

Claims handlers and underwriters spend a large share of their day reading multi-document claim
files — First Notice of Loss, medical reports, photos, adjuster notes, repair estimates. This
agent condenses a complete claim case into one structured summary so a handler can triage it in
seconds.

Two domain constraints shape the design:

- **Policyholder personal data must not persist beyond the session.** A dedicated
  `PiiRedactNode` masks personal data in the generated sections before they leave the pipeline,
  and the output boundary sweeps again.
- **Precise monetary figures must not leave on the external surface.** Every published figure is
  expressed on a fixed 1,000 grid, and per-line estimate amounts are never rendered.

## Architecture overview

### Backbone (outer `AgentBaseGraph` — fixed 5-node pipeline)

```
START → initialize → pre_process → main(GraphNode) → post_process → finalize → END
                                         ↓ (retry, max 3)
                                       pre_process
```

### Inner domain workflow (`DomainWorkflowGraph` — linear 5-node pipeline)

```
START → input_validate → build_context → generate_response
          → pii_redact → output_format → END
```

`GraphNode.execute()` does not forward `input_context` into `subgraph.invoke()`, so the caller's
damages data is carried across the boundary by a ContextVar bridge
(`src/graph/context_bridge.py`): `ClaimSummaryGraphNode.extract_input()` stashes it and
`DomainWorkflowGraph._extra_initial_state()` seeds it into the inner state.

### Node configuration

| Node | Class | File | Trust | Responsibility | Input keys | Output keys |
|------|-------|------|-------|---------------|------------|-------------|
| initialize | InitializeNode | framework | — | session init | — | session_id, schema_version |
| pre_process | PreProcessNode | src/nodes/pre_process_node.py | VERIFIED_EXTERNAL | trust gate + payload/caller-data validation | user_input, input_context | validated_input, enriched_context |
| main | ClaimSummaryGraphNode | src/graph/graph.py | — | delegates to DomainWorkflowGraph; bridges input_context | validated_input, input_context | claim_reference, claim_summary, redacted_sections, pii_findings, damages_assessment |
| post_process | PostProcessNode | src/nodes/post_process_node.py | ANONYMOUS | output boundary + set formatted_output | claim_summary, claim_reference | formatted_output, result |
| finalize | FinalizeNode | framework | — | response metadata | — | response_metadata, total_time_ms |
| input_validate (inner) | InputValidateNode | src/nodes/input_validate_node.py | ANONYMOUS | reference normalisation, closed vocabularies, document typing/index | validated_input | claim_case, claim_reference |
| build_context (inner) | BuildContextNode | src/nodes/build_context_node.py | ANONYMOUS | group documents by type; compute damages aggregates | claim_case, input_context | assembled_context, damages_assessment |
| generate_response (inner) | GenerateResponseNode | src/nodes/generate_response_node.py | ANONYMOUS | generate the 6 summary sections | assembled_context | summary_sections |
| pii_redact (inner) | PiiRedactNode | src/nodes/pii_redact_node.py | ANONYMOUS | mask policyholder personal data | summary_sections | redacted_sections, pii_findings |
| output_format (inner) | OutputFormatNode | src/nodes/output_format_node.py | ANONYMOUS | render sections on the monetary grid; assemble the document | redacted_sections, pii_findings, claim_reference | claim_summary, redacted_sections, result |

### Data flow

```
user_input (JSON claim-case payload) + input_context (caller damages data)
    │
    ▼ PreProcessNode (VERIFIED_EXTERNAL — trust gate + caller-data contract)
validated_input (normalised JSON string)
enriched_context (JSON string)
    │
    ▼ ClaimSummaryGraphNode → DomainWorkflowGraph  (input_context via the context bridge)
    │   InputValidateNode      → claim_case (JSON string), claim_reference (identifier)
    │   BuildContextNode       → assembled_context (JSON string), damages_assessment (JSON string)
    │   GenerateResponseNode   → summary_sections (JSON string, inner-only)
    │   PiiRedactNode          → redacted_sections (JSON string), pii_findings (JSON string)
    │   OutputFormatNode       → claim_summary (str), redacted_sections (rendered, on-grid)
    ▼ merge_output
claim_reference, claim_summary, redacted_sections, pii_findings, damages_assessment → outer state
    │
    ▼ PostProcessNode (ANONYMOUS — output boundary)
formatted_output (gated claim_summary), result
    │   on a block: formatted_output = withholding notice (truthy), result = the same,
    │               claim_summary / redacted_sections / pii_findings / damages_assessment = None
    ▼ InsuranceClaimCaseSummarizationAgent.get_output()
output (= formatted_output on success; formatted_output or None otherwise), result (success only),
claim_summary / claim_reference / redacted_sections / pii_findings / damages_assessment (success only)
```

`get_output()` extends the framework envelope rather than replacing it, and it is the second half
of the output-gate contract. The base resolves `output` as `formatted_output or result` **without
consulting status**, and on this agent `result` is the PRE-GATE summary `merge_output()` copied out
of the inner graph — so on any outcome that does not reach `post_process` (a non-success inner
result forwarded by `merge_output`, a node that raised, a terminal status routed straight to
`finalize`) an envelope that forwarded state verbatim would hand back the un-gated document. On any
non-success status `result` is therefore `None` and the `or result` fallback is re-resolved as
`formatted_output or None`, so an absent gate output stays absent.

The raw generated sections (`summary_sections`) never cross the inner-graph boundary: only the
rendered, masked, on-grid section set is published, so the structured result and the document
text can never express different figures.

### State definition

| Field | Type | Purpose | Producer |
|-------|------|---------|----------|
| validated_input | Optional[str] | Normalised claim JSON string | PreProcessNode |
| enriched_context | Optional[str] | JSON: {source, channel} | PreProcessNode |
| claim_reference | Optional[str] | Inert lowercase claim identifier | InputValidateNode |
| claim_case | Optional[str] | JSON: normalised claim payload + doc_index | InputValidateNode |
| assembled_context | Optional[str] | JSON: documents grouped by type + damages | BuildContextNode |
| damages_assessment | Optional[str] | JSON: on-grid aggregates + severity | BuildContextNode |
| summary_sections | Optional[str] | JSON: {section_name: text} × 6 sections | GenerateResponseNode |
| redacted_sections | Optional[str] | JSON: masked section dict, rendered on-grid | PiiRedactNode → OutputFormatNode |
| pii_findings | Optional[str] | JSON: [{section, kind, count}, ...] | PiiRedactNode |
| claim_summary | Optional[str] | Final formatted summary text | OutputFormatNode |
| result | Optional[str] | Same as claim_summary | OutputFormatNode / PostProcessNode |

**Serialisation constraint**: all dict/list-valued fields use JSON-serialised `Optional[str]`;
`to_json()` / `from_json()` in `src/schemas/state.py` are used at every producer/consumer
boundary — one contract end to end.

Every field is declared `Optional[str]`; the framework's state base is the single source of
the shared fields.

**Prohibited**: re-declaring `formatted_output` (inherited from `AgentState`), credentials in
State, Pydantic models.

### Input payload schema (`user_input` JSON)

```json
{
  "claim_id": "CLM-2026-004567",
  "claim_type": "auto",
  "date_of_loss": "2026-07-01",
  "status": "open",
  "claimant": {"name": "…", "contact": "…"},
  "documents": [
    {"type": "FNOL", "content": "Rear-end collision at intersection; no injuries reported at scene."},
    {"type": "medical_report", "content": "Whiplash; 2 weeks physiotherapy recommended."},
    {"type": "adjuster_note", "content": "Estimated repair cost 320,000 JPY; total loss unlikely."},
    {"type": "photo", "caption": "Front bumper damage, driver side."}
  ]
}
```

Required: `claim_id` and a non-empty `documents` list. At most 100 documents.

### Caller-data contract (`input_context`)

Every field is validated against explicit bounds at the ingest boundary. A violation is a value
the caller can correct, so the run **completes** (`status=success`) carrying the reason instead of
terminating: the message names the **field**, never the rejected value, and no summary is produced.
Completing is what makes the reason reachable — terminating would end the calling surface's turn
and surface only a failure status, leaving the field name in the audit trail alone, where the
caller cannot act on it. See *Two ways to stop* below for the outcomes that do terminate.

| Field | Contract | Absent |
|---|---|---|
| `channel` | `^[a-z0-9_]{1,32}$` | `"unknown"` |
| `deductible` | finite number, 0 … 1e12 | 0 |
| `policy_limit` | finite number, 0 … 1e12 | uncapped |
| `reserve_amount` | finite number, 0 … 1e12 | no variance reported |
| `estimate_lines` | array, ≤ 20 entries of `{label: ^[a-z0-9_]{1,32}$, amount: finite 0 … 1e12}` | document-derived baseline |

Numbers go through a finite+bounded parser (`finite_in_range`). This is not paranoia:
`float("NaN")` parses, Python's `json` accepts bare `NaN` in a request body, and every IEEE NaN
comparison is False — a NaN threshold would silently pass every check it is supposed to stop.
Unknown keys are ignored; the `/invoke` adapter separately caps the serialised size at 256 KB.

### Two ways to stop

A run that does not produce a summary stops in one of two ways, and the caller can act on only
one of them. The distinction is a deliberate contract, not an implementation detail: presenting a
refusal as though a reworded request would get past it is as wrong as terminating on a typo.

| | **Declined — the caller can correct it** | **Refused / terminated** |
|---|---|---|
| `status` | `success` | `error` |
| Caller sees | a sentence naming what to correct (`src/services/failure_message.py`) | the envelope's failure status; `output` is `null` |
| Conversation | continues — a corrected request can be sent on the same turn | ends |

**Declined (`status=success`)** — everything the ingest boundary (`PreProcessNode`) settles:
an empty payload, a payload over the size cap, malformed JSON, a JSON root that is not an object,
a missing `claim_id` or `documents`, and any `input_context` field outside its bound. The node
writes an internal marker instead of a validated payload; `ClaimSummaryGraphNode.execute()` sees
the marker and skips the inner workflow entirely, and `PostProcessNode` renders the marker as the
caller-facing sentence. Running the workflow anyway would only produce a second, vaguer reason for
the same rejection and overwrite the specific one already settled.

**Refused (`status=error`)** — outcomes where a corrected request is not the remedy:
instruction-override content anywhere in the decoded claim payload (`PreProcessNode`) and a
credential pattern in the assembled summary (`PostProcessNode`). Neither is described to the
caller in terms that suggest rewording, and the credential case additionally clears every
output-bearing field (see *Blocking also contains* below).

**Terminated (`status=error`)** — a precondition the previous node was required to satisfy is
absent: `claim_case`, `assembled_context`, `summary_sections` or `redacted_sections`. This also
covers the inner workflow's own validation — an invalid claim reference, an empty `documents`
list, or more than 100 documents. `InputValidateNode` stops there and writes no `claim_case`, so
the run ends at `BuildContextNode` with nothing to assemble; only the ingest boundary's declines
reach the caller as a correctable sentence.

### Damages computation

With an estimate table supplied, the Damages Assessment section reports **aggregates only**:

```
estimated_total   = Σ estimate_lines[].amount
net_exposure      = min( max(estimated_total − deductible, 0), policy_limit )
reserve_variance  = estimated_total − reserve_amount        (when a reserve is supplied)
severity          = low | moderate | high | severe          (net_exposure vs the configured bands)
```

Every published figure is snapped onto the 1,000 grid before it is stored, so the structured
result and the rendered document agree. Individual line amounts are never published — only the
labels and the count. The severity band selects the routing sentence in Recommended Action
(fast-track → claims handler → senior handler + reserve review → major-loss escalation). Without
an estimate table the agent degrades to the document-derived narrative and recommends manual
assessment.

### Output summary sections

1. **Case Overview** — reference, type, date of loss, case status, whether claimant details are
   on file, document count
2. **Incident Facts** — what happened (from FNOL)
3. **Documentation Summary** — documents on file, grouped by type
4. **Injury / Medical Summary** — medical findings (from medical reports)
5. **Damages Assessment** — computed aggregates + adjuster/estimate narrative
6. **Recommended Action** — missing documentation, then the severity-driven routing decision

A trailer records how many personal-data redactions were applied, and — when the document
actually renders money — the schema note "Monetary figures in this summary are expressed in
units of 1,000."

## Security configuration

| Layer | Gate | Implementation |
|-------|------|---------------|
| Trust enforcement | Caller trust | `PreProcessNode.required_trust_level = VERIFIED_EXTERNAL`; the standalone `/invoke` adapter elevates a Bearer-authenticated caller |
| Input validation | Structural + domain | PreProcessNode (size cap, control-character strip, JSON shape, caller-data contract) + InputValidateNode (inert reference, closed vocabularies, document cap) |
| Output boundary | `_security_gate_output()` + the layered gate | Module-level function in `post_process_node.py`, also exposed on the agent class; scans for API keys, JWTs, Bearer tokens and credential assignments |
| Audit logging | `emit_trace_event()` | At least one domain event in every node `execute()` |
| Credential handling | No credentials in State | Secrets only via the invocation context; the manifest declares none |
| Personal data | Policyholder PII | `PiiRedactNode` masks email / phone / national-ID / payment-card / social-security-style patterns; the output boundary re-sweeps |

### Output boundary layer order

The boundary runs four layers in a deliberate order:

1. credential scan — a hit withholds the summary entirely (sanitised stub, `status=error`);
2. residual personal-data sweep — the same pattern set the domain redaction step uses;
3. monetary precision grid — every monetary-form token snapped onto the 1,000 grid;
4. both scans again over the snapped text.

**The order is load-bearing.** A numeric rewrite destroys the shape a pattern scan recognises, so
the secret is mangled instead of redacted and the remains of the number ship. Pattern scans
therefore always precede the numeric rewrite, and run again after it. The identifier guards below
removed the hyphenated collision (`SSN 123-45-6789` now survives the grid untouched), but they
cannot remove the class: a payment-card number written without separators is a bare digit run with
no delimiter to guard on, and snapping it first would leave it grouped and unredacted.

**Recognition scope (credential layer).** The scan runs this template's own pattern set first — it
is broader than the framework's for shapes that matter in claim prose, such as a `password:` or
`token=` assignment in an adjuster note — and then the framework's own `detect_credentials()` over
whatever the domain set let through. The framework arm is not defence in depth: the framework scans
every node result with that same detector and *raises* on a hit, and a raise discards the node's
whole return **including the containment below**, leaving the pre-gate summary in state for the
envelope to read. A shape the framework refuses and this gate missed is therefore a containment
bypass, not merely a narrower gate. Four shapes fell in that gap before the parity arm was added:
`AKIA…`, `sk_live_…`, a single-segment `eyJ…`, and a `postgresql://…` connection string.

**Blocking also contains.** The envelope resolves the caller-facing value as
`formatted_output or result` **without consulting status**, and `result` carries the PRE-GATE
summary that `merge_output()` copied out of the inner graph. Returning `status=error` while leaving
the output-bearing state populated therefore ships the refused document inside the error envelope.
The block overwrites every field that carries assembled answer text or a structured payload —
`claim_summary`, `redacted_sections`, `pii_findings`, `damages_assessment` (the inventory
`_OUTPUT_BEARING_FIELDS`, pinned against `merge_output()`'s key set by a test so a future field
cannot quietly join it) — and the `formatted_output` replacement is deliberately non-empty, because
a falsy one hands `formatted_output or result` straight back to the value the gate just refused.
`claim_reference` is deliberately left: it is an inert validated identifier carrying no claim
content, and the envelope withholds it on any non-success outcome. Violations name the pattern
CLASS only, never the matched value — echoing it would put the refused string back into this node's
own result, where the framework scan raises and discards the clearing.

The claim reference is held out of the numeric rewrite by exact value. Since the identifier guards
were added this hold-out is belt-and-braces rather than load-bearing: no value the ingest boundary
accepts (`^[a-z][a-z0-9_-]{1,31}$`) can match the monetary grammar any more, because every digit
run inside one is joined to a letter, a digit, `-` or `_`. It is kept as a second, independent
layer, and a test pins which of the two is actually doing the work.

### Monetary grammar — what is an amount and what is a record reference

A monetary value is identified by FORM (comma-grouped numbers, and unformatted runs of 5+ digits)
and by CURRENCY CONTEXT (a 1–4 digit number beside a currency marker), never by magnitude. Four
properties keep it from rewriting the claim record itself:

- **Decimal absorption.** Every value alternative takes its fraction into the same token, written
  `(?:\.\d+|(?!\.\d))` — take the fraction whole, or assert there is not one. A plain
  `(?:\.\d+)?` lets the engine backtrack out of the fraction and re-match the integer part
  whenever the text after it fails the trailing guard, which is how `JPY 1234.56m` still came out
  `JPY 1,000.56m`. `_snap` parses `float`, so an off-grid decimal amount snaps as ONE number
  (`JPY 1234.56` → `JPY 1,000`) while a ratio or percentage that was never monetary is untouched.
- **Identifier guards on the unmarked value alternatives.** The class is `\w` plus `/`, `.` and
  `-` — `\w`, not an ASCII literal set, because this agent summarises Japanese claim documents
  where 条 番 名 件 are word characters and an ASCII guard walks straight into the digit run of
  第12345条. `.` and `/` are LEADING-only: in the trailing guard `.` would let an amount ending a
  sentence escape the grid and `/` would exempt a rate. A currency SYMBOL is exempt from the
  trailing guard, because 円 and ₩ are themselves word characters and blocking there re-enters the
  run after the comma and snaps the tail alone (`380,500円` → `380,0円`).
- **Attached markers restricted to ISO-4217 codes.** `<3 upper-case letters>-<digits>` is the shape
  this domain's record references take (POL-2026-0012345, ADJ-4821, NAIC-12345) and is
  indistinguishable by form from an attached negative amount (JPY-9999). Naming the currencies
  resolves it in the direction that keeps claim records intact; the list is closed and stable,
  whereas an identifier list never could be. SEPARATED markers stay unrestricted (`ADJ 4821` still
  snaps), so the gate still fails safe on the genuinely ambiguous case.
- **A left boundary on the currency code**, or a longer acronym donates its tail and `STAR 2026`
  becomes `STAR 2,000`.

**Known residual, accepted as fail-safe:** a 5+-digit run with nothing joined to it is monetary by
form under this schema, so an unpunctuated code written that way (`CPT 99213`) is snapped. There is
no delimiter to guard on, and exempting bare runs would re-open the rendering-regression leak the
form rule exists to catch. Codes that must survive are rendered joined (`CPT-99213`), and the one
caller-supplied identifier this agent renders verbatim — the claim reference — is joined by
construction. Pinned by test so the trade-off is a decision rather than a surprise.

### Runtime configuration (`config/config.yaml`)

```yaml
max_retry: 3
timeout_s: 30
damages:
  moderate_threshold: 100000
  high_threshold: 1000000
  severe_threshold: 10000000
llm:
  system_prompt_template: "prompts/claim_summary.j2"
  temperature: 0.0
  max_tokens: 4000
```

`config/agent.yaml` is the static registration manifest and carries no runtime parameters. The
registry loads `config/config.yaml` and passes it as `Graph(config=...)`; the standalone server
does the same, so `max_retry` is live in both deployments. Declared values are validated (type,
finiteness, range) before they are forwarded into the node constructors: a malformed severity
band is not adopted at all, because a half-applied band set is how a severe claim quietly becomes
a moderate one.

## Framework utilisation

- [x] Invocation context (`config["configurable"]` — session id, trust level)
- [x] Module-level `_security_gate_output()` in `post_process_node.py`, re-exposed on the agent class
- [x] `emit_trace_event()` — at least one domain-specific event per node `execute()`
- [x] `to_json()` / `from_json()` / `finite_in_range()` in `src/schemas/state.py`

### Composition pattern

- **Pattern**: Cat 2 nested two-layer — `GraphNode` wrapping an inner `BaseGraph`
- **Outer graph**: `InsuranceClaimCaseSummarizationAgent(AgentBaseGraph)` — fixed 5-node backbone
- **Inner graph**: `DomainWorkflowGraph(BaseGraph)` — 5-node linear domain pipeline
- **Error propagation**: propagate (inner failure raises out; the outer backbone retries pre_process)

## Import isolation confirmation

- [x] The template does not import the platform SDK
- [x] Import targets: `framework/` and `shared/` only

## Design decision record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| Base class | AgentBaseGraph | AutonomousBaseGraph | AgentBaseGraph | Fixed sequential summarization pipeline; no autonomous reasoning loop |
| Composition pattern | Cat 1 (flat) | Cat 2 (nested GraphNode) | Cat 2 nested | 5 sequential domain steps behind one backbone slot |
| PII handling | Inline in generate | Separate PiiRedactNode | Separate node | Single responsibility; the redaction record is a first-class output |
| State dict fields | bare dict | JSON-serialised str | JSON-serialised str | msgpack serialisation safety |
| Claim reference | echo caller value | inert lowercase identifier | inert identifier | It is the one caller string rendered verbatim; lowercase also removes any currency-marker ambiguity at the output grid |
| Claimant details | render name/contact | render presence only | presence only | The summary needs "details on file", not the person's name |
| Monetary rendering | full precision | 1,000 grid, aggregates only | grid + aggregates | Precise per-line figures are the leak; the grid is enforced independently of the renderer |
| Damages input | parse from documents | validated `input_context` | validated `input_context` | Bounded, typed, fail-closed — free-text amount parsing is neither |
| Summary generation | live LLM | deterministic synthesis | deterministic | The framework ships no LLM client; the declared settings are forwarded for a live-LLM build |
