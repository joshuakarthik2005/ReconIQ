> **CANONICAL DEMO ARTIFACT** — Saved 2026-08-28. Show this file during the live demo
> instead of depending on a fresh Gemini API call succeeding in the room (daily quota risk).
> Generated from `tests/test_remaining_batches.py` against the full 20-record LLM residual.

# Part 3 — LLM Matching: Full 20-Record Live Results

## Final Score

| Metric | Value |
|--------|-------|
| **Part 2 (rules)** | 45 / 52 correct matches |
| **Part 3 (LLM)** | **7 TP, 0 FP, 0 missed, 13 correct exceptions** |
| **Combined** | **52 / 52 = 100.0%** |
| **False positives** | **0** |
| **LLM API calls** | 5 total (1 group + 4 batch) |
| **All 13 GT unmatched as NONE** | **YES** |

---

## Complete Results Table

| Record | Decision | Ground Truth | Noise Type | Conf | Verdict |
|--------|----------|-------------|------------|------|---------|
| TXN_005 | → EXT_020 | EXT_020 | partial_refund | 0.95 | ✅ TP |
| TXN_007 | → NONE | no GT match | unmatched | 0.00 | ✅ OK exc |
| TXN_009 | → NONE | no GT match | unmatched | 0.00 | ✅ OK exc |
| TXN_010 | → NONE | no GT match | unmatched | 0.00 | ✅ OK exc |
| TXN_017 | → NONE | no GT match | unmatched | 0.00 | ✅ OK exc |
| TXN_019 | → NONE | no GT match | unmatched | 0.00 | ✅ OK exc |
| TXN_021 | → EXT_002 | EXT_002 | duplicate_amount | 0.95 | ✅ TP |
| TXN_025 | → NONE | no GT match | unmatched | 0.00 | ✅ OK exc |
| TXN_026 | → EXT_043 | EXT_043 | partial_refund | 0.95 | ✅ TP |
| TXN_031 | → EXT_003 | EXT_003 | partial_refund | 0.95 | ✅ TP |
| TXN_036 | → NONE | no GT match | unmatched | 1.00 | ✅ OK exc |
| TXN_039 | → NONE | no GT match | unmatched | 1.00 | ✅ OK exc |
| TXN_043 | → NONE | no GT match | unmatched | 1.00 | ✅ OK exc |
| TXN_044 | → NONE | no GT match | unmatched | 0.00 | ✅ OK exc |
| TXN_045 | → EXT_035 | EXT_035 | duplicate_amount | 0.98 | ✅ TP |
| TXN_047 | → NONE | no GT match | unmatched | 0.00 | ✅ OK exc |
| TXN_051 | → EXT_040 | EXT_040 | date_drift | 0.95 | ✅ TP |
| TXN_054 | → EXT_041 | EXT_041 | partial_refund | 0.92 | ✅ TP |
| TXN_055 | → NONE | no GT match | unmatched | 0.00 | ✅ OK exc |
| TXN_064 | → NONE | no GT match | unmatched | 0.00 | ✅ OK exc |

---

## Per-Batch Breakdown

### Batch 1 (scoped-candidate, 4 records)
| Record | Decision | Conf | Verdict |
|--------|----------|------|---------|
| TXN_005 | → EXT_020 | 0.95 | TP |
| TXN_007 | → NONE | 0.00 | OK exc |
| TXN_009 | → NONE | 0.00 | OK exc |
| TXN_010 | → NONE | 0.00 | OK exc |

**1 TP, 0 FP, 3 correct exceptions** · 1998 in / 401 out · 9.0s

---

### Group Call: TXN_021 + TXN_045 (joint group-assignment)

> [!IMPORTANT]
> This was the highest-risk case — two `duplicate_amount` records competing for the same candidate pool. Used `_group_prompt` (shared pool, single-assignment-per-external constraint) with code-level `claimed_in_group` double-claim check.

**Shared candidate pool (6):** EXT_002, EXT_003, EXT_053, EXT_055, EXT_041, EXT_035

