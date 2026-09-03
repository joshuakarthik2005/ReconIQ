# AI Finance Controller

[![CI](https://github.com/joshuakarthik2005/ReconIQ/actions/workflows/ci.yml/badge.svg)](https://github.com/joshuakarthik2005/ReconIQ/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> Razorpay Buildathon — Track 04: AI Finance Ops

Automated reconciliation engine that matches internal payment/settlement records
against external bank statements, classifies entries into GL categories, and
produces honest full-batch reports with audit trails.

## Status

**Complete** -- see `PROGRESS_REPORT.md` for the full build log.

## Quick Start

```bash
# Runtime dependencies
pip install -r requirements.txt

# Generate synthetic test data (seed=42, deterministic)
python src/data_generator.py

# Run full pipeline (Parts 0-6)
python run_full_pipeline.py

# Run with live Gemini API (optional, requires GEMINI_API_KEY)
python run_full_pipeline.py --live

# Ask questions about the results (Settlement Q&A layer)
python qa_cli.py
```

## Development

```bash
# Dev/test/lint tooling, on top of requirements.txt
pip install -r requirements.txt -r requirements-dev.txt

# Run tests (live API tests auto-skip without GEMINI_API_KEY)
pytest tests/ -v

# Lint + type check (same checks CI runs on every push/PR)
ruff check src/ run_full_pipeline.py qa_cli.py tests/
mypy
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
  schemas.py                    # Canonical data types
  data_generator.py             # Synthetic data generation
  ingestion.py                  # Part 1 -- multi-format parsers
  deterministic_matcher.py      # Part 2 -- rule-based matching + Hungarian
                                #   algorithm (scipy.optimize.linear_sum_assignment)
                                #   optimal assignment within each rule tier
  llm_matcher.py                # Part 3 -- Gemini residual matching
  gl_classifier.py              # Part 4 -- GL classification (Phase A/B)
  exceptions.py                 # Part 5 -- exception reporting
  audit.py                      # Part 5 -- audit trail
  reporting.py                  # Part 6 -- report generation
  dashboard.py                  # Part 7 -- self-contained HTML dashboard
  qa.py                         # Settlement Q&A layer -- deterministic
                                #   intent router over the pipeline's own
                                #   output, with an LLM fallback only for
                                #   unrecognized questions

reports/                       # Generated reports
  reconciliation_report.md     # Demo artifact -- full batch report
  dashboard.html               # Interactive KPI dashboard
  exceptions.json              # Machine-readable exception records
  audit_trail.jsonl            # Machine-readable audit trail

run_full_pipeline.py           # End-to-end pipeline (Parts 0-6)
qa_cli.py                      # Interactive CLI for the Settlement Q&A layer
DATA_NOTES.md                  # Dataset design documentation
PROGRESS_REPORT.md             # Build log with honest numbers

.github/workflows/ci.yml       # Lint + type check + test + full pipeline,
                                #   on every push/PR to main
requirements.txt               # Runtime dependencies
requirements-dev.txt           # pytest, ruff, mypy, hypothesis
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
- **Claim-ordering (deterministic tiers):** Within each Tier-1 rule, candidates now go
  through `_optimal_assign_tier` (Hungarian algorithm via
  `scipy.optimize.linear_sum_assignment`) instead of greedy sequential claiming, so an
  early low-confidence pairing can no longer starve a later, better one inside the same
  rule. Cross-tier ordering (a Tier-1 rule claiming a record before a later tier gets to
  see it) is still first-match-wins by design — rules are meant to be tried in
  precedence order.
- **GL classification reasoning-text keyword match:** The classifier identifies partial
  refunds by checking for "partial refund" in the LLM's match reasoning text, with a
  negation-aware regex (`_NEGATION_RE`) so phrasing like "not a partial refund" or
  "wasn't a partial refund" is correctly rejected. Verified on this dataset's 2
  applicable records (TXN_005, TXN_031) plus a dedicated negation test suite. Still
  single-language (English) only.
- **`--fee-tolerance-pct` / `--date-window-days`:** exposed as CLI flags on
  `run_full_pipeline.py` instead of being hardcoded. Note: the canonical (non-`--live`)
  LLM results shipped in this repo are pinned to the *default* residual pool — passing
  non-default values without `--live` will fail the canonical-results validation check
  (loudly, not silently) rather than produce a wrong report.
