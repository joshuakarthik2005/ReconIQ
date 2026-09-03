#!/usr/bin/env python3
"""
Test: Ingestion / Parsing Layer
===============================

Validates that all 3 external formats + internal CSV parse correctly into
the canonical schema, with Decimal amounts, normalised dates, and no
silent data loss.

Run:  python -m pytest tests/test_ingestion.py -v
      python tests/test_ingestion.py          (legacy script mode)
"""

import json
import re
import sys
from collections import Counter
from decimal import Decimal, ROUND_HALF_UP, ROUND_HALF_EVEN
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.ingestion import (
    parse_internal,
    parse_format_a,
    parse_format_b,
    parse_format_c,
    parse_all_external,
    save_parse_errors,
    _to_decimal_2dp,
    TWO_DP,
)
from src.schemas import InternalTransaction, ExternalTransaction

DATA_DIR = PROJECT_ROOT / "data"
REPORTS_DIR = PROJECT_ROOT / "reports"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ── Fixtures ─────────────────────────────────────────────────

@pytest.fixture(scope="module")
def internal_data():
    records, errors = parse_internal(DATA_DIR / "internal_transactions.csv")
    return records, errors


@pytest.fixture(scope="module")
def format_a_data():
    records, errors = parse_format_a(DATA_DIR / "external_format_a.csv")
    return records, errors


@pytest.fixture(scope="module")
def format_b_data():
    records, errors = parse_format_b(DATA_DIR / "external_format_b.csv")
    return records, errors


@pytest.fixture(scope="module")
def format_c_data():
    records, errors = parse_format_c(DATA_DIR / "external_format_c.json")
    return records, errors


@pytest.fixture(scope="module")
def combined_external():
    records, errors = parse_all_external(DATA_DIR)
    return records, errors


@pytest.fixture(scope="module")
def ground_truth():
    gt_path = DATA_DIR / "ground_truth.json"
    return json.loads(gt_path.read_text(encoding="utf-8"))


# ── Internal Transactions ────────────────────────────────────

class TestInternalTransactions:
    def test_parse_count(self, internal_data):
        records, _ = internal_data
        assert len(records) == 65

    def test_zero_parse_errors(self, internal_data):
        _, errors = internal_data
        assert len(errors) == 0

    def test_all_are_internal_transaction(self, internal_data):
        records, _ = internal_data
        assert all(isinstance(r, InternalTransaction) for r in records)

    def test_all_amounts_are_decimal(self, internal_data):
        records, _ = internal_data
        assert all(isinstance(r.amount, Decimal) for r in records)

    def test_all_amounts_have_2dp(self, internal_data):
        records, _ = internal_data
        assert all(r.amount == r.amount.quantize(Decimal("0.01")) for r in records)

    def test_all_dates_are_yyyy_mm_dd(self, internal_data):
        records, _ = internal_data
        assert all(DATE_RE.match(r.date) for r in records)

    def test_no_zero_amounts(self, internal_data):
        records, _ = internal_data
        assert all(r.amount != 0 for r in records)


# ── Format A (Clean CSV) ────────────────────────────────────

class TestFormatA:
    def test_parse_count(self, format_a_data):
        records, _ = format_a_data
        assert len(records) == 25

    def test_zero_parse_errors(self, format_a_data):
        _, errors = format_a_data
        assert len(errors) == 0

    def test_all_are_external_transaction(self, format_a_data):
        records, _ = format_a_data
        assert all(isinstance(r, ExternalTransaction) for r in records)

    def test_all_source_format_a(self, format_a_data):
        records, _ = format_a_data
        assert all(r.source_format == "A" for r in records)

    def test_all_amounts_are_decimal(self, format_a_data):
        records, _ = format_a_data
        assert all(isinstance(r.amount, Decimal) for r in records)

    def test_all_amounts_have_2dp(self, format_a_data):
        records, _ = format_a_data
        assert all(r.amount == r.amount.quantize(Decimal("0.01")) for r in records)

    def test_all_dates_are_yyyy_mm_dd(self, format_a_data):
        records, _ = format_a_data
        assert all(DATE_RE.match(r.date) for r in records)

    def test_all_have_ext_id(self, format_a_data):
        records, _ = format_a_data
        assert all(r.ext_id.startswith("EXT_") for r in records)

    def test_raw_description_populated(self, format_a_data):
        records, _ = format_a_data
        assert all(r.raw_description == r.description for r in records)


