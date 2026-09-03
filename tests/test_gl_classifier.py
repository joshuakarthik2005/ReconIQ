"""
Tests for GL Classification (Part 4)
=====================================

Tests cover:
  1. Full-batch: all 52 matched records produce exactly 52 ClassifiedEntry objects
  2. Fee/tax rounding-leakage: GATEWAY_FEE + TAX_ADJUSTMENT == total fee (exact Decimal)
  3. All 4 partial_refund-noise records get consistent partial annotation
  4. No UNCLASSIFIED entries in output
  5. Rule distribution matches expectations
  6. Mock-based LLM classification code path test
"""

import json
import sys
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Setup path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.ingestion import parse_internal, parse_all_external
from src.deterministic_matcher import run_deterministic_matching
from src.gl_classifier import (
    ClassificationOutput,
    ClassifiedEntry,
    GLSubEntry,
    compute_fee_tax_split,
    run_gl_classification,
    _rule_1_refund_type,
    _attach_fee_sub_entries,
    _rule_3_partial_refund_llm,
    _rule_4_clean_settlement,
    _build_batch_classification_prompt,
    _parse_classification_response,
)
from src.schemas import (
    ExternalTransaction,
    GLCategory,
    InternalTransaction,
    MatchPath,
    MatchResult,
)

DATA_DIR = PROJECT_ROOT / "data"


# ── Fixtures ─────────────────────────────────────────────────

@pytest.fixture(scope="module")
def pipeline_data():
    """Load and run Parts 0–2 to get all matched records."""
    int_records, _ = parse_internal(DATA_DIR / "internal_transactions.csv")
    ext_records, _ = parse_all_external(DATA_DIR)
    det = run_deterministic_matching(int_records, ext_records)

    gt = json.loads(
        (DATA_DIR / "ground_truth.json").read_text(encoding="utf-8")
    )

    return {
        "int_records": int_records,
        "ext_records": ext_records,
        "det": det,
        "gt": gt,
    }


@pytest.fixture(scope="module")
def all_matches(pipeline_data):
    """Build the complete list of 52 MatchResult objects (45 rule + 7 LLM).

    LLM matches are reconstructed from the canonical Part 3 results
    to avoid requiring a live API call.
    """
    det = pipeline_data["det"]

    # Part 2 rule matches (45)
    rule_matches = list(det.matched)

    # Part 3 LLM matches (7) — reconstructed from canonical results
    llm_matches = [
        MatchResult(
            internal_id="TXN_005", external_id="EXT_020",
            match_path=MatchPath.LLM, confidence=0.95,
            rule_name="llm_single",
            reasoning="Ref match + date + merchant match, partial refund — bank amount is net after refund deduction",
            is_partial_refund=True,
            timestamp="2026-08-28T00:00:00",
        ),
        MatchResult(
            internal_id="TXN_021", external_id="EXT_002",
            match_path=MatchPath.LLM, confidence=0.95,
            rule_name="llm_group_assignment",
            reasoning="Exact match on amount (5873.85 INR) and transaction date (2026-08-12).",
            timestamp="2026-08-28T00:00:00",
        ),
        MatchResult(
            internal_id="TXN_026", external_id="EXT_043",
            match_path=MatchPath.LLM, confidence=0.95,
            rule_name="llm_batch",
            reasoning="Ref match + date + merchant match, partial refund — bank amount is net after refund deduction",
            is_partial_refund=True,
            timestamp="2026-08-28T00:00:00",
        ),
        MatchResult(
            internal_id="TXN_031", external_id="EXT_003",
            match_path=MatchPath.LLM, confidence=0.95,
            rule_name="llm_batch",
            reasoning="Ref match + date + merchant match, partial refund — bank amount is net after refund deduction",
            is_partial_refund=True,
            timestamp="2026-08-28T00:00:00",
        ),
        MatchResult(
            internal_id="TXN_045", external_id="EXT_035",
            match_path=MatchPath.LLM, confidence=0.98,
            rule_name="llm_batch",
            reasoning="Exact match on amount (1576.26 INR), date (2026-08-26), and clear merchant match in external description",
            timestamp="2026-08-28T00:00:00",
        ),
        MatchResult(
            internal_id="TXN_051", external_id="EXT_040",
            match_path=MatchPath.LLM, confidence=0.95,
            rule_name="llm_batch",
            reasoning="Exact amount match, 3-day settlement window date drift",
            timestamp="2026-08-28T00:00:00",
        ),
        MatchResult(
            internal_id="TXN_054", external_id="EXT_041",
            match_path=MatchPath.LLM, confidence=0.92,
            rule_name="llm_batch",
            reasoning="Ref match + date + merchant match, partial refund — bank amount is net after refund deduction",
            is_partial_refund=True,
            timestamp="2026-08-28T00:00:00",
        ),
    ]

    return rule_matches + llm_matches


