# Progress Report — AI Finance Controller

## Part 0: Scaffolding & Synthetic Dataset — completed 2026-08-24

### What was built

- Project structure: `src/`, `data/`, `reports/`, plus `README.md`, `DATA_NOTES.md`.
- `src/schemas.py` — canonical dataclasses (`InternalTransaction`, `ExternalTransaction`,
  `MatchResult`, `AuditEntry`, `ExceptionRecord`) and enums (`MatchPath`, `GLCategory`)
  used across all pipeline stages.
- `src/data_generator.py` — deterministic (seed=42) synthetic data generator producing
  65 internal records and 55 external records across 3 formats, with 7 noise types.
- All data files generated and verified.

### Key decisions & why

- **3 external formats chosen:**
  - Format A (clean CSV, 25 records) — baseline "easy parse" case.
  - Format B (messy CSV, 18 records) — currency symbols, mixed date formats, NEFT/IMPS
    prefixed refs, truncated descriptions. Tests parser robustness.
  - Format C (JSON, 12 records) — nested structure with reference IDs appearing in
    3 different locations (top-level, metadata, narrative). Tests structural flexibility.
- **No cross-format overlap** — each external record appears in exactly one format.
- **13 unmatched internal records** (not just 4 as originally planned) — this is a natural
  consequence of 65 internal minus 52 matched. Simulates pending settlements and
  out-of-scope transactions. More realistic than artificially shrinking the internal set.
- **Noise distribution weighted toward exact matches** (20/52 = 38%) — reflects real-world
  datasets where the majority of records are clean. The remaining 32 cover the hard cases.
- **Fee deduction model**: `fee = amount * rate * 1.18` where `rate ~ U(1.5%, 2.5%)` and
  the 1.18 multiplier is GST on the fee. This produces amount discrepancies of 1.77%–2.95%.

### Results so far (cumulative, full batch)

- Internal records: **65**
- External records: **55** (Format A: 25 | Format B: 18 | Format C: 12)
- Ground truth matches: **52**
- Unmatched internal: **13**
- Unmatched external: **3**
- Match rate: N/A (matching engine not yet built)
- Throughput: N/A

**Noise breakdown (exact counts):**

| Noise Type       | Count |
|------------------|-------|
| exact            | 20    |
| fee_rounding     | 8     |
| missing_ref      | 6     |
| date_drift       | 5     |
| partial_refund   | 4     |
| duplicate_amount | 6     |
| description_only | 3     |

**Transaction type breakdown:**

| Type       | Count |
|------------|-------|
| payment    | 33    |
| settlement | 19    |
| refund     | 13    |

### Known issues or shortcuts taken

- All amounts are INR — no multi-currency support. Acceptable for hackathon scope.
- The "duplicate_amount" noise only garbles the ref ID for ~1 in 3 of those records.
  The other 2 in 3 still have correct ref IDs, making them matchable by ref alone.
  This may make them easier than intended; will revisit in Part 2 if the rule-based
  matcher handles them too easily.
- Format B's messiness is column-value level (varied date/amount formatting), not
  structural (columns are consistent within the file). A truly messy export might
  have varying column counts per row — not implemented here.

### Open questions for next Part

- Should the ingestion layer normalize all amounts to 2 decimal places, or preserve
  the original precision? (Leaning toward normalizing to 2dp.)
