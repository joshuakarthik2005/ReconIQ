#!/usr/bin/env python3
"""
Synthetic Data Generator for AI Finance Controller
===================================================

Generates a reproducible reconciliation test dataset with deliberate noise:

  - 65 internal payment/settlement/refund records
  - 55 external bank-statement records across 3 formats (A, B, C)
  - Ground-truth match file (never read by matching logic)

Noise types (applied to matched pairs):
  exact           – Ref ID, amount, and date all match perfectly
  fee_rounding    – Amount reduced by gateway fee (1.5–2.5 %) + 18 % GST
  missing_ref     – External ref ID is blank, truncated, or garbled
  date_drift      – Settlement date shifted forward 1–3 days
  partial_refund  – External shows net amount after partial refund
  duplicate_amount– Two internal records share the same amount + date
  description_only– Ref ID removed; merchant name embedded in description

Usage
-----
    python src/data_generator.py
"""

import csv
import json
import random
import string
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

# ─── Configuration ───────────────────────────────────────────

SEED = 42
BASE_DATE = datetime(2026, 8, 1)
DATE_RANGE_DAYS = 30
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# GST rate on gateway fees — tuned to this dataset's Razorpay settlements.
# Matches GST_RATE_PCT in gl_classifier.py (as a float here for generation).
_GST_RATE = 0.18

NUM_INTERNAL = 65
NUM_MATCHED = 52          # internal records that get an external counterpart
NUM_UNMATCHED_EXT = 3     # bank-side entries with no internal counterpart

NOISE_COUNTS = {
    "exact": 20,
    "fee_rounding": 8,
    "missing_ref": 6,
    "date_drift": 5,
    "partial_refund": 4,
    "duplicate_amount": 6,    # 3 pairs of 2
    "description_only": 3,
}
assert sum(NOISE_COUNTS.values()) == NUM_MATCHED, "Noise slots must equal NUM_MATCHED"

FORMAT_SIZES = {"A": 25, "B": 18, "C": 12}
assert sum(FORMAT_SIZES.values()) == NUM_MATCHED + NUM_UNMATCHED_EXT, \
    "Format sizes must equal total external records"

# ─── Reference Data ──────────────────────────────────────────

MERCHANTS = [
    ("Flipkart Online Services", "E-commerce"),
    ("Amazon Seller Services", "E-commerce"),
    ("Swiggy Private Ltd", "Food Delivery"),
    ("Zomato Limited", "Food Delivery"),
    ("BigBasket (Innovative Retail)", "Grocery"),
    ("Myntra Designs Pvt Ltd", "Fashion"),
    ("BookMyShow Internet Pvt Ltd", "Entertainment"),
    ("MakeMyTrip India Pvt Ltd", "Travel"),
    ("Urban Company Technologies", "Home Services"),
    ("Nykaa E-Retail Pvt Ltd", "Beauty"),
    ("CRED Operations Pvt Ltd", "Fintech"),
    ("PharmEasy Healthcare", "Healthcare"),
    ("Lenskart Solutions Pvt Ltd", "Eyewear"),
    ("Dunzo Digital Pvt Ltd", "Delivery"),
    ("Rapido Bike Taxi", "Transport"),
    ("Jio Platforms Ltd", "Telecom"),
    ("Paytm E-commerce Pvt Ltd", "Fintech"),
    ("PolicyBazaar Insurance", "Insurance"),
    ("Zerodha Broking Ltd", "Finance"),
    ("Cure.fit Healthcare", "Health & Fitness"),
]

PAYMENT_METHODS = ["UPI", "credit_card", "debit_card", "netbanking", "wallet"]


# ─── Helpers ─────────────────────────────────────────────────

def _ref_id(prefix: str = "pay") -> str:
    """Razorpay-style reference: pay_<14 chars>."""
    chars = string.ascii_letters + string.digits
    return f"{prefix}_{''.join(random.choices(chars, k=14))}"


def _rand_date() -> datetime:
    return BASE_DATE + timedelta(days=random.randint(0, DATE_RANGE_DAYS - 1))


def _rand_amount(txn_type: str = "payment") -> float:
    """Realistic INR amounts with a realistic distribution."""
    if txn_type == "refund":
        return round(random.uniform(100, 10000) if random.random() > 0.5
                     else random.uniform(100, 2000), 2)
    r = random.random()
    if r < 0.20:
        return round(random.uniform(49, 499), 2)
    elif r < 0.55:
        return round(random.uniform(500, 5000), 2)
    elif r < 0.85:
        return round(random.uniform(5000, 25000), 2)
    else:
        return round(random.uniform(25000, 75000), 2)