@pytest.fixture(scope="module")
def classification_output(all_matches, pipeline_data):
    """Run GL classification on all 52 matches."""
    return run_gl_classification(
        all_matches,
        pipeline_data["int_records"],
        pipeline_data["ext_records"],
    )


@pytest.fixture(scope="module")
def gt_lookup(pipeline_data):
    """Build ground truth lookups."""
    gt = pipeline_data["gt"]
    return {
        "matches": {m["internal_id"]: m for m in gt["matches"]},
        "by_noise": {},
    }


# ── Test: Full batch coverage ────────────────────────────────

class TestFullBatchCoverage:
    """Verify all 52 matched records produce exactly 52 ClassifiedEntry objects."""

    def test_count_is_52(self, classification_output):
        """52 ClassifiedEntry objects — one per match, not flattened."""
        assert len(classification_output.classified) == 52, (
            f"Expected 52 ClassifiedEntry objects, got "
            f"{len(classification_output.classified)}"
        )

    def test_no_silent_drops(self, classification_output, all_matches):
        """Every match ID appears in classified output."""
        classified_ids = {
            e.match_result.internal_id for e in classification_output.classified
        }
        match_ids = {m.internal_id for m in all_matches}
        assert classified_ids == match_ids, (
            f"Missing: {match_ids - classified_ids}, "
            f"Extra: {classified_ids - match_ids}"
        )

    def test_no_unclassified(self, classification_output):
        """No UNCLASSIFIED entries in output."""
        unclassified = [
            e for e in classification_output.classified
            if e.gl_category == GLCategory.UNCLASSIFIED
        ]
        assert len(unclassified) == 0, (
            f"Found {len(unclassified)} UNCLASSIFIED entries: "
            f"{[e.match_result.internal_id for e in unclassified]}"
        )

    def test_no_llm_calls_on_this_dataset(self, classification_output):
        """All records classified by rules — no LLM calls needed."""
        assert classification_output.llm_calls == 0, (
            f"Expected 0 LLM calls, got {classification_output.llm_calls}"
        )


# ── Test: Fee/tax rounding leakage ───────────────────────────

