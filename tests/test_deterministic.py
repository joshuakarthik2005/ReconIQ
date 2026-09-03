#!/usr/bin/env python3
"""
Test: Deterministic Matching Engine
====================================

Runs the rule-based matcher on the full batch, validates against ground truth,
and reports per-rule hit counts, accuracy, and throughput.

Run:  python -m pytest tests/test_deterministic.py -v
      python tests/test_deterministic.py          (legacy script mode)
"""

import json
import sys
import time
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.ingestion import parse_internal, parse_all_external
from src.deterministic_matcher import run_deterministic_matching

DATA_DIR = PROJECT_ROOT / "data"


# ── Fixtures ─────────────────────────────────────────────────

@pytest.fixture(scope="module")
def data():
    int_records, _ = parse_internal(DATA_DIR / "internal_transactions.csv")
    ext_records, _ = parse_all_external(DATA_DIR)
    return int_records, ext_records


@pytest.fixture(scope="module")
def det_output(data):
    int_records, ext_records = data
    return run_deterministic_matching(int_records, ext_records)


@pytest.fixture(scope="module")
def ground_truth():
    gt = json.loads((DATA_DIR / "ground_truth.json").read_text(encoding="utf-8"))
    return gt


# ── Structural Checks ───────────────────────────────────────

class TestStructuralChecks:
    def test_total_equals_matched_plus_residual(self, data, det_output):
        int_records, _ = data
        assert len(det_output.matched) + len(det_output.residual_internal) == len(int_records)

    def test_no_ext_id_claimed_twice(self, det_output):
        claimed_ext_ids = {m.external_id for m in det_output.matched}
        assert len(claimed_ext_ids) == len(det_output.matched)

    def test_claimed_plus_residual_equals_all_external(self, data, det_output):
        _, ext_records = data
        claimed_ext_ids = {m.external_id for m in det_output.matched}
        residual_ext_ids = {e.ext_id for e in det_output.residual_external}
        assert len(claimed_ext_ids) + len(residual_ext_ids) == len(ext_records)

    def test_claimed_and_residual_disjoint(self, det_output):
        claimed_ext_ids = {m.external_id for m in det_output.matched}
        residual_ext_ids = {e.ext_id for e in det_output.residual_external}
        assert claimed_ext_ids.isdisjoint(residual_ext_ids)

    def test_all_matches_have_rule_path(self, det_output):
        assert all(m.match_path.value == "rule" for m in det_output.matched)

    def test_all_matches_have_rule_name(self, det_output):
        assert all(m.rule_name for m in det_output.matched)

    def test_all_matches_have_timestamp(self, det_output):
        assert all(m.timestamp for m in det_output.matched)

    def test_all_matches_have_reasoning(self, det_output):
        assert all(m.reasoning for m in det_output.matched)

    def test_amount_date_unique_confidence_below_1(self, det_output):
        adu_matches = [m for m in det_output.matched if m.rule_name == "amount_date_unique"]
        assert all(m.confidence < 1.0 for m in adu_matches), (
            f"confidences: {[m.confidence for m in adu_matches]}"
        )


# ── Ground Truth Validation ─────────────────────────────────

class TestGroundTruthValidation:
    def test_zero_false_positives(self, det_output, ground_truth):
        gt_lookup = {m["internal_id"]: m["external_id"] for m in ground_truth["matches"]}
        gt_noise = {m["internal_id"]: m["noise_type"] for m in ground_truth["matches"]}

        false_pos = 0
        wrong_match = []

        for m in det_output.matched:
            expected_ext = gt_lookup.get(m.internal_id)
            if expected_ext and m.external_id == expected_ext:
                pass  # true positive
            elif expected_ext and m.external_id != expected_ext:
                false_pos += 1
                wrong_match.append(
                    f"{m.internal_id} matched to {m.external_id} "
                    f"(expected {expected_ext}, noise={gt_noise.get(m.internal_id)})"
                )
            else:
                false_pos += 1
                wrong_match.append(
                    f"{m.internal_id} matched to {m.external_id} "
                    f"(no ground-truth match expected -- unmatched internal)"
                )

        assert false_pos == 0, "\n".join(wrong_match)


# ── Adversarial: Hungarian beats greedy ─────────────────────

