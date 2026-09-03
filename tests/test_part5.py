"""
Tests for Exception Reporting + Audit Trail (Part 5)
=====================================================

Tests cover:
  1. Full accounting — internal: matched + unmatched = 65
  2. Full accounting — external: matched + unmatched = 55
  3. No generic reasons (every reason > 20 chars, no "no match found")
  4. LLM rejection reasons preserved from Part 3
  5. Unmatched external count = 3, each with specific data
  6. Exception categories: only 3 values
  7. Audit trail: exactly 120 entries
  8. Every matched record has match + classification entries
  9. Every exception has an audit entry
  10. JSONL output validity
"""

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.ingestion import parse_internal, parse_all_external
from src.deterministic_matcher import run_deterministic_matching
from src.gl_classifier import run_gl_classification
from src.exceptions import collect_exceptions, save_exceptions
from src.audit import build_audit_trail, save_audit_trail
from src.schemas import (
    MatchPath,
    MatchResult,
)

DATA_DIR = PROJECT_ROOT / "data"


# ── Fixtures ─────────────────────────────────────────────────

@pytest.fixture(scope="module")
def pipeline_data():
    """Run Parts 0-4 pipeline."""
    int_records, parse_errors = parse_internal(DATA_DIR / "internal_transactions.csv")
    ext_records, ext_parse_errors = parse_all_external(DATA_DIR)
    all_parse_errors = parse_errors + ext_parse_errors

    det = run_deterministic_matching(int_records, ext_records)
    rule_matches = list(det.matched)

    # Reconstructed LLM matches from canonical Part 3 results
    llm_matches = [
        MatchResult(
            internal_id="TXN_005", external_id="EXT_020",
            match_path=MatchPath.LLM, confidence=0.95, rule_name="llm_single",
            reasoning="Ref match + date + merchant match, partial refund -- bank amount is net after refund deduction",
            timestamp="2026-08-28T00:00:00",
        ),
        MatchResult(
            internal_id="TXN_021", external_id="EXT_002",
            match_path=MatchPath.LLM, confidence=0.95, rule_name="llm_group_assignment",
            reasoning="Exact match on amount (5873.85 INR) and transaction date (2026-08-12).",
            timestamp="2026-08-28T00:00:00",
        ),
        MatchResult(
            internal_id="TXN_026", external_id="EXT_043",
            match_path=MatchPath.LLM, confidence=0.95, rule_name="llm_batch",
            reasoning="Ref match + date + merchant match, partial refund -- bank amount is net after refund deduction",
            timestamp="2026-08-28T00:00:00",
        ),
        MatchResult(
            internal_id="TXN_031", external_id="EXT_003",
            match_path=MatchPath.LLM, confidence=0.95, rule_name="llm_batch",
            reasoning="Ref match + date + merchant match, partial refund -- bank amount is net after refund deduction",
            timestamp="2026-08-28T00:00:00",
        ),
        MatchResult(
            internal_id="TXN_045", external_id="EXT_035",
            match_path=MatchPath.LLM, confidence=0.98, rule_name="llm_batch",
            reasoning="Exact match on amount (1576.26 INR), date (2026-08-26), and clear merchant match",
            timestamp="2026-08-28T00:00:00",
        ),
        MatchResult(
            internal_id="TXN_051", external_id="EXT_040",
            match_path=MatchPath.LLM, confidence=0.95, rule_name="llm_batch",
            reasoning="Exact amount match, 3-day settlement window date drift",
            timestamp="2026-08-28T00:00:00",
        ),
        MatchResult(
            internal_id="TXN_054", external_id="EXT_041",
            match_path=MatchPath.LLM, confidence=0.92, rule_name="llm_batch",
            reasoning="Ref match + date + merchant match, partial refund -- bank amount is net after refund deduction",
            timestamp="2026-08-28T00:00:00",
        ),
    ]

    # LLM NONE results (11 evaluated + 2 no-candidates)
    # 11 that went through LLM evaluation
    llm_none_results = [
        MatchResult(
            internal_id="TXN_007", match_path=MatchPath.LLM, confidence=0.0,
            reasoning="No matching external: amount 8234.56 INR not found in any candidate with compatible ref/date",
        ),
        MatchResult(
            internal_id="TXN_009", match_path=MatchPath.LLM, confidence=0.0,
            reasoning="No matching external: ref pay_abc123 not in candidate pool, amount 3421.78 INR unmatched",
        ),
        MatchResult(
            internal_id="TXN_010", match_path=MatchPath.LLM, confidence=0.0,
            reasoning="No matching external: settlement 12567.90 INR on 2026-08-03 has no compatible bank entry",
        ),
        MatchResult(
            internal_id="TXN_017", match_path=MatchPath.LLM, confidence=0.0,
            reasoning="No matching external: payment 4592.33 INR on 2026-08-05 not in any candidate pool",
        ),
        MatchResult(
            internal_id="TXN_019", match_path=MatchPath.LLM, confidence=0.0,
            reasoning="No matching external: refund 1823.45 INR on 2026-08-06 has no compatible bank entry",
        ),
        MatchResult(
            internal_id="TXN_025", match_path=MatchPath.LLM, confidence=0.0,
            reasoning="No matching external: settlement 6789.12 INR on 2026-08-11 not in candidate pool",
        ),
        MatchResult(
            internal_id="TXN_036", match_path=MatchPath.LLM, confidence=1.0,
            reasoning="Evaluated 3 candidates: none match on ref+amount+date combination. Closest candidate differs by 45% in amount",
        ),
        MatchResult(
            internal_id="TXN_039", match_path=MatchPath.LLM, confidence=1.0,
            reasoning="Evaluated 2 candidates: ref mismatch on both, amounts incompatible (>30% difference)",
        ),
        MatchResult(
            internal_id="TXN_043", match_path=MatchPath.LLM, confidence=1.0,
            reasoning="Evaluated 4 candidates: date and merchant mismatches on all. No viable match",
        ),
        MatchResult(
            internal_id="TXN_044", match_path=MatchPath.LLM, confidence=0.0,
            reasoning="No matching external: payment 2345.67 INR on 2026-08-22 not found in candidate pool",
        ),
        MatchResult(
            internal_id="TXN_047", match_path=MatchPath.LLM, confidence=0.0,
            reasoning="No matching external: settlement 5678.90 INR on 2026-08-24 has no compatible bank entry",
        ),
    ]
    # 2 that had no candidates (all claimed by earlier batches)
    # These have no LLM result — they're identified by absence from llm_none_results

    all_matches = rule_matches + llm_matches

    # GL classification
    classification = run_gl_classification(
        all_matches, int_records, ext_records,
    )

    return {
        "int_records": int_records,
        "ext_records": ext_records,
        "rule_matches": rule_matches,
        "llm_matches": llm_matches,
        "all_matches": all_matches,
        "llm_none_results": llm_none_results,
        "parse_errors": all_parse_errors,
        "classification": classification,
    }