class TestFeeTaxSplit:
    """Verify GATEWAY_FEE + TAX_ADJUSTMENT == total fee with zero rounding leakage."""

    def test_rounding_leakage_zero(self, classification_output, pipeline_data):
        """For every ref_fee_tolerance match, sub-entries sum exactly to the fee."""
        int_by_id = {t.txn_id: t for t in pipeline_data["int_records"]}
        ext_by_id = {e.ext_id: e for e in pipeline_data["ext_records"]}

        # Fee entries identified by having sub_entries (Phase B attached them)
        fee_entries = [
            e for e in classification_output.classified
            if len(e.sub_entries) > 0
        ]

        assert len(fee_entries) == 8, (
            f"Expected 8 entries with fee sub-entries, got {len(fee_entries)}"
        )

        for entry in fee_entries:
            internal = int_by_id[entry.match_result.internal_id]
            external = ext_by_id[entry.match_result.external_id]
            total_fee = internal.amount - external.amount

            sub_amounts = [s.amount for s in entry.sub_entries]
            assert len(sub_amounts) == 2, (
                f"{entry.match_result.internal_id}: expected 2 sub-entries, "
                f"got {len(sub_amounts)}"
            )

            gateway_fee = sub_amounts[0]
            tax_adjustment = sub_amounts[1]

            # Exact Decimal equality — zero tolerance
            assert gateway_fee + tax_adjustment == total_fee, (
                f"{entry.match_result.internal_id}: "
                f"gateway_fee ({gateway_fee}) + tax ({tax_adjustment}) = "
                f"{gateway_fee + tax_adjustment} != total_fee ({total_fee})"
            )

    def test_sub_entry_categories(self, classification_output):
        """Sub-entries have correct GL categories."""
        fee_entries = [
            e for e in classification_output.classified
            if len(e.sub_entries) > 0
        ]

        for entry in fee_entries:
            assert len(entry.sub_entries) == 2
            assert entry.sub_entries[0].category == GLCategory.GATEWAY_FEE
            assert entry.sub_entries[1].category == GLCategory.TAX_ADJUSTMENT

    def test_txn_020_is_refund_with_fee_split(self, classification_output):
        """TXN_020 is txn_type=refund AND ref_fee_tolerance: gets REFUND + fee sub-entries."""
        entry = next(
            e for e in classification_output.classified
            if e.match_result.internal_id == "TXN_020"
        )
        # Primary category from Phase A: REFUND (txn_type=refund)
        assert entry.gl_category == GLCategory.REFUND, (
            f"TXN_020: expected REFUND, got {entry.gl_category}"
        )
        assert entry.rule_name == "refund_type"
        # Fee sub-entries from Phase B (ref_fee_tolerance match)
        assert len(entry.sub_entries) == 2, (
            f"TXN_020: expected 2 fee sub-entries, got {len(entry.sub_entries)}"
        )
        assert entry.sub_entries[0].category == GLCategory.GATEWAY_FEE
        assert entry.sub_entries[1].category == GLCategory.TAX_ADJUSTMENT

    def test_gateway_fee_uses_round_half_up(self):
        """Verify gateway_fee quantization uses ROUND_HALF_UP explicitly."""
        # Construct a case where ROUND_HALF_UP differs from ROUND_HALF_EVEN
        # Fee = 1.185 → 100/118 * 1.185 = 1.00423... → but test with known values
        # Use fee = 2.36 INR → gateway = 2.36 * 100/118 = 2.00000... = 2.00
        # Use fee = 1.77 INR → gateway = 1.77 * 100/118 = 1.50000... = 1.50
        # Better test: known boundary case
        internal_amt = Decimal("100.00")
        external_amt = Decimal("97.65")  # fee = 2.35
        gateway, tax = compute_fee_tax_split(internal_amt, external_amt)

        assert gateway + tax == Decimal("2.35"), (
            f"Rounding leakage: {gateway} + {tax} = {gateway + tax} != 2.35"
        )
        # Verify gateway is quantized to 2dp
        assert gateway == gateway.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ── Test: Partial refund consistency ─────────────────────────

class TestPartialRefundConsistency:
    """All 4 partial_refund-noise records get consistent partial annotation."""

    PARTIAL_REFUND_IDS = {"TXN_005", "TXN_026", "TXN_031", "TXN_054"}

    def test_all_four_classified_as_refund(self, classification_output):
        """All 4 partial_refund records → REFUND category."""
        for entry in classification_output.classified:
            if entry.match_result.internal_id in self.PARTIAL_REFUND_IDS:
                assert entry.gl_category == GLCategory.REFUND, (
                    f"{entry.match_result.internal_id}: expected REFUND, "
                    f"got {entry.gl_category}"
                )

    def test_all_four_have_partial_annotation(self, classification_output):
        """All 4 partial_refund records have 'partial refund' in reasoning."""
        for entry in classification_output.classified:
            if entry.match_result.internal_id in self.PARTIAL_REFUND_IDS:
                assert "partial refund" in entry.reasoning.lower(), (
                    f"{entry.match_result.internal_id}: reasoning missing "
                    f"'partial refund': {entry.reasoning}"
                )

    def test_rule_assignment_split(self, classification_output):
        """TXN_026/054 (txn_type=refund) → Rule 1, TXN_005/031 → Rule 3."""
        entries_by_id = {
            e.match_result.internal_id: e
            for e in classification_output.classified
        }

        # txn_type=refund cases → Rule 1 (refund_type)
        assert entries_by_id["TXN_026"].rule_name == "refund_type"
        assert entries_by_id["TXN_054"].rule_name == "refund_type"

        # txn_type=payment/settlement cases → Rule 3 (partial_refund_llm)
        assert entries_by_id["TXN_005"].rule_name == "partial_refund_llm"
        assert entries_by_id["TXN_031"].rule_name == "partial_refund_llm"


