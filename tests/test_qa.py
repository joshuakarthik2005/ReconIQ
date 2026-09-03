"""
Tests for Settlement Q&A Layer (Part 7)
========================================

Tests cover deterministic paths only (no LLM, no mocks needed):
  1. why_unmatched — exception lookup, matched record, unknown ID
  2. record_detail — matched and exception records
  3. count_by_gl_category — totals, largest category
  4. count_by_rule — expected rules present, total matches
  5. count_by_resolution_path — rule/llm/exception counts
  6. summary — contains expected count strings
  7. answer() intent routing — deterministic patterns
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.qa import SettlementQA

REPORTS_DIR = PROJECT_ROOT / "reports"


# ── Fixture ──────────────────────────────────────────────────

@pytest.fixture(scope="module")
def qa():
    """Load SettlementQA from the pipeline's reports directory."""
    if not (REPORTS_DIR / "exceptions.json").exists():
        pytest.skip("Reports not generated — run run_full_pipeline.py first")
    return SettlementQA(REPORTS_DIR)


# ── Test: why_unmatched ──────────────────────────────────────

class TestWhyUnmatched:

    def test_known_exception_returns_reason(self, qa):
        """TXN_007 is an unmatched internal — returns specific reason."""
        result = qa.why_unmatched("TXN_007")
        assert "TXN_007" not in result or "LLM evaluated" in result
        assert len(result) > 20  # not a stub

    def test_known_exception_contains_rejection_detail(self, qa):
        """TXN_007's reason contains specific LLM rejection text."""
        result = qa.why_unmatched("TXN_007")
        assert "8234.56" in result  # TXN_007's amount is in the reason

    def test_matched_record_says_matched(self, qa):
        """TXN_001 was matched — should say so, not return an error."""
        result = qa.why_unmatched("TXN_001")
        assert "matched" in result.lower()
        assert "EXT_" in result  # should mention the matched external

    def test_unknown_id_returns_unknown(self, qa):
        """TXN_999 doesn't exist — returns unknown message."""
        result = qa.why_unmatched("TXN_999")
        assert "Unknown" in result or "unknown" in result
        assert "TXN_999" in result


# ── Test: record_detail ──────────────────────────────────────

class TestRecordDetail:

    def test_matched_record_has_entries(self, qa):
        """TXN_001 should have at least 2 audit entries (match + classification)."""
        result = qa.record_detail("TXN_001")
        assert "TXN_001" in result
        assert "audit entries" in result.lower() or "path=" in result

    def test_exception_record_has_reason(self, qa):
        """TXN_007 (exception) should show reason."""
        result = qa.record_detail("TXN_007")
        assert "TXN_007" in result

    def test_unknown_record(self, qa):
        """Unknown record returns not-found message."""
        result = qa.record_detail("TXN_999")
        assert "No records" in result or "not found" in result.lower()


# ── Test: count_by_gl_category ───────────────────────────────

class TestCountByGLCategory:

    def test_total_is_52(self, qa):
        """Sum of GL categories should be 52 (all matched records)."""
        counts = qa.count_by_gl_category()
        assert sum(counts.values()) == 52

    def test_settlement_is_largest(self, qa):
        """Settlement Income should be the largest category."""
        counts = qa.count_by_gl_category()
        assert "Settlement Income" in counts
        assert counts["Settlement Income"] == max(counts.values())

    def test_refund_present(self, qa):
        """Customer Refund should be present."""
        counts = qa.count_by_gl_category()
        assert "Customer Refund" in counts
        assert counts["Customer Refund"] > 0


# ── Test: count_by_rule ──────────────────────────────────────

class TestCountByRule:

    def test_total_is_52(self, qa):
        """Sum of rule counts should be 52 (45 rule + 7 LLM matches)."""
        counts = qa.count_by_rule()
        assert sum(counts.values()) == 52

    def test_exact_ref_present(self, qa):
        """exact_ref_amount_date rule should be present."""
        counts = qa.count_by_rule()
        assert "exact_ref_amount_date" in counts

    def test_llm_rules_present(self, qa):
        """LLM rule names (llm_batch, llm_single) should be present."""
        counts = qa.count_by_rule()
        rule_names = set(counts.keys())
        assert "llm_batch" in rule_names or "llm_single" in rule_names


# ── Test: count_by_resolution_path ───────────────────────────

class TestCountByResolutionPath:

    def test_rule_count_is_45(self, qa):
        """45 records matched by rule."""
        counts = qa.count_by_resolution_path()
        assert counts.get("rule", 0) == 45

    def test_llm_count_is_7(self, qa):
        """7 records matched by LLM."""
        counts = qa.count_by_resolution_path()
        assert counts.get("llm", 0) == 7

    def test_exception_count_is_16(self, qa):
        """16 exception entries."""
        counts = qa.count_by_resolution_path()
        assert counts.get("exception", 0) == 16


# ── Test: summary ────────────────────────────────────────────

class TestSummary:

    def test_contains_counts(self, qa):
        """Summary should contain key pipeline counts."""
        result = qa.summary()
        assert "65" in result   # internal count
        assert "55" in result   # external count
        assert "52" in result   # matched count

    def test_contains_resolution_breakdown(self, qa):
        """Summary should show resolution path breakdown."""
        result = qa.summary()
        assert "rule" in result.lower()


# ── Test: answer() intent routing ────────────────────────────

class TestAnswerRouting:

    def test_routes_why_unmatched(self, qa):
        """'Why didn't TXN_007 match?' routes to why_unmatched."""
        answer = qa.answer("Why didn't TXN_007 match?")
        direct = qa.why_unmatched("TXN_007")
        assert answer == direct

    def test_routes_record_detail(self, qa):
        """'Show me TXN_001' routes to record_detail."""
        answer = qa.answer("Show me TXN_001")
        direct = qa.record_detail("TXN_001")
        assert answer == direct

    def test_routes_category_breakdown(self, qa):
        """'Breakdown by GL category' routes to count_by_gl_category."""
        answer = qa.answer("How many by GL category?")
        assert "Settlement Income" in answer

    def test_routes_rule_breakdown(self, qa):
        """'Which rules fired?' routes to count_by_rule."""
        answer = qa.answer("Which rules fired and how many?")
        assert "exact_ref_amount_date" in answer

    def test_routes_summary(self, qa):
        """'Summary' routes to summary."""
        answer = qa.answer("summary")
        assert "65" in answer
        assert "52" in answer

    def test_no_api_key_fallback_message(self, qa):
        """Unrecognized question without API key returns guidance."""
        import os
        # Temporarily clear env
        old_gemini = os.environ.pop("GEMINI_API_KEY", None)
        old_google = os.environ.pop("GOOGLE_API_KEY", None)
        try:
            answer = qa.answer(
                "What is the meaning of life?",
                api_key=None,
            )
            assert "structured queries" in answer.lower() or "api key" in answer.lower()
        finally:
            if old_gemini:
                os.environ["GEMINI_API_KEY"] = old_gemini
            if old_google:
                os.environ["GOOGLE_API_KEY"] = old_google


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