def _rand_time() -> str:
    return f"{random.randint(0, 23):02d}:{random.randint(0, 59):02d}:{random.randint(0, 59):02d}"


# ═══════════════════════════════════════════════════════════════
# Step 1 — Internal Transactions
# ═══════════════════════════════════════════════════════════════

def generate_internal_transactions() -> list[dict]:
    txns = []
    for i in range(NUM_INTERNAL):
        r = random.random()
        if r < 0.55:
            txn_type, status, ref = "payment", "captured", _ref_id("pay")
        elif r < 0.80:
            txn_type, status, ref = "settlement", "settled", _ref_id("setl")
        else:
            txn_type, status, ref = "refund", "processed", _ref_id("rfnd")

        merchant, category = random.choice(MERCHANTS)
        amount = _rand_amount(txn_type)
        date = _rand_date()
        method = random.choice(PAYMENT_METHODS)

        desc_map = {
            "payment": f"Payment for order at {merchant}",
            "settlement": f"Settlement batch - {merchant}",
            "refund": f"Refund processed - {merchant}",
        }

        txns.append({
            "txn_id": f"TXN_{i + 1:03d}",
            "reference_id": ref,
            "amount": amount,
            "currency": "INR",
            "txn_type": txn_type,
            "date": date.strftime("%Y-%m-%d"),
            "merchant_name": merchant,
            "merchant_category": category,
            "payment_method": method,
            "status": status,
            "description": desc_map[txn_type],
        })
    return txns


# ═══════════════════════════════════════════════════════════════
# Step 2 — Match Plan
# ═══════════════════════════════════════════════════════════════

def create_match_plan(txns: list[dict]) -> tuple[list[dict], list[str]]:
    """
    Returns
    -------
    plan : list of {"txn": dict, "noise_type": str}
    unmatched_internal_ids : list of txn_ids with no external counterpart
    """
    slots: list[str] = []
    for noise, count in NOISE_COUNTS.items():
        slots.extend([noise] * count)
    random.shuffle(slots)

    indices = list(range(NUM_INTERNAL))
    random.shuffle(indices)

    matched_idx = indices[:NUM_MATCHED]
    unmatched_idx = indices[NUM_MATCHED:]

    plan = [{"txn": txns[idx], "noise_type": slots[i]}
            for i, idx in enumerate(matched_idx)]

    # Force duplicate_amount pairs to share amount + date + merchant
    dups = [p for p in plan if p["noise_type"] == "duplicate_amount"]
    for pair_start in range(0, len(dups), 2):
        if pair_start + 1 >= len(dups):
            break
        shared_amt = _rand_amount()
        shared_date = _rand_date().strftime("%Y-%m-%d")
        shared_merchant = random.choice(MERCHANTS)
        for offset in (0, 1):
            d = dups[pair_start + offset]["txn"]
            d["amount"] = shared_amt
            d["date"] = shared_date
            d["merchant_name"] = shared_merchant[0]
            d["merchant_category"] = shared_merchant[1]
            d["description"] = f"Payment for order at {shared_merchant[0]}"

    return plan, [txns[i]["txn_id"] for i in unmatched_idx]


# ═══════════════════════════════════════════════════════════════
# Step 3 — Generate External Records (with noise)
# ═══════════════════════════════════════════════════════════════