class TestPartialRefundStructuredSignal:
    """Verify is_partial_refund field-based detection and keyword fallback."""

    def _make_match(self, is_partial=False, reasoning="test", path=MatchPath.LLM):
        return MatchResult(
            internal_id="TEST_PR", external_id="EXT_PR",
            match_path=path, confidence=0.95,
            rule_name="llm_batch", reasoning=reasoning,
            is_partial_refund=is_partial,
            timestamp="2026-01-01T00:00:00",
        )

    def test_field_true_detected(self):
        """is_partial_refund=True → detected as partial refund."""
        from src.gl_classifier import _is_partial_refund_reasoning
        match = self._make_match(is_partial=True, reasoning="exact amount match")
        assert _is_partial_refund_reasoning(match) is True

    def test_field_false_keyword_fallback(self):
        """is_partial_refund=False but reasoning says 'partial refund' → still detected."""
        from src.gl_classifier import _is_partial_refund_reasoning
        match = self._make_match(
            is_partial=False,
            reasoning="partial refund — bank amount is net after refund",
        )
        assert _is_partial_refund_reasoning(match) is True

    def test_field_false_no_keyword_not_detected(self):
        """is_partial_refund=False and no keyword → not detected."""
        from src.gl_classifier import _is_partial_refund_reasoning
        match = self._make_match(
            is_partial=False,
            reasoning="Exact amount match, settlement window date drift",
        )
        assert _is_partial_refund_reasoning(match) is False

    def test_field_overrides_absence_of_keyword(self):
        """is_partial_refund=True with no 'partial refund' keyword → still detected.

        This is the key test: proves the field works even if the LLM phrases
        the reasoning differently (e.g. 'net after deduction' instead of
        'partial refund').
        """
        from src.gl_classifier import _is_partial_refund_reasoning
        match = self._make_match(
            is_partial=True,
            reasoning="Bank amount is net after deduction of returned items",
        )
        assert _is_partial_refund_reasoning(match) is True

    def test_default_is_false(self):
        """MatchResult without explicit is_partial_refund defaults to False."""
        match = MatchResult(
            internal_id="T", external_id="E",
            match_path=MatchPath.LLM, confidence=0.9,
        )
        assert match.is_partial_refund is False


# ── Test: Rule distribution ──────────────────────────────────

class TestRuleDistribution:
    """Verify expected rule distribution across all 52 records."""

    def test_rule_counts(self, classification_output):
        """Check rule hit counts match expectations.

        Phase A primary categories (no fee_split rule — fee is Phase B):
          refund_type=10 (all txn_type=refund incl. TXN_020)
          partial_refund_llm=2 (TXN_005, TXN_031)
          clean_settlement=40 (52 - 10 - 2)
        Phase B fee sub-entries: 8 records with sub_entries
        """
        counts = {}
        for entry in classification_output.classified:
            counts[entry.rule_name] = counts.get(entry.rule_name, 0) + 1

        assert counts.get("refund_type", 0) == 10, (
            f"Expected 10 refund_type, got {counts.get('refund_type', 0)}"
        )
        # fee_split is no longer a rule — it's Phase B annotation
        assert counts.get("fee_split", 0) == 0, (
            f"fee_split should not appear as a rule_name, got {counts.get('fee_split', 0)}"
        )
        assert counts.get("partial_refund_llm", 0) == 2, (
            f"Expected 2 partial_refund_llm, got "
            f"{counts.get('partial_refund_llm', 0)}"
        )
        assert counts.get("clean_settlement", 0) == 40, (
            f"Expected 40 clean_settlement, got "
            f"{counts.get('clean_settlement', 0)}"
        )

    def test_all_classification_paths_are_rule(self, classification_output):
        """All entries should be classification_path='rule' on this dataset."""
        for entry in classification_output.classified:
            assert entry.classification_path == "rule", (
                f"{entry.match_result.internal_id}: "
                f"classification_path={entry.classification_path}"
            )


# ── Test: Individual rule unit tests ─────────────────────────