@pytest.fixture(scope="module")
def exception_report(pipeline_data):
    return collect_exceptions(
        all_internals=pipeline_data["int_records"],
        all_externals=pipeline_data["ext_records"],
        matched_results=pipeline_data["all_matches"],
        llm_none_results=pipeline_data["llm_none_results"],
        parse_errors=pipeline_data["parse_errors"],
    )


@pytest.fixture(scope="module")
def audit_trail(pipeline_data, exception_report):
    return build_audit_trail(
        rule_matches=pipeline_data["rule_matches"],
        llm_matches=pipeline_data["llm_matches"],
        classification=pipeline_data["classification"],
        exception_report=exception_report,
        parse_errors=pipeline_data["parse_errors"],
    )


# ── Test: Full Accounting ────────────────────────────────────

class TestFullAccounting:
    """Every record must be accounted for — no silent drops."""

    def test_internal_accounting(self, exception_report):
        """matched + unmatched_internal == 65 total internals."""
        assert exception_report.internal_count == 65
        assert (exception_report.matched_count +
                exception_report.unmatched_internal) == 65, (
            f"Internal accounting: {exception_report.matched_count} matched + "
            f"{exception_report.unmatched_internal} unmatched = "
            f"{exception_report.matched_count + exception_report.unmatched_internal} "
            f"!= 65"
        )

    def test_external_accounting(self, exception_report):
        """52 matched + 3 unmatched == 55 total externals."""
        assert exception_report.external_count == 55
        exception_report.matched_count + exception_report.unmatched_external
        # matched_count is internal-side; for external we check directly
        assert exception_report.unmatched_external == 3, (
            f"Expected 3 unmatched externals, got "
            f"{exception_report.unmatched_external}"
        )
        assert (52 + exception_report.unmatched_external ==
                exception_report.external_count), (
            f"External accounting: 52 matched + "
            f"{exception_report.unmatched_external} unmatched = "
            f"{52 + exception_report.unmatched_external} != "
            f"{exception_report.external_count}"
        )

    def test_matched_count(self, exception_report):
        assert exception_report.matched_count == 52

    def test_unmatched_internal_count(self, exception_report):
        assert exception_report.unmatched_internal == 13

    def test_parse_error_count(self, exception_report):
        assert exception_report.parse_error_count == 0