def apply_noise(txn: dict, noise_type: str, ext_counter: int) -> dict:
    """
    Build an external record from *txn*, applying *noise_type* distortion.
    """
    ext = {
        "ext_id": f"EXT_{ext_counter:03d}",
        "reference_id": txn["reference_id"],
        "amount": txn["amount"],
        "date": txn["date"],
        "description": f"RAZORPAY*{txn['merchant_name']}",
        "_merchant": txn["merchant_name"],
        "_noise": noise_type,
    }

    if noise_type == "exact":
        pass                          # nothing to change

    elif noise_type == "fee_rounding":
        rate = random.uniform(0.015, 0.025)
        fee = txn["amount"] * rate
        gst = fee * _GST_RATE
        ext["amount"] = round(txn["amount"] - fee - gst, 2)
        ext["description"] = f"Settlement net of charges - {txn['merchant_name']}"

    elif noise_type == "missing_ref":
        roll = random.random()
        if roll < 0.35:
            ext["reference_id"] = ""
        elif roll < 0.65:
            ext["reference_id"] = txn["reference_id"][:random.randint(4, 8)]
        else:
            ext["reference_id"] = f"REF{''.join(random.choices(string.digits, k=8))}"

    elif noise_type == "date_drift":
        orig = datetime.strptime(txn["date"], "%Y-%m-%d")
        ext["date"] = (orig + timedelta(days=random.randint(1, 3))).strftime("%Y-%m-%d")

    elif noise_type == "partial_refund":
        frac = random.uniform(0.25, 0.55)
        ext["amount"] = round(txn["amount"] * (1 - frac), 2)
        ext["description"] = f"Net settlement after partial refund - {txn['merchant_name']}"

    elif noise_type == "duplicate_amount":
        if random.random() < 0.33:
            ext["reference_id"] = ""   # garble ref for ≈1 in 3

    elif noise_type == "description_only":
        ext["reference_id"] = ""
        method = txn.get("payment_method", "UPI").upper()
        ext["description"] = f"RAZORPAY*{txn['merchant_name']}*{method}"

    return ext


def generate_unmatched_externals(start: int) -> list[dict]:
    """Bank-side entries with no internal counterpart."""
    return [
        {
            "ext_id": f"EXT_{start:03d}",
            "reference_id": "",
            "amount": round(random.uniform(75, 450), 2),
            "date": _rand_date().strftime("%Y-%m-%d"),
            "description": "Bank charges - Account maintenance fee Q3 2026",
            "_merchant": "BANK",
            "_noise": "unmatched_external",
        },
        {
            "ext_id": f"EXT_{start + 1:03d}",
            "reference_id": "",
            "amount": round(random.uniform(200, 1800), 2),
            "date": _rand_date().strftime("%Y-%m-%d"),
            "description": "Interest credit - Current account",
            "_merchant": "BANK",
            "_noise": "unmatched_external",
        },
        {
            "ext_id": f"EXT_{start + 2:03d}",
            "reference_id": f"GST{''.join(random.choices(string.digits, k=10))}",
            "amount": round(random.uniform(15, 250), 2),
            "date": _rand_date().strftime("%Y-%m-%d"),
            "description": "GST TDS deduction - Government of India",
            "_merchant": "GOV",
            "_noise": "unmatched_external",
        },
    ]


# ═══════════════════════════════════════════════════════════════
# Step 4 — Format Assignment
# ═══════════════════════════════════════════════════════════════

def assign_formats(externals: list[dict]) -> None:
    """Shuffle, then slice into format buckets A / B / C in-place."""
    random.shuffle(externals)
    cursor = 0
    for fmt, size in FORMAT_SIZES.items():
        for i in range(cursor, cursor + size):
            externals[i]["_format"] = fmt
        cursor += size


# ═══════════════════════════════════════════════════════════════
# Step 5 — File Writers
# ═══════════════════════════════════════════════════════════════

def write_internal_csv(txns: list[dict], path: Path) -> None:
    """Clean CSV — our internal system's export."""
    fields = [
        "txn_id", "reference_id", "amount", "currency", "txn_type",
        "date", "merchant_name", "merchant_category", "payment_method",
        "status", "description",
    ]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for t in txns:
            w.writerow({k: t[k] for k in fields})


def write_format_a(records: list[dict], path: Path) -> None:
    """
    Format A — Clean bank statement CSV.
    Columns: Txn ID | Transaction Date | Value Date | Reference No |
             Credit | Debit | Description | Balance
    """
    records_sorted = sorted(records, key=lambda r: r["date"])
    balance = 500_000.00
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow([
            "Txn ID", "Transaction Date", "Value Date", "Reference No",
            "Credit", "Debit", "Description", "Balance",
        ])
        for r in records_sorted:
            is_credit = r["amount"] > 0
            credit = f'{r["amount"]:.2f}' if is_credit else ""
            debit = f'{abs(r["amount"]):.2f}' if not is_credit else ""
            balance += r["amount"]
            w.writerow([
                r["ext_id"], r["date"], r["date"], r["reference_id"],
                credit, debit, r["description"], f"{balance:.2f}",
            ])