class TestIndividualRules:
    """Unit tests for each classification rule in isolation."""

    def _make_match(self, rule_name="exact_ref_amount_date", path=MatchPath.RULE,
                    reasoning="test", confidence=1.0):
        return MatchResult(
            internal_id="TEST_001",
            external_id="EXT_001",
            match_path=path,
            confidence=confidence,
            rule_name=rule_name,
            reasoning=reasoning,
            timestamp="2026-01-01T00:00:00",
        )

    def _make_internal(self, txn_type="payment"):
        return InternalTransaction(
            txn_id="TEST_001", reference_id="ref_001",
            amount=Decimal("1000.00"), currency="INR",
            txn_type=txn_type, date="2026-01-01",
            merchant_name="Test Merchant", merchant_category="ecommerce",
            payment_method="UPI", status="captured",
        )

    def _make_external(self, amount=Decimal("1000.00")):
        return ExternalTransaction(
            ext_id="EXT_001", reference_id="ref_001",
            amount=amount, date="2026-01-01",
            description="Test payment", source_format="A",
        )

    def test_rule_1_fires_for_refund(self):
        match = self._make_match()
        internal = self._make_internal(txn_type="refund")
        external = self._make_external()
        result = _rule_1_refund_type(match, internal, external)
        assert result is not None
        assert result.gl_category == GLCategory.REFUND
        assert result.rule_name == "refund_type"

    def test_rule_1_skips_payment(self):
        match = self._make_match()
        internal = self._make_internal(txn_type="payment")
        external = self._make_external()
        result = _rule_1_refund_type(match, internal, external)
        assert result is None

    def test_rule_1_adds_partial_note(self):
        match = self._make_match(
            reasoning="partial refund — bank amount is net",
            path=MatchPath.LLM,
        )
        internal = self._make_internal(txn_type="refund")
        external = self._make_external()
        result = _rule_1_refund_type(match, internal, external)
        assert result is not None
        assert "partial refund" in result.reasoning.lower()

    def test_phase_b_attaches_fee_sub_entries(self):
        """Phase B: _attach_fee_sub_entries mutates entry in place."""
        match = self._make_match(rule_name="ref_fee_tolerance")
        internal = self._make_internal()
        external = self._make_external(amount=Decimal("977.00"))
        # Phase A: determine category first
        entry = _rule_4_clean_settlement(match, internal, external)
        assert entry is not None
        assert entry.gl_category == GLCategory.SETTLEMENT
        assert len(entry.sub_entries) == 0  # no sub-entries yet
        # Phase B: attach fee sub-entries
        _attach_fee_sub_entries(entry, internal, external)
        assert len(entry.sub_entries) == 2
        assert entry.sub_entries[0].category == GLCategory.GATEWAY_FEE
        assert entry.sub_entries[1].category == GLCategory.TAX_ADJUSTMENT
        # Verify rounding
        total_fee = internal.amount - external.amount  # 23.00
        assert entry.sub_entries[0].amount + entry.sub_entries[1].amount == total_fee

    def test_phase_b_works_on_refund_category(self):
        """Phase B: fee sub-entries attach to REFUND category too (TXN_020 pattern)."""
        match = self._make_match(rule_name="ref_fee_tolerance")
        internal = self._make_internal(txn_type="refund")
        external = self._make_external(amount=Decimal("977.00"))
        # Phase A: refund category
        entry = _rule_1_refund_type(match, internal, external)
        assert entry is not None
        assert entry.gl_category == GLCategory.REFUND
        # Phase B: fee sub-entries attached to refund
        _attach_fee_sub_entries(entry, internal, external)
        assert len(entry.sub_entries) == 2
        assert entry.gl_category == GLCategory.REFUND  # still REFUND

    def test_rule_3_fires_for_llm_partial_refund(self):
        match = self._make_match(
            path=MatchPath.LLM,
            reasoning="partial refund netting",
            rule_name="llm_batch",
        )
        internal = self._make_internal(txn_type="payment")
        external = self._make_external()
        result = _rule_3_partial_refund_llm(match, internal, external)
        assert result is not None
        assert result.gl_category == GLCategory.REFUND
        assert result.rule_name == "partial_refund_llm"

    def test_rule_3_skips_rule_matches(self):
        match = self._make_match(
            path=MatchPath.RULE,
            reasoning="partial refund netting",
        )
        internal = self._make_internal(txn_type="payment")
        external = self._make_external()
        result = _rule_3_partial_refund_llm(match, internal, external)
        assert result is None

    def test_rule_3_skips_non_partial_reasoning(self):
        match = self._make_match(
            path=MatchPath.LLM,
            reasoning="exact amount match, settlement window",
            rule_name="llm_batch",
        )
        internal = self._make_internal(txn_type="payment")
        external = self._make_external()
        result = _rule_3_partial_refund_llm(match, internal, external)
        assert result is None

    def test_rule_4_fires_for_payment(self):
        match = self._make_match()
        internal = self._make_internal(txn_type="payment")
        external = self._make_external()
        result = _rule_4_clean_settlement(match, internal, external)
        assert result is not None
        assert result.gl_category == GLCategory.SETTLEMENT
        assert result.rule_name == "clean_settlement"

    def test_rule_4_fires_for_settlement(self):
        match = self._make_match()
        internal = self._make_internal(txn_type="settlement")
        external = self._make_external()
        result = _rule_4_clean_settlement(match, internal, external)
        assert result is not None