class TestHungarianBeatsGreedy:
    """
    Prove that the optimal assignment (Hungarian) avoids the starvation
    bug that a greedy first-match-wins approach would produce.

    Setup (all in Rule 3 — ref_fee_tolerance tier):
      INT_A: amount=1005, ref=REF_SHARED
      INT_B: amount=1020, ref=REF_SHARED  ← listed FIRST (adversarial ordering)
      EXT_X: amount=1000, ref=REF_SHARED
      EXT_Y: amount=1050, ref=REF_SHARED

    Tolerance checks (3%):
      INT_A→EXT_X: |1005-1000|/1005 = 0.50% ✓
      INT_A→EXT_Y: |1005-1050|/1005 = 4.48% ✗
      INT_B→EXT_X: |1020-1000|/1020 = 1.96% ✓
      INT_B→EXT_Y: |1020-1050|/1020 = 2.94% ✓

    INT_A can match EXT_X only. INT_B can match EXT_X or EXT_Y.
    Greedy (INT_B first): INT_B→EXT_X (1.96%), INT_A→nothing. 1 match.
    Hungarian:            INT_A→EXT_X (0.50%), INT_B→EXT_Y (2.94%). 2 matches.
    """

    def _make_adversarial_data(self):
        """Build the adversarial test data shared by both tests."""
        from src.schemas import InternalTransaction, ExternalTransaction

        internals = [
            # INT_B listed FIRST — triggers greedy starvation
            InternalTransaction(
                txn_id="INT_B", reference_id="REF_SHARED",
                amount=Decimal("1020.00"), currency="INR",
                txn_type="payment", date="2026-08-01",
                merchant_name="TestCorp", merchant_category="test",
                payment_method="UPI", status="captured",
            ),
            # INT_A listed second
            InternalTransaction(
                txn_id="INT_A", reference_id="REF_SHARED",
                amount=Decimal("1005.00"), currency="INR",
                txn_type="payment", date="2026-08-01",
                merchant_name="TestCorp", merchant_category="test",
                payment_method="UPI", status="captured",
            ),
        ]

        externals = [
            ExternalTransaction(
                ext_id="EXT_X", reference_id="REF_SHARED",
                amount=Decimal("1000.00"), date="2026-08-01",
                description="Test X", source_format="A",
            ),
            ExternalTransaction(
                ext_id="EXT_Y", reference_id="REF_SHARED",
                amount=Decimal("1050.00"), date="2026-08-01",
                description="Test Y", source_format="A",
            ),
        ]

        return internals, externals

    def test_optimal_beats_greedy_on_starvation(self):
        """
        Hungarian finds 2 matches (INT_A→EXT_X, INT_B→EXT_Y) where
        greedy would find only 1 (INT_B→EXT_X, INT_A starved).
        """
        internals, externals = self._make_adversarial_data()

        output = run_deterministic_matching(internals, externals)

        matched_pairs = {
            (m.internal_id, m.external_id) for m in output.matched
        }

        # Hungarian should find 2 matches, not 1
        assert len(output.matched) == 2, (
            f"Expected 2 matches (Hungarian optimal), got {len(output.matched)}: "
            f"{matched_pairs}"
        )
        assert ("INT_A", "EXT_X") in matched_pairs, (
            f"INT_A should match EXT_X (its only option), got {matched_pairs}"
        )
        assert ("INT_B", "EXT_Y") in matched_pairs, (
            f"INT_B should match EXT_Y (freeing EXT_X for INT_A), got {matched_pairs}"
        )
        assert len(output.residual_internal) == 0

    def test_greedy_would_fail_on_adversarial_ordering(self):
        """
        Prove that a naive greedy approach WOULD produce only 1 match
        on the same adversarial input, confirming Hungarian is necessary.
        Simulates greedy by manually iterating and claiming with _try_rule_3.
        """
        from src.deterministic_matcher import _ExternalIndex, _try_rule_3

        internals, externals = self._make_adversarial_data()

        ext_index = _ExternalIndex.build(externals)
        claimed = set()
        greedy_matched = []

        for txn in internals:
            result = _try_rule_3(
                txn, ext_index, claimed,
                fee_tolerance_pct=Decimal("3"),
                date_window=3,
            )
            if result:
                greedy_matched.append(result)
                claimed.add(result.external_id)

        # Greedy: INT_B grabs EXT_X first (1.96%), starving INT_A
        assert len(greedy_matched) == 1, (
            f"Expected greedy to produce only 1 match (starvation), "
            f"got {len(greedy_matched)}"
        )
        assert greedy_matched[0].internal_id == "INT_B"
        assert greedy_matched[0].external_id == "EXT_X"

    def test_oversubscribed_tier_no_forced_match(self):
        """
        Exercises the BIG-cost filter (line 324 of deterministic_matcher.py).

        3 internals, 3 externals — all in Rule 3 tier. The valid edges
        DON'T form a perfect matching (Hall's condition violated), so
        linear_sum_assignment on the 3×3 cost matrix MUST assign one
        row to a BIG cell. Without the `if cost[i,j] < BIG` filter,
        that forced pair becomes a false-positive match.

        Valid edges (Rule 3: ref + fee tolerance ≤3% + date window):
          INT_A (1005) → EXT_X (1000): |1005-1000|/1005 = 0.50% ✓
          INT_A (1005) → EXT_Y (2000): 99%  ✗
          INT_A (1005) → EXT_Z (2050): 104% ✗
          INT_B (1020) → EXT_X (1000): |1020-1000|/1020 = 1.96% ✓
          INT_B (1020) → EXT_Y (2000): 96%  ✗
          INT_B (1020) → EXT_Z (2050): 101% ✗
          INT_C (2010) → EXT_X (1000): 50%  ✗
          INT_C (2010) → EXT_Y (2000): |2010-2000|/2010 = 0.50% ✓
          INT_C (2010) → EXT_Z (2050): |2010-2050|/2010 = 1.99% ✓

        Cost matrix (3×3):
                    EXT_X   EXT_Y   EXT_Z
          INT_A  [  0.00    BIG     BIG  ]
          INT_B  [  0.00    BIG     BIG  ]
          INT_C  [  BIG     0.00    0.00 ]

        Hall's violation: S={INT_A, INT_B} → neighbors={EXT_X}, |{EXT_X}|=1 < 2.
        No valid perfect matching exists. linear_sum_assignment returns 3 pairs;
        one is forced onto a BIG cell.

        With filter:    2 matches (BIG pair dropped). Correct.
        Without filter: 3 matches (false positive). Bug.
        """
        from src.schemas import InternalTransaction, ExternalTransaction

        internals = [
            InternalTransaction(
                txn_id="INT_A", reference_id="REF_SHARED",
                amount=Decimal("1005.00"), currency="INR",
                txn_type="payment", date="2026-08-01",
                merchant_name="TestCorp", merchant_category="test",
                payment_method="UPI", status="captured",
            ),
            InternalTransaction(
                txn_id="INT_B", reference_id="REF_SHARED",
                amount=Decimal("1020.00"), currency="INR",
                txn_type="payment", date="2026-08-01",
                merchant_name="TestCorp", merchant_category="test",
                payment_method="UPI", status="captured",
            ),
            InternalTransaction(
                txn_id="INT_C", reference_id="REF_SHARED",
                amount=Decimal("2010.00"), currency="INR",
                txn_type="payment", date="2026-08-01",
                merchant_name="TestCorp", merchant_category="test",
                payment_method="UPI", status="captured",
            ),
        ]

        externals = [
            ExternalTransaction(
                ext_id="EXT_X", reference_id="REF_SHARED",
                amount=Decimal("1000.00"), date="2026-08-01",
                description="Test X", source_format="A",
            ),
            ExternalTransaction(
                ext_id="EXT_Y", reference_id="REF_SHARED",
                amount=Decimal("2000.00"), date="2026-08-01",
                description="Test Y", source_format="A",
            ),
            ExternalTransaction(
                ext_id="EXT_Z", reference_id="REF_SHARED",
                amount=Decimal("2050.00"), date="2026-08-01",
                description="Test Z", source_format="A",
            ),
        ]

        output = run_deterministic_matching(internals, externals)

        matched_pairs = {
            (m.internal_id, m.external_id) for m in output.matched
        }
        residual_ids = {t.txn_id for t in output.residual_internal}

        # Exactly 2 matches — the BIG-cell pair must be filtered out
        assert len(output.matched) == 2, (
            f"Expected 2 matches (Hall's condition violated, no valid "
            f"perfect matching), got {len(output.matched)}: {matched_pairs}"
        )

        # One of INT_A/INT_B gets EXT_X (the only valid external for both)
        ext_x_matches = [m for m in output.matched if m.external_id == "EXT_X"]
        assert len(ext_x_matches) == 1
        assert ext_x_matches[0].internal_id in ("INT_A", "INT_B")

        # INT_C gets EXT_Y or EXT_Z (both valid for it)
        int_c_matches = [m for m in output.matched if m.internal_id == "INT_C"]
        assert len(int_c_matches) == 1
        assert int_c_matches[0].external_id in ("EXT_Y", "EXT_Z")

        # The other of INT_A/INT_B is residual (not force-matched)
        assert len(output.residual_internal) == 1, (
            f"Expected 1 residual internal, got {len(output.residual_internal)}: "
            f"{residual_ids}"
        )
        loser = {"INT_A", "INT_B"} - {ext_x_matches[0].internal_id}
        assert loser.issubset(residual_ids)




# ── Legacy script mode ───────────────────────────────────────

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
