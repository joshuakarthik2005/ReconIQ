#!/usr/bin/env python3
"""
Unit tests for LLM matcher components (no API key needed).
Tests shortlist building, stoplist, group detection, response parsing.

Run:  python -m pytest tests/test_llm_unit.py -v
      python tests/test_llm_unit.py          (legacy script mode)
"""

import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.ingestion import parse_internal, parse_all_external
from src.deterministic_matcher import run_deterministic_matching
from src.llm_matcher import (
    _build_candidate_shortlist,
    _detect_ambiguity_groups,
    _distinctive_words,
    _parse_single_response,
    _DESCRIPTION_STOPWORDS,
)
from src.schemas import ExternalTransaction

DATA_DIR = PROJECT_ROOT / "data"


# ── Fixtures ─────────────────────────────────────────────────

@pytest.fixture(scope="module")
def det_output():
    int_records, _ = parse_internal(DATA_DIR / "internal_transactions.csv")
    ext_records, _ = parse_all_external(DATA_DIR)
    return run_deterministic_matching(int_records, ext_records)


@pytest.fixture(scope="module")
def ground_truth():
    return json.loads((DATA_DIR / "ground_truth.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def fake_ext():
    return [ExternalTransaction(
        ext_id="EXT_999", reference_id=None,
        amount=Decimal("100"), date="2026-08-01",
        description="test", source_format="A",
    )]


# ── Stoplist ─────────────────────────────────────────────────

class TestStoplist:
    @pytest.mark.parametrize("word", [
        "razorpay", "payment", "settlement", "transaction",
        "transfer", "credit", "debit", "charges", "software",
    ])
    def test_word_in_stoplist(self, word):
        assert word in _DESCRIPTION_STOPWORDS

    def test_settlement_filtered_out(self):
        words = _distinctive_words("Settlement net of charges - PharmEasy Healthcare")
        assert "settlement" not in words

    def test_charges_filtered_out(self):
        words = _distinctive_words("Settlement net of charges - PharmEasy Healthcare")
        assert "charges" not in words

    def test_pharmeasy_kept(self):
        words = _distinctive_words("Settlement net of charges - PharmEasy Healthcare")
        assert "pharmeasy" in words

    def test_healthcare_kept(self):
        words = _distinctive_words("Settlement net of charges - PharmEasy Healthcare")
        assert "healthcare" in words

    def test_razorpay_filtered(self):
        words = _distinctive_words("RAZORPAY*Amazon Seller Services*UPI")
        assert "razorpay" not in words

    def test_amazon_kept(self):
        words = _distinctive_words("RAZORPAY*Amazon Seller Services*UPI")
        assert "amazon" in words

    def test_seller_kept(self):
        words = _distinctive_words("RAZORPAY*Amazon Seller Services*UPI")
        assert "seller" in words


# ── Group Detection ──────────────────────────────────────────

class TestGroupDetection:
    def test_zero_groups(self, det_output):
        groups, grouped_ids = _detect_ambiguity_groups(
            det_output.residual_internal, det_output.residual_external, set()
        )
        assert len(groups) == 0


# ── Shortlist: Partial Refund ────────────────────────────────

class TestShortlistPartialRefund:
    @pytest.mark.parametrize("int_id,ext_id", [
        ("TXN_031", "EXT_003"),
        ("TXN_005", "EXT_020"),
        ("TXN_054", "EXT_041"),
        ("TXN_026", "EXT_043"),
    ])
    def test_correct_ext_in_shortlist(self, det_output, int_id, ext_id):
        int_map = {r.txn_id: r for r in det_output.residual_internal}
        txn = int_map[int_id]
        cands = _build_candidate_shortlist(
            txn, det_output.residual_external, set(), 5
        )
        cand_ids = [c.ext_id for c in cands]
        assert ext_id in cand_ids, (
            f"{int_id}: expected {ext_id} in {cand_ids}"
        )


# ── Shortlist: Date Drift ───────────────────────────────────

class TestShortlistDateDrift:
    def test_correct_ext_in_shortlist(self, det_output, ground_truth):
        int_map = {r.txn_id: r for r in det_output.residual_internal}
        for m in ground_truth["matches"]:
            if m["noise_type"] == "date_drift" and m["internal_id"] in int_map:
                txn = int_map[m["internal_id"]]
                cands = _build_candidate_shortlist(
                    txn, det_output.residual_external, set(), 5
                )
                cand_ids = [c.ext_id for c in cands]
                assert m["external_id"] in cand_ids, (
                    f"{txn.txn_id}: expected {m['external_id']} in {cand_ids}"
                )


# ── Response Parsing: Single ────────────────────────────────

class TestParseSingleResponse:
    def test_valid_match_returns_ext_id(self, fake_ext):
        ext_id, conf, reason, is_pr = _parse_single_response(
            '{"match_index": 1, "confidence": 0.85, "reasoning": "test"}',
            fake_ext, 0.7,
        )
        assert ext_id == "EXT_999"

    def test_confidence_passed_through(self, fake_ext):
        _, conf, _, _ = _parse_single_response(
            '{"match_index": 1, "confidence": 0.85, "reasoning": "test"}',
            fake_ext, 0.7,
        )
        assert conf == 0.85

    def test_null_match_returns_none(self, fake_ext):
        ext_id, _, _, _ = _parse_single_response(
            '{"match_index": null, "confidence": 0.1, "reasoning": "no match"}',
            fake_ext, 0.7,
        )
        assert ext_id is None

    def test_below_threshold_returns_none(self, fake_ext):
        ext_id, _, _, _ = _parse_single_response(
            '{"match_index": 1, "confidence": 0.5, "reasoning": "weak"}',
            fake_ext, 0.7,
        )
        assert ext_id is None

    def test_below_threshold_noted_in_reasoning(self, fake_ext):
        _, _, reason, _ = _parse_single_response(
            '{"match_index": 1, "confidence": 0.5, "reasoning": "weak"}',
            fake_ext, 0.7,
        )
        assert "Below threshold" in reason

    def test_invalid_json_returns_none(self, fake_ext):
        ext_id, _, _, _ = _parse_single_response("not json", fake_ext, 0.7)
        assert ext_id is None

    def test_out_of_bounds_index_returns_none(self, fake_ext):
        ext_id, _, _, _ = _parse_single_response(
            '{"match_index": 5, "confidence": 0.9, "reasoning": "oob"}',
            fake_ext, 0.7,
        )
        assert ext_id is None

    def test_is_partial_refund_true(self, fake_ext):
        """is_partial_refund=true in JSON → True in 4th element."""
        _, _, _, is_pr = _parse_single_response(
            '{"match_index": 1, "confidence": 0.9, '
            '"is_partial_refund": true, "reasoning": "net after refund"}',
            fake_ext, 0.7,
        )
        assert is_pr is True

    def test_is_partial_refund_false(self, fake_ext):
        """is_partial_refund=false in JSON → False in 4th element."""
        _, _, _, is_pr = _parse_single_response(
            '{"match_index": 1, "confidence": 0.9, '
            '"is_partial_refund": false, "reasoning": "exact match"}',
            fake_ext, 0.7,
        )
        assert is_pr is False

    def test_is_partial_refund_missing_defaults_false(self, fake_ext):
        """is_partial_refund absent from JSON → defaults to False."""
        _, _, _, is_pr = _parse_single_response(
            '{"match_index": 1, "confidence": 0.9, "reasoning": "match"}',
            fake_ext, 0.7,
        )
        assert is_pr is False

    def test_below_threshold_clears_partial_refund(self, fake_ext):
        """Below confidence threshold → is_partial_refund forced to False."""
        _, _, _, is_pr = _parse_single_response(
            '{"match_index": 1, "confidence": 0.5, '
            '"is_partial_refund": true, "reasoning": "weak partial"}',
            fake_ext, 0.7,
        )
        assert is_pr is False


# ── Legacy script mode ───────────────────────────────────────

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