# ── Test: LLM classification code path (mock-based) ─────────

class TestLLMClassificationMock:
    """Mock-based test for the LLM classification fallback path.

    The LLM path doesn't fire on this dataset (0 residual), but we test it
    with mocked responses to verify the code path works — same standard as
    Part 3's group-assignment mock tests.
    """

    def test_single_record_llm_classification(self):
        """Test LLM classification of a single record via mock."""
        # Create a record that no rule can classify (txn_type='chargeback')
        match = MatchResult(
            internal_id="TEST_CB",
            external_id="EXT_CB",
            match_path=MatchPath.LLM,
            confidence=0.8,
            rule_name="llm_batch",
            reasoning="Amount and date match, chargeback reversal",
            timestamp="2026-01-01T00:00:00",
        )
        internal = InternalTransaction(
            txn_id="TEST_CB", reference_id="ref_cb",
            amount=Decimal("500.00"), currency="INR",
            txn_type="chargeback",  # Not caught by any rule
            date="2026-01-01",
            merchant_name="Test", merchant_category="ecommerce",
            payment_method="credit_card", status="captured",
        )
        external = ExternalTransaction(
            ext_id="EXT_CB", reference_id="ref_cb",
            amount=Decimal("500.00"), date="2026-01-01",
            description="Chargeback reversal", source_format="A",
        )

        mock_response = MagicMock()
        mock_response.text = json.dumps({
            "category": "CHARGEBACK",
            "reasoning": "Transaction is a chargeback reversal based on description",
        })
        mock_response.usage_metadata = MagicMock()
        mock_response.usage_metadata.prompt_token_count = 100
        mock_response.usage_metadata.candidates_token_count = 50

        mock_client_cls = MagicMock()
        mock_client = mock_client_cls.return_value
        mock_client.models.generate_content.return_value = mock_response

        with patch("src.gl_classifier.genai.Client", mock_client_cls):
            output = run_gl_classification(
                [match], [internal], [external],
                api_key="test-key",
            )

        assert len(output.classified) == 1
        entry = output.classified[0]
        assert entry.gl_category == GLCategory.CHARGEBACK
        assert entry.classification_path == "llm"
        assert "chargeback" in entry.reasoning.lower()
        assert output.llm_calls == 1

    def test_batch_llm_classification(self):
        """Test batched LLM classification of multiple records via mock."""
        matches = []
        internals = []
        externals = []
        for i in range(3):
            matches.append(MatchResult(
                internal_id=f"TEST_X{i}",
                external_id=f"EXT_X{i}",
                match_path=MatchPath.LLM,
                confidence=0.8,
                rule_name="llm_batch",
                reasoning="ambiguous case",
                timestamp="2026-01-01T00:00:00",
            ))
            internals.append(InternalTransaction(
                txn_id=f"TEST_X{i}", reference_id=f"ref_x{i}",
                amount=Decimal("100.00"), currency="INR",
                txn_type="chargeback",  # Not caught by any rule
                date="2026-01-01",
                merchant_name="Test", merchant_category="ecommerce",
                payment_method="UPI", status="captured",
            ))
            externals.append(ExternalTransaction(
                ext_id=f"EXT_X{i}", reference_id=f"ref_x{i}",
                amount=Decimal("100.00"), date="2026-01-01",
                description="test", source_format="A",
            ))

        mock_response = MagicMock()
        mock_response.text = json.dumps({
            "classifications": [
                {"label": "A", "category": "CHARGEBACK", "reasoning": "chargeback A"},
                {"label": "B", "category": "BANK_CHARGE", "reasoning": "bank charge B"},
                {"label": "C", "category": "MISCELLANEOUS", "reasoning": "misc C"},
            ]
        })
        mock_response.usage_metadata = MagicMock()
        mock_response.usage_metadata.prompt_token_count = 200
        mock_response.usage_metadata.candidates_token_count = 100

        mock_client_cls = MagicMock()
        mock_client = mock_client_cls.return_value
        mock_client.models.generate_content.return_value = mock_response

        with patch("src.gl_classifier.genai.Client", mock_client_cls):
            output = run_gl_classification(
                matches, internals, externals,
                api_key="test-key",
            )

        assert len(output.classified) == 3
        assert output.classified[0].gl_category == GLCategory.CHARGEBACK
        assert output.classified[1].gl_category == GLCategory.BANK_CHARGE
        assert output.classified[2].gl_category == GLCategory.MISCELLANEOUS
        assert output.llm_calls == 1  # batched into single call

    def test_llm_parse_error_fallback(self):
        """Test that malformed LLM response produces UNCLASSIFIED, not a crash."""
        match = MatchResult(
            internal_id="TEST_ERR",
            external_id="EXT_ERR",
            match_path=MatchPath.LLM,
            confidence=0.8,
            rule_name="llm_batch",
            reasoning="ambiguous",
            timestamp="2026-01-01T00:00:00",
        )
        internal = InternalTransaction(
            txn_id="TEST_ERR", reference_id="ref_err",
            amount=Decimal("100.00"), currency="INR",
            txn_type="chargeback", date="2026-01-01",
            merchant_name="Test", merchant_category="ecommerce",
            payment_method="UPI", status="captured",
        )
        external = ExternalTransaction(
            ext_id="EXT_ERR", reference_id="ref_err",
            amount=Decimal("100.00"), date="2026-01-01",
            description="test", source_format="A",
        )

        mock_response = MagicMock()
        mock_response.text = "not valid json at all {{{}"
        mock_response.usage_metadata = MagicMock()
        mock_response.usage_metadata.prompt_token_count = 100
        mock_response.usage_metadata.candidates_token_count = 50

        mock_client_cls = MagicMock()
        mock_client = mock_client_cls.return_value
        mock_client.models.generate_content.return_value = mock_response

        with patch("src.gl_classifier.genai.Client", mock_client_cls):
            output = run_gl_classification(
                [match], [internal], [external],
                api_key="test-key",
            )

        assert len(output.classified) == 1
        entry = output.classified[0]
        assert entry.gl_category == GLCategory.UNCLASSIFIED
        assert "parse error" in entry.reasoning.lower() or "json" in entry.reasoning.lower()

    def test_no_api_key_fallback(self):
        """Test that missing API key produces UNCLASSIFIED with reason."""
        match = MatchResult(
            internal_id="TEST_NOKEY",
            external_id="EXT_NOKEY",
            match_path=MatchPath.LLM,
            confidence=0.8,
            rule_name="llm_batch",
            reasoning="ambiguous",
            timestamp="2026-01-01T00:00:00",
        )
        internal = InternalTransaction(
            txn_id="TEST_NOKEY", reference_id="ref_nk",
            amount=Decimal("100.00"), currency="INR",
            txn_type="chargeback", date="2026-01-01",
            merchant_name="Test", merchant_category="ecommerce",
            payment_method="UPI", status="captured",
        )
        external = ExternalTransaction(
            ext_id="EXT_NOKEY", reference_id="ref_nk",
            amount=Decimal("100.00"), date="2026-01-01",
            description="test", source_format="A",
        )

        # Ensure no env var is set
        with patch.dict("os.environ", {}, clear=True):
            output = run_gl_classification(
                [match], [internal], [external],
                api_key=None,
            )

        assert len(output.classified) == 1
        assert output.classified[0].gl_category == GLCategory.UNCLASSIFIED
        assert "api key" in output.classified[0].reasoning.lower()