# ── Format B (Messy CSV) ────────────────────────────────────

class TestFormatB:
    def test_parse_count(self, format_b_data):
        records, _ = format_b_data
        assert len(records) == 18

    def test_zero_parse_errors(self, format_b_data):
        _, errors = format_b_data
        assert len(errors) == 0

    def test_all_are_external_transaction(self, format_b_data):
        records, _ = format_b_data
        assert all(isinstance(r, ExternalTransaction) for r in records)

    def test_all_source_format_b(self, format_b_data):
        records, _ = format_b_data
        assert all(r.source_format == "B" for r in records)

    def test_all_amounts_are_decimal(self, format_b_data):
        records, _ = format_b_data
        assert all(isinstance(r.amount, Decimal) for r in records)

    def test_all_amounts_have_2dp(self, format_b_data):
        records, _ = format_b_data
        assert all(r.amount == r.amount.quantize(Decimal("0.01")) for r in records)

    def test_all_dates_are_yyyy_mm_dd(self, format_b_data):
        records, _ = format_b_data
        assert all(DATE_RE.match(r.date) for r in records)

    def test_all_have_ext_id(self, format_b_data):
        records, _ = format_b_data
        assert all(r.ext_id.startswith("EXT_") for r in records)

    def test_no_neft_imps_prefix_in_ref(self, format_b_data):
        records, _ = format_b_data
        assert all(
            not r.reference_id.startswith(("NEFT/", "IMPS/"))
            for r in records if r.reference_id
        )

    def test_no_currency_symbols_in_amounts(self, format_b_data):
        # If amounts parsed as Decimal, symbols were stripped
        records, _ = format_b_data
        assert all(isinstance(r.amount, Decimal) for r in records)


# ── Format C (JSON Feed) ────────────────────────────────────

class TestFormatC:
    def test_parse_count(self, format_c_data):
        records, _ = format_c_data
        assert len(records) == 12

    def test_zero_parse_errors(self, format_c_data):
        _, errors = format_c_data
        assert len(errors) == 0

    def test_all_are_external_transaction(self, format_c_data):
        records, _ = format_c_data
        assert all(isinstance(r, ExternalTransaction) for r in records)

    def test_all_source_format_c(self, format_c_data):
        records, _ = format_c_data
        assert all(r.source_format == "C" for r in records)

    def test_all_amounts_are_decimal(self, format_c_data):
        records, _ = format_c_data
        assert all(isinstance(r.amount, Decimal) for r in records)

    def test_all_amounts_have_2dp(self, format_c_data):
        records, _ = format_c_data
        assert all(r.amount == r.amount.quantize(Decimal("0.01")) for r in records)

    def test_all_dates_are_yyyy_mm_dd(self, format_c_data):
        records, _ = format_c_data
        assert all(DATE_RE.match(r.date) for r in records)

    def test_all_have_ref_extraction_path(self, format_c_data):
        records, _ = format_c_data
        assert all("_ref_extraction_path" in r.original_data for r in records)

    def test_raw_description_retained(self, format_c_data):
        records, _ = format_c_data
        assert all(isinstance(r.raw_description, str) for r in records)


# ── Description-Only Clue Records (Format C) ────────────────

class TestDescriptionOnlyRecords:
    def test_desc_only_ref_id_is_none(self, format_c_data, ground_truth):
        records, _ = format_c_data
        desc_only_in_c = [
            m for m in ground_truth["matches"]
            if m["noise_type"] == "description_only" and m["external_format"] == "C"
        ]
        for m in desc_only_in_c:
            ext_id = m["external_id"]
            parsed = next((r for r in records if r.ext_id == ext_id), None)
            assert parsed is not None, f"{ext_id} not found in parsed records"
            assert parsed.reference_id is None, (
                f"{ext_id}: expected ref_id=None, got {parsed.reference_id!r}"
            )

    def test_desc_only_raw_description_has_merchant(self, format_c_data, ground_truth):
        records, _ = format_c_data
        desc_only_in_c = [
            m for m in ground_truth["matches"]
            if m["noise_type"] == "description_only" and m["external_format"] == "C"
        ]
        for m in desc_only_in_c:
            ext_id = m["external_id"]
            parsed = next((r for r in records if r.ext_id == ext_id), None)
            assert parsed is not None, f"{ext_id} not found"
            assert "RAZORPAY*" in parsed.raw_description, (
                f"{ext_id}: raw_description={parsed.raw_description!r}"
            )