- For Format C's reference ID that appears embedded in the narrative string
  (`| Ref: <id>`), should the parser extract it via regex, or leave it in the
  description for the matcher to deal with? (Leaning toward extracting it during
  parsing — the matcher shouldn't need to know about format-specific quirks.)

---

## Part 1: Ingestion / Parsing Layer -- completed 2026-08-24

### What was built

- `src/ingestion.py` -- multi-format parser with 4 public functions:
  - `parse_internal()` -- internal CSV
  - `parse_format_a()` -- clean bank CSV
  - `parse_format_b()` -- messy CSV (currency symbols, varied dates, prefixed refs)
  - `parse_format_c()` -- nested JSON (ref extraction from 3 locations)
  - `parse_all_external()` -- combines all 3 formats
  - `save_parse_errors()` -- persists errors as JSON for Part 5 consumption
- `src/schemas.py` updated: `amount: float` -> `amount: Decimal`, added
  `raw_description` field to `ExternalTransaction`, added `ParseError` dataclass.
- `tests/test_ingestion.py` -- 45 test assertions, all passing.
- Updated `data_generator.py` -- Format A and B now include `ext_id` columns
  (Txn ID / Bank Ref) for ground-truth validation traceability.

### Key decisions & why

- **Decimal from string, never via float**: Every `_to_decimal_2dp()` and
  `Decimal(row["amount"])` call builds Decimal directly from the cleaned string.
  No intermediate float. `ROUND_HALF_UP` passed explicitly on every `.quantize()`.
- **Opportunistic ref extraction in Format C**: Parser checks 3 locations
  (top-level `reference`, `metadata.payment_ref`, narrative regex) and records
  which path was used in `original_data["_ref_extraction_path"]`. Raw narrative
  always retained in `raw_description` regardless of extraction outcome.
- **ParseError persistence**: Errors are `ParseError` dataclass instances,
  collected in a list and saveable to `reports/parse_errors.json` via
  `save_parse_errors()`. Part 5 reads this file directly.
- **ext_id added to Format A/B**: Banks assign transaction IDs in real life;
  including them avoids a content-matching step during ground-truth validation.

### Results so far (cumulative, full batch)

- Match rate: N/A (matching engine not yet built)
- Matched via rule / matched via LLM / exceptions: N/A
- Throughput: N/A
- **Parse results**: 65 internal + 55 external = 120 total records parsed,
  **0 parse errors** across all formats.
- Test suite: **45/45 passed**.

### Format C ref extraction paths (actual observed, not assumed)

| Path             | Count | Percentage |
|------------------|-------|------------|
| reference        | 2     | 17%        |
| metadata         | 4     | 33%        |
| narrative_regex  | 1     | 8%         |
| none             | 5     | 42%        |

The assumed 50/30/20 split from the plan did not hold because: (a) only 12 records
in Format C (small sample), (b) 5 records have no ref at all (2 missing-ref noise,
1 description-only noise, 1 unmatched-external with no ref, and 1 where narrative
was randomly omitted by the generator's 12% drop). The actual distribution is
correct given the data -- no extraction bug.

### Per-noise-type counts (confirming Part 0 generation is intentional)

| Noise Type       | Count | In Format A | In Format B | In Format C |
|------------------|-------|-------------|-------------|-------------|
| exact            | 20    | 10          | 6           | 4           |
| fee_rounding     | 8     | 4           | 1           | 3           |
| missing_ref      | 6     | 2           | 3           | 1           |
| date_drift       | 5     | 2           | 2           | 1           |
| partial_refund   | 4     | 2           | 2           | 0           |
| duplicate_amount | 6     | 2           | 3           | 1           |
| description_only | 3     | 1           | 1           | 1           |
| **Total matched**| **52**| **23**      | **18**      | **11**      |

The 13 unmatched internal records are: TXN_010, TXN_064, TXN_055, TXN_047,
TXN_017, TXN_019, TXN_025, TXN_044, TXN_036, TXN_007, TXN_043, TXN_009, TXN_062.
This count is a natural consequence of 65 - 52 = 13 and is intentional, not a
generation artifact.

### Parse failures vs matching ambiguity

**No Part 0 noise category triggers a genuine parse failure.** All 7 noise types
produce structurally valid data -- the noise is in the *values* (wrong amounts,
missing ref IDs, shifted dates, etc.), not in file structure. Specifically:
- `fee_rounding`: valid amount, just a different number
- `missing_ref`: empty or garbled string in a valid column
- `date_drift`: valid date, just a different day
- `partial_refund`: valid amount, smaller than expected
- `duplicate_amount`: perfectly valid, just ambiguous
- `description_only`: valid description with merchant name
- `exact`: no noise at all

All ambiguity is pushed entirely to the matching layer (Parts 2-3), not parsing.

### Known issues or shortcuts taken

- The Format B date parser tries 4 formats in sequence (`%d-%m-%Y`, `%d/%m/%Y`,
  `%d %b %Y`, `%d-%m-%y`). If a bank produces a format outside these 4, it would
  fail. Acceptable for hackathon scope with known data.
- Format C's `| Ref:` regex is simple (`\| Ref:\s*(\S+)`). It would fail on refs
  containing spaces or if the format used a different separator. Good enough for
  our data.
- Windows cp1252 console can't render ₹ -- test output uses `ascii()` for raw
  values. Not a data issue, just a display limitation.

### Open questions for next Part

- What amount tolerance should the deterministic matcher use? The fee_rounding
  noise produces discrepancies of 1.77% to 2.95%. A 3% tolerance band would catch
  all fee cases but might also create false positives with partial_refund cases
  (25-55% difference). Need to separate these into distinct rules.
- For duplicate_amount pairs (same amount + date + merchant), should the
  deterministic matcher attempt disambiguation using ref ID alone, or should all
  ambiguous cases go to the LLM? Leaning toward: if ref ID resolves it, use rule;
  if not, send to LLM.

---

## Part 2: Deterministic Matching Engine -- completed 2026-08-24

### What was built

- `src/deterministic_matcher.py` -- 4-rule cascade matcher with pre-built indexes.
  Rules fire in order (first match wins), each external claimed at most once.
- `tests/test_deterministic.py` -- full-batch test with ground-truth validation
  (10/10 assertions passed).
- Analysis scripts `tests/analyze_rules.py` and `tests/analyze_residual.py`
  used to verify design decisions against real data before coding.

### Rule set (final, with evidence for each decision)

| Rule | Name | Logic | Hits | Confidence |
|------|------|-------|------|------------|
| 1 | exact_ref_amount_date | ref + exact amount + exact date | 24 | 1.0 |
| 2 | exact_ref_amount_window | ref + exact amount + date within 3 days | 4 | 1.0 |
| 3 | ref_fee_tolerance | ref + amount within 3% + date within 3 days | 8 | 1.0 |
| 4 | amount_date_unique | amount + date exact, bidirectionally unique | 9 | 0.95 |

**Dropped rule: `ref_id_only`** (ref match, unbounded amount diff). Dataset analysis
proved this would ONLY catch the 4 partial_refund records (amount diff 29-38%),
which are explicitly designated for LLM residual. Zero legitimate targets exist
for this rule -- every ref-matchable record with a reasonable amount difference
is already caught by Rules 1-3. Evidence: `tests/analyze_rules.py` output.

### Key decisions & why

- **Partial refunds -> LLM, never rules.** They require netting reasoning
  (original txn + refund -> net), not a percentage tolerance. All 4 correctly
  go to residual.
- **Rule 4 confidence = 0.95, not 1.0.** It matches without ref verification.
  Still very reliable (bidirectionally unique = only one possible pairing), but
  less certain than a ref-confirmed match.
- **Bidirectional uniqueness in Rule 4.** Proven necessary: EXT_035 and EXT_002
  are duplicate_amount records with ambiguous amount+date (2 internals match).
  Without checking internal-side uniqueness, Rule 4 would have produced wrong
  matches. Both correctly went to residual instead.

### Results (cumulative, full batch)

| Metric | Value |
|--------|-------|
| Internal records | 65 |
| External records | 55 |
| Rule-matched (correct) | 45 / 52 = **86.5%** |
| False positives | **0** |
| Residual for LLM | 7 matched + 13 unmatched-int + 3 unmatched-ext |
| Throughput | ~199,000 records/sec |

### Residual breakdown (what Part 3 LLM will receive)

| Noise type | Count | Why rules can't match |
|------------|-------|----------------------|
| partial_refund | 4 | Amount diff 29-38%, needs netting reasoning |
| duplicate_amount | 2 | Garbled ref + ambiguous amount+date (multiple candidates) |
| date_drift | 1 | Ref is None + date shifted 3 days -> no ref for Rules 1-3, date mismatch for Rule 4 |

Plus 13 unmatched internals (no ground-truth pair) and 3 unmatched externals
(no ground-truth pair) -- these should become exceptions in Part 5.

### What Rule 4 actually caught (9 matches, all correct)

- 6 missing_ref records: external has garbled/truncated/empty ref (e.g. `pay_x`,
  `pay_Ue`, `None`), so Rules 1-3 miss. Amount+date is unique in both directions.
- 3 description_only records: external ref is None, same cascade.
- Original estimate was 5 (only counted ref=None externals), actual is 9 because
  garbled refs also fall through Rules 1-3.

### Known issues or shortcuts

- Rule cascade is O(n*m) in worst case but fast for n=65, m=55 (~0.6ms). For
  production-scale data (100K+ records), the ref index would need hash-based
  lookup (already done) and the amount_date index might need range queries.
- The 3% fee tolerance is tuned to this dataset's 1.77-2.95% range. A real
  system would parameterise this per payment gateway.
- Rule 4's `date` comparison is exact (not windowed). The 1 residual date_drift
  record has both no ref AND a 3-day date shift, so it can't match on amount+date
  alone. This is the correct behaviour -- date drift without a ref is genuinely
  ambiguous and should go to the LLM.

### Open questions for next Part

- The 7 residual matched records need the LLM to reason over: merchant name,
  description text, amount netting for partial refunds. What context should be
  included in the LLM prompt -- just the internal+external pair, or also the
  list of candidate externals for disambiguation?
- For the 2 duplicate_amount residuals (ambiguous amount+date, 2 candidates
  each), should the LLM see both candidates and pick one, or see them
  sequentially?

---

## Part 4: GL Mapping / Classification -- completed 2026-08-28

### What was built

- `src/gl_classifier.py` -- two-phase GL classification with LLM fallback:
  - **Phase A:** Primary category cascade (refund_type -> partial_refund_llm ->
    clean_settlement)
  - **Phase B:** Orthogonal fee/tax sub-entry attachment whenever
    `match.rule_name == "ref_fee_tolerance"`, regardless of Phase A category
- `tests/test_gl_classifier.py` -- 33 tests, all passing. Covers:
  - Full-batch coverage (52 ClassifiedEntry objects, not flattened)
  - Fee/tax rounding-leakage (exact Decimal equality, zero tolerance)
  - TXN_020 explicit test: REFUND + fee sub-entries (the decoupling proof)
  - Partial refund consistency (all 4 records annotated)
  - Rule distribution verification
  - Individual rule unit tests + Phase B attachment unit tests
  - Mock-based LLM classification path (4 tests)
  - Batch prompt construction + fault-tolerant parsing
  - 1000-iteration fuzz test for fee/tax split rounding

### Key decisions & why

- **Phase A/B decoupled architecture.** Fee/tax sub-entry attachment is
  orthogonal to primary category selection. This was the third iteration:
  1. First version had `refund_type` before `fee_split` -- TXN_020
     (txn_type=refund, ref_fee_tolerance) lost its fee split.
  2. Second version reordered `fee_split` before `refund_type` -- TXN_020
     got its fee split but was misclassified as SETTLEMENT instead of REFUND.
  3. Final version decouples: Phase A picks REFUND (correct domain category),
     Phase B attaches fee sub-entries (correct accounting detail). Both apply.

- **Fee/tax split uses remainder method.** `gateway_fee = fee * 100/118`
  quantized with `ROUND_HALF_UP`, then `tax_adjustment = fee - gateway_fee`.
  The tax is the remainder, not `fee * 18/118`, guaranteeing zero rounding
  leakage by construction. Verified with 1000-iteration fuzz test.

- **Fee gating on `match.rule_name == "ref_fee_tolerance"` only.** Amount
  arithmetic computes sub-entry values, never decides whether a fee split
  applies. This prevents false positives from coincidental amount differences.

- **Partial refund annotation on all 4 records.** Rule 1 (refund_type) checks
  for "partial refund" in LLM reasoning and adds the annotation. Rule 2
  (partial_refund_llm) catches TXN_005 (payment) and TXN_031 (settlement)
  which have non-refund txn_type but are partial refunds per LLM reasoning.

- **Systematic overlap analysis.** After discovering TXN_020's rule collision
  twice, ran a one-pass analysis of all Phase A rule pairs. Result: 4 overlaps
  (2 refund_type/partial_refund_llm, 2 partial_refund_llm/clean_settlement),
  all resolved by cascade order with zero information loss. Phase B is fully
  orthogonal -- no ordering conflict possible.

- **LLM classification path implemented but not needed.** All 52 records
  classified by deterministic rules. The LLM path is a safety net for future
  data with unknown txn_types. Tested via mocks (4 tests: single, batch,
  parse error, missing API key).

### Results so far (cumulative, full batch)

- Match rate: **52 / 52 = 100.0%** (unchanged from Part 3)
- Matched via rule / matched via LLM / exceptions: **45 / 7 / 13**
- Classification: **52 / 52 = 100.0%** (all classified, 0 UNCLASSIFIED)
- Throughput: matching ~199,000 rec/sec (rule) + ~50s (LLM); classification <1ms
- LLM API calls for classification: **0** (all deterministic)
- Test suite: **33/33 passed**

**GL category distribution (Phase A + Phase B):**

| Phase A Category | Count | Rule | Phase B (fee sub-entries) |
|------------------|-------|------|--------------------------|
| REFUND | 10 | refund_type | 1 (TXN_020) |
| REFUND (partial) | 2 | partial_refund_llm | 0 |
| SETTLEMENT | 40 | clean_settlement | 7 |
| **Total** | **52** | | **8** |

**Fee/tax split verification (all 8 records):**
- GATEWAY_FEE + TAX_ADJUSTMENT == total fee: **YES** (exact Decimal equality)
- Rounding leakage: **0** across all 8 records and 1000 fuzz iterations
- TXN_020 (refund with fee): REFUND category + fee sub-entries: **CORRECT**

### Known issues or shortcuts taken

- **Reasoning-text keyword match is a heuristic.** Rule 2 (`partial_refund_llm`)
  identifies partial refunds by checking for "partial refund" in the LLM's
  reasoning text. Verified correct on TXN_005 and TXN_031, but would need
  hardening (negation handling, e.g., "this is not a partial refund") to
  generalise. Documented in README Known Limitations.

- **TXN_020 was a real domain-modelling bug found through 3 iterations.**
  A refund transaction with a fee deduction needs BOTH its refund category
  AND its fee accounting detail. The first two attempts treated these as
  competing concerns; the final Phase A/B split gets it right.

- **No multi-currency support.** All amounts are INR. Fee/tax split assumes
  a single GST rate of 18%. Real systems would parameterise both.

### Open questions (resolved in Part 5)

- **Exception reasons:** reference matching outcome, not GL classification.
  LLM rejection reasoning pulled from Part 3 stored output, not recomputed.
- **Audit trail format:** JSON lines (machine-readable). Rendered markdown
  report deferred to Part 6.

---

## Part 5: Exception Reporting + Audit Trail -- completed 2026-08-28

### What was built

- `src/exceptions.py` -- exception collection with 3 categories only:
  `unmatched_internal`, `unmatched_external`, `parse_error`. LLM rejection
  reasoning nested in reason text, not a peer category.
- `src/audit.py` -- JSON-lines audit trail covering every pipeline decision
  (match, classification, exception). One entry per decision point.
- `tests/test_part5.py` -- 21 tests, all passing.

### Key decisions & why

- **Only 3 exception categories.** `llm_rejected` and `no_candidates` are
  reason-text explanations inside `unmatched_internal`, not 4th/5th peer
  categories. This avoids double-listing the same record.

- **LLM rejection reasoning pulled from Part 3.** Not recomputed in Part 5.
  The `llm_none_results` list carries the LLM's specific reasoning for each
  of the 11 evaluated records. The 2 no-candidate records (TXN_055, TXN_064)
  get a different reason template citing "all candidates already claimed".

- **Unmatched external reasons cite specific record data.** Each of the 3
  unmatched externals (EXT_053, EXT_054, EXT_055) gets a unique reason citing
  its own amount, date, reference ID, and description. No identical templates.

- **Exactly 120 audit entries.** 65 (one per internal: 52 match + 13 exception)
  + 3 (unmatched externals) + 52 (GL classification) + 0 (parse errors) = 120.

### Results (cumulative, full batch)

- Match rate: **52 / 52 = 100.0%**
- Classification: **52 / 52 = 100.0%**
- Full accounting -- internal: **52 matched + 13 unmatched = 65**
- Full accounting -- external: **52 matched + 3 unmatched = 55**
- Exception records: **16 total** (13 internal + 3 external + 0 parse errors)
- Audit trail: **120 entries** (exactly)
- Test suite: **54/54 passed** (33 Part 4 + 21 Part 5)

**Exception breakdown (13 unmatched internals):**

| Sub-type | Count | Records |
|----------|-------|---------|
| LLM evaluated, rejected all | 11 | TXN_007/009/010/017/019/025/036/039/043/044/047 |
| No candidates (all claimed) | 2 | TXN_055, TXN_064 |

**Unmatched externals (3):**

| Record | Amount | Date | Description |
|--------|--------|------|-------------|
| EXT_053 | 423.93 | 2026-08-14 | Bank charges - Account maintenance fee Q3 2026 |
| EXT_054 | 285.21 | 2026-08-17 | Interest credit - Current account |
| EXT_055 | 230.59 | 2026-08-16 | GST TDS deduction - Government of India |

### Known issues or shortcuts taken

- **LLM NONE reasoning is reconstructed from canonical demo output.** In
  production, the LLM matcher would persist these results; here they're
  hardcoded in the test fixture from the verified Part 3 output.

- **Audit trail timestamps are all the same instant.** The pipeline runs
  synchronously, so all entries get `datetime.now()`. A real system would
  use per-decision timestamps from the actual processing pipeline.

---

## Part 6: Report Generation + Full Pipeline -- completed 2026-08-28

### What was built

- `src/reporting.py` -- generates `reports/reconciliation_report.md` with
  executive summary, full match table, GL classification, exceptions,
  audit trail summary, architecture diagram, and "What Broke" section.
  **Zero hardcoded totals** -- every number computed from passed-in data.
- `run_full_pipeline.py` -- end-to-end script chaining Parts 0-6.
  Canonical LLM results validated via **hard-fail** ID-based lookup against
  fresh residual pool. Optional `--live` flag for real Gemini API call.
- Output files: `reconciliation_report.md`, `exceptions.json`, `audit_trail.jsonl`

### Key decisions & why

- **Canonical validation is a hard fail.** If the data generator changes and
  produces a different residual pool, `run_full_pipeline.py` crashes with a
  clear error message naming the mismatched IDs. It does NOT silently skip
  or fall back to stale results.

- **`--live` flag is opt-in.** Default path uses canonical results to avoid
  API quota risk during demos. `--live` runs the real Gemini API for
  verification.

- **All numbers from aggregation.** `reporting.py` uses `len()`, `sum()`,
  and dict aggregation on passed-in data structures. A search for hardcoded
  integers finds zero instances.

### Results (final)

- Pipeline runs end-to-end in <1s (canonical mode)
- Report: `reports/reconciliation_report.md` (201 lines)
- Exceptions: `reports/exceptions.json` (16 records)
- Audit trail: `reports/audit_trail.jsonl` (120 entries)
- Test suite: **54/54 passed** (33 Part 4 + 21 Part 5)