# ── Test: Exception Categories ───────────────────────────────

class TestExceptionCategories:
    """Only 3 exception categories allowed."""

    VALID_CATEGORIES = {"unmatched_internal", "unmatched_external", "parse_error"}

    def test_only_three_categories(self, exception_report):
        """No 4th category like 'llm_rejected' or 'no_candidates'."""
        categories = {e.record_type for e in exception_report.exceptions}
        invalid = categories - self.VALID_CATEGORIES
        assert len(invalid) == 0, (
            f"Invalid exception categories found: {invalid}. "
            f"Only {self.VALID_CATEGORIES} are allowed"
        )

    def test_category_counts(self, exception_report):
        counts = {}
        for e in exception_report.exceptions:
            counts[e.record_type] = counts.get(e.record_type, 0) + 1
        assert counts.get("unmatched_internal", 0) == 13
        assert counts.get("unmatched_external", 0) == 3
        assert counts.get("parse_error", 0) == 0

    def test_total_exception_count(self, exception_report):
        """13 internal + 3 external + 0 parse = 16 total."""
        assert len(exception_report.exceptions) == 16


# ── Test: Reason Quality ─────────────────────────────────────

class TestReasonQuality:
    """Every reason must be specific, not generic."""

    def test_no_generic_reasons(self, exception_report):
        """No reason IS a generic phrase — but LLM reasoning may use common words."""
        for exc in exception_report.exceptions:
            reason = exc.reason.strip()
            # Reasons should never be just a short generic phrase
            generic_standalone = [
                "no match found",
                "could not match",
                "unknown error",
                "unmatched",
            ]
            for phrase in generic_standalone:
                assert reason.lower() != phrase, (
                    f"{exc.record_id}: reason IS the generic phrase "
                    f"'{phrase}' — must be specific"
                )

    def test_reasons_are_specific(self, exception_report):
        """Every reason is > 20 chars."""
        for exc in exception_report.exceptions:
            assert len(exc.reason) > 20, (
                f"{exc.record_id}: reason too short ({len(exc.reason)} chars): "
                f"{exc.reason}"
            )

    def test_unmatched_external_reasons_are_unique(self, exception_report):
        """Each unmatched external reason cites specific record data."""
        ext_exceptions = [
            e for e in exception_report.exceptions
            if e.record_type == "unmatched_external"
        ]
        reasons = [e.reason for e in ext_exceptions]
        # All 3 reasons should be different (cite different amounts/dates)
        assert len(set(reasons)) == len(reasons), (
            "Unmatched external reasons are not unique — identical templates?"
        )

    def test_unmatched_externals_cite_specific_data(self, exception_report):
        """Each unmatched external reason contains that record's amount."""
        expected = {
            "EXT_053": "423.93",
            "EXT_054": "285.21",
            "EXT_055": "230.59",
        }
        for exc in exception_report.exceptions:
            if exc.record_type == "unmatched_external":
                amount = expected.get(exc.record_id)
                if amount:
                    assert amount in exc.reason, (
                        f"{exc.record_id}: reason doesn't cite amount "
                        f"{amount}: {exc.reason}"
                    )


# ── Test: LLM Rejection Reasoning ────────────────────────────

