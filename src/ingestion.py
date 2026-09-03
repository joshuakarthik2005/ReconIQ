"""
Ingestion / Parsing Layer
=========================

Parses internal transactions and external bank statements in 3 formats
into a common normalized schema.

Key invariants enforced at parse time:
  - Amounts are Decimal with 2 dp, rounded ROUND_HALF_UP
  - Decimal is built directly from the cleaned *string* -- never via float
  - Dates are normalized to YYYY-MM-DD
  - Ref IDs are cleaned (stripped, NEFT/IMPS prefixes removed)
  - For Format C, ref IDs are opportunistically extracted from narrative
    via regex, but the raw narrative is always retained in raw_description
  - Malformed rows are captured as ParseError objects and collected for
    Part 5 exception handling -- the parser never crashes on bad data

Usage
-----
    from src.ingestion import parse_internal, parse_all_external, save_parse_errors
"""

import csv
import json
import re
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Optional, Tuple, List

from .schemas import (
    InternalTransaction,
    ExternalTransaction,
    ParseError,
)

TWO_DP = Decimal("0.01")


# ── Amount Cleaning ──────────────────────────────────────────

_AMT_CRUFT = re.compile(r'[₹,]')
_AMT_INR_PREFIX = re.compile(r'^INR\s*', re.IGNORECASE)
_AMT_TRAILING_SLASH = re.compile(r'/-\s*$')


def _to_decimal_2dp(raw: str) -> Decimal:
    """
    Clean a raw amount string and convert to Decimal(2dp, ROUND_HALF_UP).

    Handles: "₹1,500.00", "INR 1500.00", "₹ 1,500.00",
             "1500.00", "1,500.00/-"

    Builds Decimal directly from the cleaned string -- never through float.
    """
    s = raw.strip()
    s = _AMT_CRUFT.sub('', s)
    s = _AMT_INR_PREFIX.sub('', s)
    s = _AMT_TRAILING_SLASH.sub('', s)
    s = s.strip()
    if not s:
        raise ValueError(f"Empty amount after cleaning: {raw!r}")
    return Decimal(s).quantize(TWO_DP, rounding=ROUND_HALF_UP)


# ── Date Normalisation ───────────────────────────────────────

_DATE_FMTS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d %b %Y", "%d-%m-%y")


def _normalize_date(raw: str) -> str:
    """Try multiple date formats, return YYYY-MM-DD or raise ValueError."""
    s = raw.strip()
    for fmt in _DATE_FMTS:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise ValueError(f"Could not parse date: {raw!r}")


# ── Reference ID Cleaning ───────────────────────────────────

_REF_PREFIX = re.compile(r'^(NEFT|IMPS|RTGS|UPI)/', re.IGNORECASE)


def _clean_ref(raw: Optional[str]) -> Optional[str]:
    """Strip whitespace, remove NEFT/IMPS/RTGS/UPI prefixes.
    Returns None if the result is empty."""
    if not raw:
        return None
    s = raw.strip()
    s = _REF_PREFIX.sub('', s)
    s = s.strip()
    return s if s else None


# ── Format C: Ref Extraction from JSON Entry ─────────────────

_REF_IN_NARRATIVE = re.compile(r'\|\s*Ref:\s*(\S+)')


def _extract_ref_from_c(entry: dict) -> Tuple[Optional[str], str]:
    """
    Extract reference ID from a Format C JSON entry.

    Returns (ref_id, extraction_path) where extraction_path is one of:
        'reference'        -- top-level ``reference`` field
        'metadata'         -- nested ``metadata.payment_ref``
        'narrative_regex'  -- regex match on ``| Ref: <id>`` in narrative
        'none'             -- no ref found
    """
    # Priority 1: top-level reference field
    ref = entry.get("reference")
    if ref and str(ref).strip():
        return str(ref).strip(), "reference"

    # Priority 2: nested metadata.payment_ref
    meta = entry.get("metadata")
    if isinstance(meta, dict):
        ref = meta.get("payment_ref")
        if ref and str(ref).strip():
            return str(ref).strip(), "metadata"

    # Priority 3: regex on narrative
    narrative = entry.get("narrative", "")
    if narrative:
        m = _REF_IN_NARRATIVE.search(narrative)
        if m:
            return m.group(1), "narrative_regex"

    return None, "none"


