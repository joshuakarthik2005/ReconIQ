#!/usr/bin/env python3
"""
Test: LLM-Assisted Matching (Part 3) — LIVE API
=================================================

Runs the LLM matcher on the residual pool from Part 2, validates against
ground truth, and reports match accuracy, exception correctness, and costs.

Requires GEMINI_API_KEY or GOOGLE_API_KEY in the environment.
Skipped automatically when no API key is available.

Run:  python -m pytest tests/test_llm_matcher.py -v
      python tests/test_llm_matcher.py          (legacy script mode)
"""

import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.ingestion import parse_internal, parse_all_external
from src.deterministic_matcher import run_deterministic_matching
from src.llm_matcher import (
    run_llm_matching,
    _build_candidate_shortlist,
    _detect_ambiguity_groups,
    _distinctive_words,
    _DESCRIPTION_STOPWORDS,
)

DATA_DIR = PROJECT_ROOT / "data"

_HAS_API_KEY = bool(
    os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
)
_skip_no_key = pytest.mark.skipif(
    not _HAS_API_KEY,
    reason="No GEMINI_API_KEY / GOOGLE_API_KEY in environment"
)


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
def llm_output(det_output):
    """Run LLM matching live — only instantiated when API key present."""
    return run_llm_matching(
        det_output.residual_internal,
        det_output.residual_external,
    )


# ── Unit tests (no API key needed) ──────────────────────────

class TestStoplist:
    def test_razorpay_in_stoplist(self):
        assert "razorpay" in _DESCRIPTION_STOPWORDS

    def test_payment_in_stoplist(self):
        assert "payment" in _DESCRIPTION_STOPWORDS

    def test_settlement_in_stoplist(self):
        assert "settlement" in _DESCRIPTION_STOPWORDS

    def test_transaction_in_stoplist(self):
        assert "transaction" in _DESCRIPTION_STOPWORDS

    def test_filters_settlement_from_description(self):
        words = _distinctive_words("Settlement net of charges - PharmEasy Healthcare")
        assert "settlement" not in words

    def test_keeps_pharmeasy(self):
        words = _distinctive_words("Settlement net of charges - PharmEasy Healthcare")
        assert "pharmeasy" in words

    def test_keeps_healthcare(self):
        words = _distinctive_words("Settlement net of charges - PharmEasy Healthcare")
        assert "healthcare" in words


class TestShortlistBuilding:
    def test_partial_refund_correct_ext_in_shortlist(self, det_output, ground_truth):
        gt_matches = ground_truth["matches"]
        int_map = {r.txn_id: r for r in det_output.residual_internal}
        for m in gt_matches:
            if m["noise_type"] == "partial_refund" and m["internal_id"] in int_map:
                txn = int_map[m["internal_id"]]
                candidates = _build_candidate_shortlist(
                    txn, det_output.residual_external, set(), 5
                )
                cand_ids = [c.ext_id for c in candidates]
                assert m["external_id"] in cand_ids, (
                    f"Shortlist for {txn.txn_id} (partial_refund) missing "
                    f"{m['external_id']}, got {cand_ids}"
                )
                break  # original test only checked the first one


class TestAmbiguityGroupDetection:
    def test_zero_groups(self, det_output):
        groups, grouped_ids = _detect_ambiguity_groups(
            det_output.residual_internal, det_output.residual_external, set()
        )
        assert len(groups) == 0


# ── Live API tests (require key) ────────────────────────────

@_skip_no_key
class TestLLMLiveStructural:
    def test_no_internal_matched_twice(self, llm_output):
        llm_int_ids = {m.internal_id for m in llm_output.matched}
        assert len(llm_int_ids) == len(llm_output.matched)

    def test_no_external_matched_twice(self, llm_output):
        llm_ext_ids = {m.external_id for m in llm_output.matched}
        assert len(llm_ext_ids) == len(llm_output.matched)

    def test_matched_plus_exceptions_equals_residual(self, llm_output, det_output):
        llm_int_ids = {m.internal_id for m in llm_output.matched}
        assert (
            len(llm_int_ids) + len(llm_output.exceptions_internal)
            == len(det_output.residual_internal)
        )

    def test_all_matches_have_llm_path(self, llm_output):
        assert all(m.match_path.value == "llm" for m in llm_output.matched)

    def test_all_matches_have_reasoning(self, llm_output):
        assert all(m.reasoning for m in llm_output.matched)

    def test_no_overlap_with_part2_internal(self, llm_output, det_output):
        llm_int_ids = {m.internal_id for m in llm_output.matched}
        det_int_ids = {m.internal_id for m in det_output.matched}
        assert llm_int_ids.isdisjoint(det_int_ids)

    def test_no_overlap_with_part2_external(self, llm_output, det_output):
        llm_ext_ids = {m.external_id for m in llm_output.matched}
        det_ext_ids = {m.external_id for m in det_output.matched}
        assert llm_ext_ids.isdisjoint(det_ext_ids)


@_skip_no_key
class TestLLMLiveGroundTruth:
    def test_zero_false_positives(self, llm_output, ground_truth):
        gt_lookup = {m["internal_id"]: m["external_id"] for m in ground_truth["matches"]}
        wrong = []
        for m in llm_output.matched:
            expected = gt_lookup.get(m.internal_id)
            if not (expected and m.external_id == expected):
                wrong.append(
                    f"{m.internal_id} -> {m.external_id} (expected {expected or 'no match'})"
                )
        assert len(wrong) == 0, "\n".join(wrong)


@_skip_no_key
class TestLLMLiveExceptions:
    def test_all_unmatched_internals_are_exceptions(self, llm_output, ground_truth):
        gt_unmatched_int = set(ground_truth["unmatched_internal"])
        exc_int_set = set(llm_output.exceptions_internal)
        assert gt_unmatched_int.issubset(exc_int_set), (
            f"missing: {gt_unmatched_int - exc_int_set}"
        )

    def test_all_unmatched_externals_are_exceptions(self, llm_output, ground_truth):
        gt_unmatched_ext = set(ground_truth["unmatched_external"])
        exc_ext_set = set(llm_output.exceptions_external)
        assert gt_unmatched_ext.issubset(exc_ext_set), (
            f"missing: {gt_unmatched_ext - exc_ext_set}"
        )


# ── Legacy script mode ───────────────────────────────────────

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