def write_format_b(records: list[dict], path: Path) -> None:
    """
    Format B — Messy semi-structured export.
    Quirks: currency symbols in amounts, varied date formats,
    whitespace-padded refs, NEFT/IMPS prefixes, truncated descriptions.
    """
    records_sorted = sorted(records, key=lambda r: r["date"])
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Sl.No.", "Bank Ref", "Txn Ref.", "Amt (INR)", "Dt.", "Particulars", "Cr/Dr"])

        for i, r in enumerate(records_sorted):
            # ── Reference ID messiness ──
            ref = r["reference_id"]
            if ref:
                roll = random.random()
                if roll < 0.20:
                    ref = f"NEFT/{ref}"
                elif roll < 0.35:
                    ref = f"IMPS/{ref}"
                elif roll < 0.50:
                    ref = f"  {ref}  "

            # ── Amount messiness ──
            amt = abs(r["amount"])
            style = random.choice([
                "symbol_comma", "inr_plain", "symbol_space", "plain", "trailing",
            ])
            amt_map = {
                "symbol_comma": f"₹{amt:,.2f}",
                "inr_plain": f"INR {amt:.2f}",
                "symbol_space": f"₹ {amt:,.2f}",
                "plain": f"{amt:.2f}",
                "trailing": f"{amt:,.2f}/-",
            }
            amt_str = amt_map[style]

            # ── Date messiness ──
            d = datetime.strptime(r["date"], "%Y-%m-%d")
            dfmt = random.choice(["%d-%m-%Y", "%d/%m/%Y", "%d %b %Y", "%d-%m-%y"])
            date_str = d.strftime(dfmt)

            # ── Description messiness ──
            desc = r["description"]
            if random.random() < 0.35:
                desc = desc[:35]
            if random.random() < 0.30:
                desc = desc.upper()

            cr_dr = "Cr" if r["amount"] > 0 else "Dr"
            w.writerow([i + 1, r["ext_id"], ref, amt_str, date_str, desc, cr_dr])


def write_format_c(records: list[dict], path: Path) -> None:
    """
    Format C — JSON bank API feed with nested objects.
    Some fields randomly omitted; reference ID may appear in
    different locations (top-level, nested metadata, or narrative).
    """
    payload = {
        "account_id": "ACC_9876543210",
        "account_type": "CURRENT",
        "statement_period": {
            "from": BASE_DATE.strftime("%Y-%m-%d"),
            "to": (BASE_DATE + timedelta(days=DATE_RANGE_DAYS)).strftime("%Y-%m-%d"),
        },
        "generated_at": datetime.now().isoformat(),
        "transactions": [],
    }

    for r in records:
        d = datetime.strptime(r["date"], "%Y-%m-%d")
        ts = f"{d.strftime('%Y-%m-%d')}T{_rand_time()}+05:30"

        entry: dict = {
            "id": r["ext_id"],
            "timestamp": ts,
            "amount": {"value": r["amount"], "currency": "INR"},
            "type": "CREDIT" if r["amount"] > 0 else "DEBIT",
            "narrative": r["description"],
        }

        ref = r["reference_id"]
        if ref:
            style = random.random()
            if style < 0.50:
                entry["reference"] = ref
                entry["counterparty"] = {
                    "name": "RAZORPAY SOFTWARE PVT LTD",
                    "account": "XXXXXXX1234",
                }
            elif style < 0.80:
                entry["metadata"] = {"payment_ref": ref}
            else:
                entry["narrative"] = f"{r['description']} | Ref: {ref}"

        if random.random() < 0.12:
            entry.pop("narrative", None)

        payload["transactions"].append(entry)

    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)