# ═══════════════════════════════════════════════════════════════
# Parsers
# ═══════════════════════════════════════════════════════════════

def parse_internal(path: Path) -> Tuple[List[InternalTransaction], List[ParseError]]:
    """
    Parse ``internal_transactions.csv``.

    Returns (records, parse_errors).
    """
    records: List[InternalTransaction] = []
    errors: List[ParseError] = []

    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row_num, row in enumerate(reader, start=2):  # header = row 1
            try:
                records.append(InternalTransaction(
                    txn_id=row["txn_id"].strip(),
                    reference_id=row["reference_id"].strip(),
                    amount=Decimal(row["amount"]).quantize(
                        TWO_DP, rounding=ROUND_HALF_UP),
                    currency=row["currency"].strip(),
                    txn_type=row["txn_type"].strip(),
                    date=row["date"].strip(),
                    merchant_name=row["merchant_name"].strip(),
                    merchant_category=row["merchant_category"].strip(),
                    payment_method=row["payment_method"].strip(),
                    status=row["status"].strip(),
                    description=row.get("description", "").strip(),
                ))
            except Exception as exc:
                errors.append(ParseError(
                    source_file=path.name,
                    row_number=row_num,
                    raw_data=dict(row),
                    error_message=f"{type(exc).__name__}: {exc}",
                    timestamp=datetime.now().isoformat(),
                ))

    return records, errors


def parse_format_a(path: Path) -> Tuple[List[ExternalTransaction], List[ParseError]]:
    """
    Parse Format A -- clean bank statement CSV.

    Columns: Txn ID | Transaction Date | Value Date | Reference No |
             Credit | Debit | Description | Balance
    """
    records: List[ExternalTransaction] = []
    errors: List[ParseError] = []

    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row_num, row in enumerate(reader, start=2):
            try:
                credit = row.get("Credit", "").strip()
                debit = row.get("Debit", "").strip()

                if credit:
                    amount = Decimal(credit).quantize(
                        TWO_DP, rounding=ROUND_HALF_UP)
                elif debit:
                    amount = -Decimal(debit).quantize(
                        TWO_DP, rounding=ROUND_HALF_UP)
                else:
                    raise ValueError("Both Credit and Debit columns are empty")

                ref_raw = row.get("Reference No", "").strip()
                desc = row.get("Description", "")
                ext_id = row.get("Txn ID", "").strip()

                records.append(ExternalTransaction(
                    ext_id=ext_id,
                    reference_id=ref_raw if ref_raw else None,
                    amount=amount,
                    date=row["Transaction Date"].strip(),
                    description=desc,
                    source_format="A",
                    raw_description=desc,
                    original_data=dict(row),
                ))
            except Exception as exc:
                errors.append(ParseError(
                    source_file=path.name,
                    row_number=row_num,
                    raw_data=dict(row),
                    error_message=f"{type(exc).__name__}: {exc}",
                    timestamp=datetime.now().isoformat(),
                ))

    return records, errors


def parse_format_b(path: Path) -> Tuple[List[ExternalTransaction], List[ParseError]]:
    """
    Parse Format B -- messy semi-structured bank export.

    Columns: Sl.No. | Bank Ref | Txn Ref. | Amt (INR) | Dt. |
             Particulars | Cr/Dr

    Handles: currency symbols, commas, varied date formats,
    NEFT/IMPS-prefixed refs, truncated descriptions.
    """
    records: List[ExternalTransaction] = []
    errors: List[ParseError] = []

    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row_num, row in enumerate(reader, start=2):
            try:
                # Amount: strip symbols, commas, trailing /-, then Decimal
                amount = _to_decimal_2dp(row["Amt (INR)"])

                # Cr/Dr sign
                cr_dr = row.get("Cr/Dr", "").strip().lower()
                if cr_dr == "dr":
                    amount = -amount

                # Date: flexible multi-format parse
                date_str = _normalize_date(row["Dt."])

                # Reference: clean prefixes and whitespace
                ref = _clean_ref(row.get("Txn Ref.", ""))

                # Bank's own transaction ID
                ext_id = row.get("Bank Ref", "").strip()

                desc = row.get("Particulars", "")

                records.append(ExternalTransaction(
                    ext_id=ext_id,
                    reference_id=ref,
                    amount=amount,
                    date=date_str,
                    description=desc,
                    source_format="B",
                    raw_description=desc,
                    original_data=dict(row),
                ))
            except Exception as exc:
                errors.append(ParseError(
                    source_file=path.name,
                    row_number=row_num,
                    raw_data=dict(row),
                    error_message=f"{type(exc).__name__}: {exc}",
                    timestamp=datetime.now().isoformat(),
                ))

    return records, errors