# ── Narrative Regex Extraction Records (Format C) ───────────

class TestNarrativeRegex:
    def test_regex_ref_extracted(self, format_c_data):
        records, _ = format_c_data
        regex_records = [
            r for r in records
            if r.original_data.get("_ref_extraction_path") == "narrative_regex"
        ]
        for r in regex_records:
            assert r.reference_id is not None and len(r.reference_id) > 4, (
                f"{r.ext_id}: ref_id={r.reference_id!r}"
            )

    def test_regex_raw_description_retained(self, format_c_data):
        records, _ = format_c_data
        regex_records = [
            r for r in records
            if r.original_data.get("_ref_extraction_path") == "narrative_regex"
        ]
        for r in regex_records:
            assert bool(r.raw_description), (
                f"{r.ext_id}: raw_description empty"
            )


# ── Combined External Parse ─────────────────────────────────

class TestCombinedExternal:
    def test_total_count(self, combined_external):
        records, _ = combined_external
        assert len(records) == 55

    def test_zero_parse_errors(self, combined_external):
        _, errors = combined_external
        assert len(errors) == 0

    def test_format_counts(self, combined_external):
        records, _ = combined_external
        fmt_counts = Counter(r.source_format for r in records)
        assert fmt_counts == {"A": 25, "B": 18, "C": 12}

    def test_all_ext_ids_unique(self, combined_external):
        records, _ = combined_external
        ext_ids = [r.ext_id for r in records]
        assert len(ext_ids) == len(set(ext_ids))


# ── Parse Error Persistence ─────────────────────────────────

class TestParseErrorPersistence:
    def _save_errors(self, internal_data, combined_external):
        """Helper: combine and save parse errors, return (save_path, all_errors)."""
        _, int_errors = internal_data
        _, ext_errors = combined_external
        all_errors = int_errors + ext_errors
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        save_path = REPORTS_DIR / "parse_errors.json"
        save_parse_errors(all_errors, save_path)
        return save_path, all_errors

    def test_parse_errors_saved_to_file(self, internal_data, combined_external):
        save_path, _ = self._save_errors(internal_data, combined_external)
        assert save_path.exists()

    def test_saved_parse_errors_count_matches(self, internal_data, combined_external):
        save_path, all_errors = self._save_errors(internal_data, combined_external)
        saved = json.loads(save_path.read_text(encoding="utf-8"))
        assert len(saved) == len(all_errors)


# ── Decimal / ROUND_HALF_UP Proof Test ──────────────────────

class TestDecimalRounding:
    def test_round_half_up_and_even_disagree(self):
        val_up = Decimal("2.005").quantize(TWO_DP, rounding=ROUND_HALF_UP)
        val_even = Decimal("2.005").quantize(TWO_DP, rounding=ROUND_HALF_EVEN)
        assert val_up != val_even

    def test_round_half_up_correct(self):
        val_up = Decimal("2.005").quantize(TWO_DP, rounding=ROUND_HALF_UP)
        assert val_up == Decimal("2.01")

    def test_round_half_even_would_be_wrong(self):
        val_even = Decimal("2.005").quantize(TWO_DP, rounding=ROUND_HALF_EVEN)
        assert val_even == Decimal("2.00")

    def test_to_decimal_2dp_uses_round_half_up(self):
        result = _to_decimal_2dp("2.005")
        assert result == Decimal("2.01")

    def test_to_decimal_2dp_with_currency_symbol(self):
        result = _to_decimal_2dp("INR 1,234.005")
        assert result == Decimal("1234.01")

    def test_format_c_original_data_is_decimal(self, format_c_data):
        records, _ = format_c_data
        assert len(records) > 0, "No Format C records to test"
        c_sample = records[0]
        orig_val = c_sample.original_data.get("amount", {}).get("value")
        assert isinstance(orig_val, Decimal), (
            f"type={type(orig_val).__name__}, value={orig_val}"
        )

    def test_internal_amounts_are_decimal(self, internal_data):
        records, _ = internal_data
        assert all(isinstance(r.amount, Decimal) for r in records)


# ── Legacy script mode ───────────────────────────────────────

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