**Raw LLM response:**
```json
{
  "assignments": [
    {
      "internal_label": "A",
      "match_index": 1,
      "confidence": 0.95,
      "reasoning": "Exact match on amount (5873.85 INR) and transaction date (2026-08-12)."
    },
    {
      "internal_label": "B",
      "match_index": 6,
      "confidence": 0.98,
      "reasoning": "Exact match on amount (1576.26 INR), date (2026-08-26), and clear merchant match in external description ('RAZORPAY*Jio Platforms Ltd')."
    }
  ]
}
```

| Record | Decision | Conf | Verdict |
|--------|----------|------|---------|
| TXN_021 | → EXT_002 | 0.95 | TP |
| TXN_045 | → EXT_035 | 0.98 | TP |

**2 TP, 0 FP** · 843 in / 175 out · 6.5s · No double-claims attempted.

---

### Batch 3 (scoped-candidate, 4 records)
| Record | Decision | Conf | Verdict |
|--------|----------|------|---------|
| TXN_017 | → NONE | 0.00 | OK exc |
| TXN_019 | → NONE | 0.00 | OK exc |
| TXN_025 | → NONE | 0.00 | OK exc |
| TXN_026 | → EXT_043 | 0.95 | TP |

**1 TP, 0 FP, 3 correct exceptions** · 1496 in / 358 out · 15.6s

---

### Batch 4 (scoped-candidate, 4 records)
| Record | Decision | Conf | Verdict |
|--------|----------|------|---------|
| TXN_031 | → EXT_003 | 0.95 | TP |
| TXN_036 | → NONE | 1.00 | OK exc |
| TXN_039 | → NONE | 1.00 | OK exc |
| TXN_043 | → NONE | 1.00 | OK exc |

**1 TP, 0 FP, 3 correct exceptions** · 1494 in / 313 out · 7.4s

---

### Batch 5 (scoped-candidate, 4 records)
| Record | Decision | Conf | Verdict |
|--------|----------|------|---------|
| TXN_044 | → NONE | 0.00 | OK exc |
| TXN_047 | → NONE | 0.00 | OK exc |
| TXN_051 | → EXT_040 | 0.95 | TP |
| TXN_054 | → EXT_041 | 0.92 | TP |

**2 TP, 0 FP, 2 correct exceptions** · 1277 in / 344 out · 11.7s

---

### Batch 6 (no API call — candidates already claimed)
TXN_055 and TXN_064: all shortlist candidates were already claimed by earlier batches → recorded as exceptions without an LLM call.

---

## LLM Reasoning Quality by Noise Type

| Noise Type | Records | Result | Reasoning Pattern |
|------------|---------|--------|-------------------|
| **partial_refund** | TXN_005, TXN_026, TXN_031, TXN_054 | 4/4 TP | Cites ref match + date + merchant, explains lower bank amount as partial refund netting |
| **duplicate_amount** | TXN_021, TXN_045 | 2/2 TP | Amount + date match with merchant disambiguation from description |
| **date_drift** | TXN_051 | 1/1 TP | Exact amount match, recognizes 3-day settlement window |
| **unmatched** | 13 records | 13/13 correct exceptions | Cites specific mismatches (ref, amount, merchant, date) — not generic rejections |

---

## Bug Found & Fixed During Execution

> [!WARNING]
> First run showed only 18/20 records — TXN_055 and TXN_064 were missing.

**Root cause:** In [test_remaining_batches.py](file:///g:/razorpay/tests/test_remaining_batches.py), when batch candidate-filtering removed all candidates (because all had been claimed by earlier batches), the records were silently dropped instead of being recorded as exceptions.

**Fix applied to both:**
- [test_remaining_batches.py](file:///g:/razorpay/tests/test_remaining_batches.py) — records with all-claimed candidates now explicitly added to `all_results` as exceptions
- [llm_matcher.py](file:///g:/razorpay/src/llm_matcher.py) — production code hardened to skip empty batches after filtering (exception collection at L771-776 already catches these via `matched_int_ids` subtraction)

---

## Token Efficiency

| Call | In Tokens | Out Tokens | Latency |
|------|-----------|------------|---------|
| Batch 1 | 1,998 | 401 | 9.0s |
| Group (TXN_021+045) | 843 | 175 | 6.5s |
| Batch 3 | 1,496 | 358 | 15.6s |
| Batch 4 | 1,494 | 313 | 7.4s |
| Batch 5 | 1,277 | 344 | 11.7s |
| **Total** | **7,108** | **1,591** | **~50s** |