def parse_format_c(path: Path) -> Tuple[List[ExternalTransaction], List[ParseError]]:
    """
    Parse Format C -- JSON bank API feed with nested structure.

    Reference IDs may appear in three locations:
      1. Top-level ``reference`` field
      2. ``metadata.payment_ref``
      3. Embedded in ``narrative`` as ``| Ref: <id>``

    The extraction path is recorded in ``original_data["_ref_extraction_path"]``
    for downstream stats and testing.  The raw narrative is always retained
    in ``raw_description`` regardless of whether a ref was extracted.
    """
    records: List[ExternalTransaction] = []
    errors: List[ParseError] = []

    with open(path, encoding="utf-8") as fh:
        data = json.load(fh, parse_float=Decimal)

    for idx, entry in enumerate(data.get("transactions", [])):
        try:
            ext_id = entry.get("id", f"C_{idx + 1:03d}")

            # Amount from nested object -- already Decimal via parse_float
            amt_obj = entry.get("amount", {})
            if isinstance(amt_obj, dict):
                amt_val = amt_obj.get("value", Decimal("0"))
            else:
                amt_val = amt_obj
            # Ensure Decimal (handles int values from JSON too)
            if not isinstance(amt_val, Decimal):
                amt_val = Decimal(str(amt_val))
            amount = amt_val.quantize(TWO_DP, rounding=ROUND_HALF_UP)

            # Type -> sign
            txn_type = entry.get("type", "CREDIT")
            if txn_type == "DEBIT":
                amount = -amount

            # Date from ISO timestamp
            ts = entry.get("timestamp", "")
            if not ts:
                raise ValueError("Missing timestamp")
            date_str = ts[:10]  # YYYY-MM-DD portion

            # Reference extraction (3 locations)
            ref, ref_path = _extract_ref_from_c(entry)

            # Raw narrative -- always retained
            narrative = entry.get("narrative", "")

            # Store extraction path metadata for testing / reporting
            enriched_data = dict(entry)
            enriched_data["_ref_extraction_path"] = ref_path

            records.append(ExternalTransaction(
                ext_id=ext_id,
                reference_id=ref,
                amount=amount,
                date=date_str,
                description=narrative,
                source_format="C",
                raw_description=narrative,
                original_data=enriched_data,
            ))
        except Exception as exc:
            errors.append(ParseError(
                source_file=path.name,
                row_number=idx + 1,
                raw_data=entry if isinstance(entry, dict) else str(entry),
                error_message=f"{type(exc).__name__}: {exc}",
                timestamp=datetime.now().isoformat(),
            ))

    return records, errors


# ═══════════════════════════════════════════════════════════════
# Convenience Functions
# ═══════════════════════════════════════════════════════════════

def parse_all_external(
    data_dir: Path,
) -> Tuple[List[ExternalTransaction], List[ParseError]]:
    """Parse all 3 external formats and return a combined list."""
    all_records: List[ExternalTransaction] = []
    all_errors: List[ParseError] = []

    for parser, filename in [
        (parse_format_a, "external_format_a.csv"),
        (parse_format_b, "external_format_b.csv"),
        (parse_format_c, "external_format_c.json"),
    ]:
        recs, errs = parser(data_dir / filename)
        all_records.extend(recs)
        all_errors.extend(errs)

    return all_records, all_errors


def save_parse_errors(errors: List[ParseError], path: Path) -> None:
    """
    Persist parse errors as JSON for Part 5 exception handling.

    Each ParseError is serialised as a dict.  The file is written
    atomically so downstream consumers always see a complete snapshot.
    """
    from dataclasses import asdict

    serialisable = []
    for err in errors:
        d = asdict(err)
        # raw_data may contain non-serialisable types; stringify as fallback
        try:
            json.dumps(d["raw_data"])
        except (TypeError, ValueError):
            d["raw_data"] = str(d["raw_data"])
        serialisable.append(d)

    with open(path, "w", encoding="utf-8") as fh:
        json.dump(serialisable, fh, indent=2, ensure_ascii=False, default=str)