def write_ground_truth(matches, unmatched_int, unmatched_ext, path: Path):
    """Validation-only file — must NEVER be imported by matching code."""
    noise_dist = Counter(m["noise_type"] for m in matches)
    fmt_dist = Counter(m["external_format"] for m in matches)

    gt = {
        "_warning": (
            "This file is for validation ONLY. "
            "It must NEVER be read by any matching or classification module."
        ),
        "matches": matches,
        "unmatched_internal": unmatched_int,
        "unmatched_external": [r["ext_id"] for r in unmatched_ext],
        "metadata": {
            "total_internal": NUM_INTERNAL,
            "total_external": NUM_MATCHED + NUM_UNMATCHED_EXT,
            "total_matched_pairs": len(matches),
            "total_unmatched_internal": len(unmatched_int),
            "total_unmatched_external": len(unmatched_ext),
            "noise_distribution": dict(sorted(noise_dist.items())),
            "format_distribution": dict(sorted(fmt_dist.items())),
            "seed": SEED,
            "generated_at": datetime.now().isoformat(),
        },
    }

    with open(path, "w", encoding="utf-8") as fh:
        json.dump(gt, fh, indent=2, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

def main():
    random.seed(SEED)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    print("Generating synthetic reconciliation data ...")
    print(f"  Seed: {SEED}\n")

    # 1 — Internal transactions
    txns = generate_internal_transactions()
    print(f"  [1/6] Internal transactions: {len(txns)}")

    # 2 — Match plan
    plan, unmatched_int_ids = create_match_plan(txns)
    print(f"  [2/6] Match plan:  {len(plan)} matched | "
          f"{len(unmatched_int_ids)} unmatched-internal")

    # 3 — External records (matched)
    ext_all: list[dict] = []
    gt_matches: list[dict] = []
    for i, entry in enumerate(plan):
        ext = apply_noise(entry["txn"], entry["noise_type"], i + 1)
        ext_all.append(ext)
        gt_matches.append({
            "internal_id": entry["txn"]["txn_id"],
            "external_id": ext["ext_id"],
            "noise_type": entry["noise_type"],
        })
    print(f"  [3/6] Matched external records: {len(ext_all)}")

    # 4 — Unmatched external records
    unmatched_ext = generate_unmatched_externals(len(ext_all) + 1)
    ext_all.extend(unmatched_ext)
    print(f"  [4/6] Unmatched external records: {len(unmatched_ext)}")
    print(f"         Total external: {len(ext_all)}")

    # 5 — Assign formats
    assign_formats(ext_all)
    fmt_a = sorted([r for r in ext_all if r["_format"] == "A"], key=lambda r: r["date"])
    fmt_b = sorted([r for r in ext_all if r["_format"] == "B"], key=lambda r: r["date"])
    fmt_c = [r for r in ext_all if r["_format"] == "C"]   # JSON — leave unsorted
    print(f"  [5/6] Format split:  A={len(fmt_a)} | B={len(fmt_b)} | C={len(fmt_c)}")

    # Tag ground truth with format
    id_to_fmt = {r["ext_id"]: r["_format"] for r in ext_all}
    for gt in gt_matches:
        gt["external_format"] = id_to_fmt[gt["external_id"]]

    # 6 — Write files
    write_internal_csv(txns, DATA_DIR / "internal_transactions.csv")
    write_format_a(fmt_a, DATA_DIR / "external_format_a.csv")
    write_format_b(fmt_b, DATA_DIR / "external_format_b.csv")
    write_format_c(fmt_c, DATA_DIR / "external_format_c.json")
    write_ground_truth(gt_matches, unmatched_int_ids, unmatched_ext,
                       DATA_DIR / "ground_truth.json")
    print(f"  [6/6] Files written to {DATA_DIR.resolve()}")

    # ── Summary ──
    noise_counts = Counter(gt["noise_type"] for gt in gt_matches)
    fmt_counts = Counter(gt["external_format"] for gt in gt_matches)

    print(f"\n{'=' * 60}")
    print("  DATA GENERATION COMPLETE")
    print(f"{'=' * 60}")
    print(f"  Internal records:       {len(txns)}")
    print(f"  External records:       {len(ext_all)}")
    print(f"  Ground-truth matches:   {len(gt_matches)}")
    print(f"  Unmatched internal:     {len(unmatched_int_ids)}")
    print(f"  Unmatched external:     {len(unmatched_ext)}")
    print()
    print("  Noise distribution:")
    for k in sorted(noise_counts):
        print(f"    {k:20s}  {noise_counts[k]}")
    print()
    print("  Matched-pair format distribution:")
    for fmt in ("A", "B", "C"):
        print(f"    Format {fmt}:  {fmt_counts.get(fmt, 0)} matched"
              f" + {len([r for r in ext_all if r['_format'] == fmt]) - fmt_counts.get(fmt, 0)} unmatched"
              f" = {len([r for r in ext_all if r['_format'] == fmt])} total")
    print()

    # Type breakdown
    type_counts = Counter(t["txn_type"] for t in txns)
    print("  Internal transaction types:")
    for t in sorted(type_counts):
        print(f"    {t:15s}  {type_counts[t]}")

    method_counts = Counter(t["payment_method"] for t in txns)
    print()
    print("  Payment methods:")
    for m in sorted(method_counts):
        print(f"    {m:15s}  {method_counts[m]}")

    print(f"\n{'=' * 60}\n")


if __name__ == "__main__":
    main()