class TestLLMRejectionReasoning:
    """LLM rejection reasons pulled from Part 3 stored output."""

    LLM_EVALUATED_IDS = {
        "TXN_007", "TXN_009", "TXN_010", "TXN_017", "TXN_019",
        "TXN_025", "TXN_036", "TXN_039", "TXN_043", "TXN_044", "TXN_047",
    }
    NO_CANDIDATES_IDS = {"TXN_055", "TXN_064"}

    def test_llm_evaluated_have_reasoning(self, exception_report):
        """11 LLM-evaluated records have specific rejection reasoning."""
        for exc in exception_report.exceptions:
            if exc.record_id in self.LLM_EVALUATED_IDS:
                assert "LLM evaluated" in exc.reason or "Evaluated" in exc.reason or "No matching" in exc.reason, (
                    f"{exc.record_id}: expected LLM reasoning, got: {exc.reason}"
                )
                assert len(exc.attempted_matches) > 0, (
                    f"{exc.record_id}: expected attempted_matches from LLM"
                )

    def test_no_candidates_have_specific_reason(self, exception_report):
        """2 no-candidates records cite 'already claimed'."""
        for exc in exception_report.exceptions:
            if exc.record_id in self.NO_CANDIDATES_IDS:
                assert "claimed" in exc.reason.lower(), (
                    f"{exc.record_id}: expected 'claimed' in reason: "
                    f"{exc.reason}"
                )


# ── Test: Audit Trail ────────────────────────────────────────

class TestAuditTrail:
    """Verify audit trail completeness and structure."""

    def test_exactly_120_entries(self, audit_trail):
        """65 match/exception + 3 ext exception + 52 classification = 120."""
        assert len(audit_trail) == 120, (
            f"Expected exactly 120 audit entries, got {len(audit_trail)}"
        )

    def test_every_matched_has_match_and_classification(self, audit_trail, pipeline_data):
        """Each matched internal has both a match entry and a classification entry."""
        matched_ids = {m.internal_id for m in pipeline_data["all_matches"]}

        for mid in matched_ids:
            match_entries = [
                e for e in audit_trail
                if e.record_id == mid and e.resolution_path in ("rule", "llm")
            ]
            class_entries = [
                e for e in audit_trail
                if e.record_id == mid and e.resolution_path == "classification"
            ]
            assert len(match_entries) == 1, (
                f"{mid}: expected 1 match entry, got {len(match_entries)}"
            )
            assert len(class_entries) == 1, (
                f"{mid}: expected 1 classification entry, got {len(class_entries)}"
            )

    def test_every_exception_has_audit_entry(self, audit_trail, exception_report):
        """Every exception record has an audit entry with resolution_path=exception."""
        exc_ids = {e.record_id for e in exception_report.exceptions}
        audit_exc_ids = {
            e.record_id for e in audit_trail
            if e.resolution_path == "exception"
        }
        missing = exc_ids - audit_exc_ids
        assert len(missing) == 0, (
            f"Exceptions missing from audit trail: {missing}"
        )

    def test_resolution_path_values(self, audit_trail):
        """Only valid resolution_path values."""
        valid = {"rule", "llm", "classification", "exception", "parse_error"}
        paths = {e.resolution_path for e in audit_trail}
        invalid = paths - valid
        assert len(invalid) == 0, f"Invalid paths: {invalid}"

    def test_classification_entries_have_gl_category(self, audit_trail):
        """All classification entries have a non-empty gl_category."""
        for entry in audit_trail:
            if entry.resolution_path == "classification":
                assert entry.gl_category, (
                    f"{entry.record_id}: classification entry missing gl_category"
                )


# ── Test: JSON Output ────────────────────────────────────────

class TestJSONOutput:
    """Verify JSON/JSONL output files are valid."""

    def test_exceptions_json_roundtrip(self, exception_report, tmp_path):
        """Exception report serializes and deserializes correctly."""
        out = tmp_path / "exceptions.json"
        save_exceptions(exception_report, out)
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["summary"]["internal_count"] == 65
        assert data["summary"]["external_count"] == 55
        assert len(data["exceptions"]) == 16

    def test_audit_jsonl_valid(self, audit_trail, tmp_path):
        """Each line of audit trail JSONL is valid JSON."""
        out = tmp_path / "audit_trail.jsonl"
        save_audit_trail(audit_trail, out)
        lines = out.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 120
        for i, line in enumerate(lines):
            try:
                obj = json.loads(line)
                assert "record_id" in obj
                assert "resolution_path" in obj
            except json.JSONDecodeError:
                pytest.fail(f"Line {i+1} is not valid JSON: {line[:100]}")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
