"""
Tests for Dashboard Generator (Part 7b)
=========================================

Verifies that generate_dashboard() produces HTML with embedded data
that matches the pipeline's own computed summary from exceptions.json.
No independently reconstructed values — all assertions compare against
the same source data the dashboard reads.
"""

import json
import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.dashboard import generate_dashboard

REPORTS_DIR = PROJECT_ROOT / "reports"
DASHBOARD_PATH = REPORTS_DIR / "dashboard.html"


# ── Fixture ──────────────────────────────────────────────────

@pytest.fixture(scope="module")
def dashboard_html():
    """Generate dashboard and return the HTML string."""
    if not (REPORTS_DIR / "exceptions.json").exists():
        pytest.skip("Reports not generated — run run_full_pipeline.py first")
    # Generate into a temp path to test the function itself
    out = REPORTS_DIR / "_test_dashboard.html"
    generate_dashboard(reports_dir=REPORTS_DIR, output_path=out)
    html = out.read_text(encoding="utf-8")
    out.unlink()  # clean up
    return html


@pytest.fixture(scope="module")
def summary():
    """Load the pipeline's own summary from exceptions.json."""
    with open(REPORTS_DIR / "exceptions.json", "r", encoding="utf-8") as f:
        return json.load(f)["summary"]


@pytest.fixture(scope="module")
def exceptions_list():
    """Load the exception records from exceptions.json."""
    with open(REPORTS_DIR / "exceptions.json", "r", encoding="utf-8") as f:
        return json.load(f)["exceptions"]


@pytest.fixture(scope="module")
def audit_entries():
    """Load audit trail entries."""
    entries = []
    with open(REPORTS_DIR / "audit_trail.jsonl", "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


# ── Test: KPI headline numbers match exceptions.json ─────────

class TestDashboardKPINumbers:
    """Verify that the dashboard's embedded headline numbers match
    the pipeline's own computed summary — not independently derived."""

    def test_matched_count_in_html(self, dashboard_html, summary):
        """matched_count from summary appears in the KPI section."""
        matched = summary["matched_count"]
        internal = summary["internal_count"]
        # The dashboard renders "{matched}/{internal}" in the KPI card
        assert f"{matched}/{internal}" in dashboard_html

    def test_match_rate_correct(self, dashboard_html, summary):
        """Match rate = matched_count / internal_count * 100, rounded."""
        matched = summary["matched_count"]
        internal = summary["internal_count"]
        expected_pct = round(matched / internal * 100)
        # The ring chart shows "{pct}%"
        assert f"{expected_pct}%" in dashboard_html

    def test_rule_match_count(self, dashboard_html, audit_entries):
        """Rule match count in KPI matches audit trail data."""
        rule_count = sum(
            1 for e in audit_entries if e["resolution_path"] == "rule"
        )
        # Appears as the value in the "Rule Matches" KPI card
        assert f">{rule_count}<" in dashboard_html

    def test_llm_match_count(self, dashboard_html, audit_entries):
        """LLM match count in KPI matches audit trail data."""
        llm_count = sum(
            1 for e in audit_entries if e["resolution_path"] == "llm"
        )
        assert f">{llm_count}<" in dashboard_html

    def test_exception_count(self, dashboard_html, exceptions_list):
        """Exception count in KPI matches exceptions.json record count."""
        exc_count = len(exceptions_list)
        assert f">{exc_count}<" in dashboard_html

    def test_external_count(self, dashboard_html, summary):
        """External record count matches summary."""
        assert f">{summary['external_count']}<" in dashboard_html

    def test_parse_error_count(self, dashboard_html, summary):
        """Parse error count matches summary."""
        assert f">{summary['parse_error_count']}<" in dashboard_html


# ── Test: GL category totals in embedded JSON ────────────────

class TestDashboardGLCounts:
    """Verify the GL category breakdown data embedded in the HTML
    matches what the audit trail actually contains."""

    def test_gl_totals_match_classified_count(self, dashboard_html, summary):
        """Sum of GL_COUNTS values == matched_count (every match classified)."""
        # Extract the embedded GL_COUNTS JSON
        m = re.search(r"const GL_COUNTS = ({.*?});", dashboard_html)
        assert m, "GL_COUNTS not found in HTML"
        gl_counts = json.loads(m.group(1))
        assert sum(gl_counts.values()) == summary["matched_count"]

    def test_settlement_income_is_largest(self, dashboard_html):
        """Settlement Income has the highest count."""
        m = re.search(r"const GL_COUNTS = ({.*?});", dashboard_html)
        gl_counts = json.loads(m.group(1))
        assert max(gl_counts, key=gl_counts.get) == "Settlement Income"

    def test_gl_counts_match_audit_trail(self, dashboard_html, audit_entries):
        """GL counts in dashboard match what audit trail classification
        entries actually contain."""
        # From audit trail
        from collections import Counter
        expected = Counter()
        for e in audit_entries:
            if e["resolution_path"] == "classification" and e.get("gl_category"):
                expected[e["gl_category"]] += 1

        # From dashboard
        m = re.search(r"const GL_COUNTS = ({.*?});", dashboard_html)
        actual = json.loads(m.group(1))
        assert dict(actual) == dict(expected)


# ── Test: Exception data embedded correctly ──────────────────

class TestDashboardExceptionData:
    """Verify the exception records are correctly embedded."""

    def test_all_exceptions_embedded(self, dashboard_html, exceptions_list):
        """Every exception record_id appears in the embedded JSON."""
        m = re.search(r"const EXCEPTIONS = (\[.*?\]);", dashboard_html, re.DOTALL)
        assert m, "EXCEPTIONS not found in HTML"
        embedded = json.loads(m.group(1))
        embedded_ids = {e["record_id"] for e in embedded}
        expected_ids = {e["record_id"] for e in exceptions_list}
        assert embedded_ids == expected_ids

    def test_exception_reasons_preserved(self, dashboard_html, exceptions_list):
        """Exception reasons are embedded verbatim, not truncated."""
        m = re.search(r"const EXCEPTIONS = (\[.*?\]);", dashboard_html, re.DOTALL)
        embedded = json.loads(m.group(1))
        embedded_by_id = {e["record_id"]: e for e in embedded}
        for exc in exceptions_list:
            assert embedded_by_id[exc["record_id"]]["reason"] == exc["reason"]


# ── Test: Rule counts in embedded JSON ───────────────────────

class TestDashboardRuleCounts:
    """Verify rule distribution data matches audit trail."""

    def test_rule_counts_sum_to_matches(self, dashboard_html, summary):
        """Sum of RULE_COUNTS == matched_count."""
        m = re.search(r"const RULE_COUNTS = ({.*?});", dashboard_html)
        assert m, "RULE_COUNTS not found in HTML"
        rule_counts = json.loads(m.group(1))
        assert sum(rule_counts.values()) == summary["matched_count"]

    def test_rule_counts_match_audit_trail(self, dashboard_html, audit_entries):
        """Rule counts match audit trail rule/llm entries."""
        from collections import Counter
        expected = Counter()
        for e in audit_entries:
            if e["resolution_path"] in ("rule", "llm") and e.get("rule_name"):
                expected[e["rule_name"]] += 1

        m = re.search(r"const RULE_COUNTS = ({.*?});", dashboard_html)
        actual = json.loads(m.group(1))
        assert dict(actual) == dict(expected)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