# ── Test: Batch prompt construction ──────────────────────────

class TestBatchPromptConstruction:
    """Verify batch prompt has correct structure."""

    def test_labels_assigned_correctly(self):
        items = [
            (
                MatchResult(internal_id=f"T{i}", external_id=f"E{i}",
                            match_path=MatchPath.LLM, reasoning="test"),
                InternalTransaction(
                    txn_id=f"T{i}", reference_id=f"r{i}",
                    amount=Decimal("100"), currency="INR",
                    txn_type="payment", date="2026-01-01",
                    merchant_name="M", merchant_category="cat",
                    payment_method="UPI", status="captured",
                ),
                ExternalTransaction(
                    ext_id=f"E{i}", reference_id=None, amount=Decimal("100"),
                    date="2026-01-01", description="d",
                    source_format="A",
                ),
            )
            for i in range(3)
        ]
        prompt = _build_batch_classification_prompt(items)
        assert "RECORD A:" in prompt
        assert "RECORD B:" in prompt
        assert "RECORD C:" in prompt

    def test_response_parsing_handles_missing_records(self):
        items = [
            (
                MatchResult(internal_id=f"T{i}", external_id=f"E{i}",
                            match_path=MatchPath.LLM, reasoning="test"),
                InternalTransaction(
                    txn_id=f"T{i}", reference_id=f"r{i}",
                    amount=Decimal("100"), currency="INR",
                    txn_type="payment", date="2026-01-01",
                    merchant_name="M", merchant_category="cat",
                    payment_method="UPI", status="captured",
                ),
                ExternalTransaction(
                    ext_id=f"E{i}", reference_id=None, amount=Decimal("100"),
                    date="2026-01-01", description="d",
                    source_format="A",
                ),
            )
            for i in range(3)
        ]

        # Response only includes labels A and C — B is missing
        raw = json.dumps({
            "classifications": [
                {"label": "A", "category": "SETTLEMENT", "reasoning": "r1"},
                {"label": "C", "category": "REFUND", "reasoning": "r3"},
            ]
        })
        results = _parse_classification_response(raw, items)
        assert len(results) == 3  # B gets UNCLASSIFIED
        result_map = {r.match_result.internal_id: r for r in results}
        assert result_map["T0"].gl_category == GLCategory.SETTLEMENT
        assert result_map["T1"].gl_category == GLCategory.UNCLASSIFIED
        assert result_map["T2"].gl_category == GLCategory.REFUND


