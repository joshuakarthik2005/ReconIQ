# AI Finance Controller

> Razorpay Buildathon — Track 04: AI Finance Ops

Automated reconciliation engine that matches internal payment/settlement records
against external bank statements, classifies entries into GL categories, and
produces honest full-batch reports with audit trails.

## Status

**Complete** -- see `PROGRESS_REPORT.md` for the full build log.

## Quick Start

```bash
# Generate synthetic test data (seed=42, deterministic)
python src/data_generator.py

# Run full pipeline (Parts 0-6)
python run_full_pipeline.py

# Run with live Gemini API (optional, requires GEMINI_API_KEY)
python run_full_pipeline.py --live

# Run tests (live API tests auto-skipped without GEMINI_API_KEY)
python -m pytest tests/ -v
```

## Project Structure

```
data/                          # Generated test data (gitignored by convention)
  internal_transactions.csv    # 65 internal payment/settlement records
  external_format_a.csv        # Format A: clean bank CSV (25 records)
  external_format_b.csv        # Format B: messy semi-structured CSV (18 records)
  external_format_c.json       # Format C: nested JSON bank feed (12 records)
  ground_truth.json            # Validation only -- never read by pipeline

src/
  schemas.py                   # Canonical data types
  data_generator.py            # Synthetic data generation
  ingestion.py                 # Part 1 -- multi-format parsers
  deterministic_matcher.py     # Part 2 -- rule-based matching
  llm_matcher.py               # Part 3 -- Gemini residual matching
  gl_classifier.py             # Part 4 -- GL classification (Phase A/B)
  exceptions.py                # Part 5 -- exception reporting
  audit.py                     # Part 5 -- audit trail
  reporting.py                 # Part 6 -- report generation

reports/                       # Generated reports
  reconciliation_report.md     # Demo artifact -- full batch report
  exceptions.json              # Machine-readable exception records
  audit_trail.jsonl            # Machine-readable audit trail

run_full_pipeline.py           # End-to-end pipeline (Parts 0-6)
DATA_NOTES.md                  # Dataset design documentation
PROGRESS_REPORT.md             # Build log with honest numbers
```

## Architecture

Two-tier matching:
1. **Deterministic rules** (fast path) — exact ref ID, amount tolerance, date window.
2. **LLM reasoning** (residual path) — Gemini 2.5 Flash for ambiguous cases only.

Every record ends up in one of three buckets:
- ✅ Matched (rule) — with audit entry noting which rule fired
- ✅ Matched (LLM) — with confidence score + one-line justification
- ❌ Exception — with a specific, human-readable reason (never "no match found")

## Known Limitations

- **Confidence split on NONE decisions:** Of the 13 correct exceptions, TXN_036/039/043
  received confidence=1.00 while the other 10 received 0.00. The distinguishing factor:
  those three appeared in Batch 4 alongside a true positive (TXN_031→EXT_003) and had
  shortlist candidates — the LLM actively evaluated and rejected them with high confidence.
  The 0.00 records either had no candidates (shortlist empty / all claimed) or were in
  batches where NONE was the default response with no candidates to evaluate.
- **Claim-ordering fragility:** Batch processing order is deterministic but arbitrary.
  If an earlier batch produces a false positive that claims an external record, a later
  batch's true match for that external is permanently starved. This didn't cause a
  collision in the current run (0 FP), but the mechanism could in principle produce
  order-dependent errors. A production system would need a global optimisation pass
  (e.g., Hungarian algorithm) rather than greedy sequential claiming.
- **GL classification reasoning-text keyword match:** The classifier identifies partial
  refunds by checking for "partial refund" in the LLM's match reasoning text. Verified
  correct on this dataset's 2 applicable records (TXN_005, TXN_031), but would need
  hardening (negation handling, multi-language support) to generalise beyond this batch.
