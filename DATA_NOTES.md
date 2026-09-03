# Data Notes — Synthetic Reconciliation Dataset

## Overview

This dataset simulates one month of payment reconciliation data for a Razorpay-like
payment gateway. It contains **65 internal transaction records** and **55 external
bank-statement records** across three deliberately different formats.

**Ground truth**: 52 matched pairs, 13 unmatched internal, 3 unmatched external.

---

## Internal Transactions (`internal_transactions.csv`)

- **65 records** in clean CSV format.
- Transaction types: 33 payments, 19 settlements, 13 refunds.
- Payment methods: UPI (12), credit card (8), debit card (12), netbanking (20), wallet (13).
- Amounts range from ~INR 49 to ~INR 75,000.
- Dates span 2026-08-01 to 2026-08-30.
- Reference IDs use Razorpay-style prefixes: `pay_`, `setl_`, `rfnd_`.

---

## External Formats

### Format A — Clean Bank Statement CSV (`external_format_a.csv`)

- **25 records** (23 matched + 2 unmatched-external).
- Standard columns: Transaction Date, Value Date, Reference No, Credit, Debit,
  Description, Balance.
- Dates in `YYYY-MM-DD`. Amounts as plain decimals. Running balance included.
- This is the "easy" format — parseable with a single `csv.DictReader` call.

### Format B — Messy Semi-Structured Export (`external_format_b.csv`)

- **18 records** (all matched).
- Non-standard columns: `Sl.No.`, `Txn Ref.`, `Amt (INR)`, `Dt.`, `Particulars`, `Cr/Dr`.
- **Deliberate messiness:**
  - Amounts include currency symbols and commas: `₹1,500.00`, `INR 1500.00`,
    `₹ 1,500.00`, `1,500.00/-`.
  - Dates in mixed formats: `DD-MM-YYYY`, `DD/MM/YYYY`, `DD MMM YYYY`, `DD-MM-YY`.
  - Reference IDs sometimes prefixed with `NEFT/` or `IMPS/`, or padded with whitespace.
  - Descriptions randomly truncated (to 35 chars) or uppercased.

### Format C — JSON Bank API Feed (`external_format_c.json`)

- **12 records** (11 matched + 1 unmatched-external).
- Nested JSON structure with `amount.value`, `amount.currency`.
- Reference ID appears in **different locations** depending on the record:
  - Top-level `reference` field (with `counterparty` block) — ~50%.
  - Nested `metadata.payment_ref` — ~30%.
  - Embedded in `narrative` string as `| Ref: <id>` — ~20%.
- `narrative` field randomly omitted for ~12% of records.

---

## Noise Types (applied to the 52 matched pairs)

| Noise Type         | Count | Description                                                                 |
|--------------------|-------|-----------------------------------------------------------------------------|
| `exact`            | 20    | Perfect match: ref ID, amount, and date all identical.                      |
| `fee_rounding`     | 8     | External amount reduced by gateway fee (1.5–2.5%) + 18% GST on the fee.    |
| `missing_ref`      | 6     | External ref ID is blank (~35%), truncated (~30%), or completely wrong (~35%). |
| `date_drift`       | 5     | External date shifted forward by 1–3 calendar days.                         |
| `partial_refund`   | 4     | External shows net amount after 25–55% partial refund.                      |
| `duplicate_amount` | 6     | 3 pairs of internal records share the same amount + date + merchant. ~1 in 3 also has a garbled ref. |
| `description_only` | 3     | Ref ID removed; merchant name + payment method embedded in description.     |

---

## Unmatched Records

### Unmatched Internal (13 records)

These are internal transactions with **no corresponding external record**. They
simulate scenarios like:
- Pending settlements not yet reflected in the bank statement.
- Very recent transactions processed after the statement cutoff.
- Transactions routed through a different settlement account.

### Unmatched External (3 records)

Bank-side entries with no internal counterpart:
1. **Bank service charge** — account maintenance fee (no ref ID).
2. **Interest credit** — current account interest (no ref ID).
3. **GST TDS deduction** — government tax deduction (has a `GST*` reference).

---

## Reproducibility

- Random seed: **42** (set in `src/data_generator.py`).
- All data can be regenerated identically by running `python src/data_generator.py`.
- Ground truth in `data/ground_truth.json` — this file must **never** be imported
  by any matching or classification module.

---

## Design Rationale

The noise distribution is deliberately weighted toward "exact" matches (20 of 52)
because real-world reconciliation datasets are typically 60-80% clean. The remaining
32 noisy records cover the edge cases that make reconciliation hard:

- **Fee deductions** are the single most common discrepancy in payment-gateway
  reconciliation (8 records).
- **Missing/garbled reference IDs** force the matcher to fall back on amount + date
  heuristics (6 records).
- **Duplicate amounts** are a classic source of false positives — the matcher must
  use secondary signals to disambiguate (6 records in 3 pairs).
- **Date drift** tests the date-window tolerance setting (5 records).
- **Partial refunds** create the largest amount discrepancies and are the hardest
  for rule-based matching (4 records).
- **Description-only matches** test NLP / string-matching capabilities (3 records).