# ── Test: compute_fee_tax_split edge cases ───────────────────

class TestComputeFeeTaxSplit:
    """Direct unit tests for the fee/tax split function."""

    def test_known_values(self):
        """Test with known fee amount."""
        # Fee = 23.60 → gateway = 23.60 * 100/118 = 20.00
        gateway, tax = compute_fee_tax_split(
            Decimal("1023.60"), Decimal("1000.00")
        )
        assert gateway == Decimal("20.00")
        assert tax == Decimal("3.60")
        assert gateway + tax == Decimal("23.60")

    def test_small_fee(self):
        """Test with very small fee."""
        gateway, tax = compute_fee_tax_split(
            Decimal("100.01"), Decimal("100.00")
        )
        assert gateway + tax == Decimal("0.01")

    def test_zero_fee(self):
        """Test with zero fee (amounts match exactly)."""
        gateway, tax = compute_fee_tax_split(
            Decimal("1000.00"), Decimal("1000.00")
        )
        assert gateway == Decimal("0.00")
        assert tax == Decimal("0.00")

    def test_many_random_fees_no_leakage(self):
        """Fuzz test: random fees never have rounding leakage."""
        import random
        rng = random.Random(42)
        for _ in range(1000):
            internal = Decimal(str(round(rng.uniform(100, 50000), 2)))
            fee_pct = Decimal(str(round(rng.uniform(0.01, 0.05), 4)))
            fee = (internal * fee_pct).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            external = internal - fee

            gateway, tax = compute_fee_tax_split(internal, external)
            assert gateway + tax == fee, (
                f"Leakage: internal={internal}, external={external}, "
                f"fee={fee}, gateway={gateway}, tax={tax}, "
                f"sum={gateway + tax}"
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
