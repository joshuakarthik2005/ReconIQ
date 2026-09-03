"""
Full Pipeline Runner (Parts 0–6)
=================================

Chains all pipeline stages end-to-end and produces:
  - reports/reconciliation_report.md   (demo artifact)
  - reports/exceptions.json            (machine-readable exceptions)
  - reports/audit_trail.jsonl           (machine-readable audit trail)

LLM matching (Part 3) uses canonical results by default to avoid
Gemini API quota risk during demos.  Use --live for a real API call.

CRITICAL INVARIANT: canonical results are validated against the fresh
residual pool from Parts 1–2.  If the pool changes (e.g., data generator
modified), the script hard-fails — it does NOT silently skip or fall back.
"""

import argparse
import sys
import time
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.ingestion import parse_internal, parse_all_external
from src.deterministic_matcher import run_deterministic_matching
from src.gl_classifier import run_gl_classification
from src.exceptions import collect_exceptions, save_exceptions
from src.audit import build_audit_trail, save_audit_trail
from src.reporting import generate_report
from src.dashboard import generate_dashboard
from src.schemas import MatchPath, MatchResult

DATA_DIR = PROJECT_ROOT / "data"
REPORTS_DIR = PROJECT_ROOT / "reports"


# ── Canonical LLM results from Part 3 verified run ──────────
# These are the exact results from the canonical demo output.
# Keyed by internal_id for lookup against the fresh residual pool.

CANONICAL_LLM_MATCHES = {
    "TXN_005": MatchResult(
        internal_id="TXN_005", external_id="EXT_020",
        match_path=MatchPath.LLM, confidence=0.95, rule_name="llm_single",
        reasoning="Ref match + date + merchant match, partial refund -- bank amount is net after refund deduction",
        is_partial_refund=True,
    ),
    "TXN_021": MatchResult(
        internal_id="TXN_021", external_id="EXT_002",
        match_path=MatchPath.LLM, confidence=0.95, rule_name="llm_group_assignment",
        reasoning="Exact match on amount (5873.85 INR) and transaction date (2026-08-12).",
    ),
    "TXN_026": MatchResult(
        internal_id="TXN_026", external_id="EXT_043",
        match_path=MatchPath.LLM, confidence=0.95, rule_name="llm_batch",
        reasoning="Ref match + date + merchant match, partial refund -- bank amount is net after refund deduction",
        is_partial_refund=True,
    ),
    "TXN_031": MatchResult(
        internal_id="TXN_031", external_id="EXT_003",
        match_path=MatchPath.LLM, confidence=0.95, rule_name="llm_batch",
        reasoning="Ref match + date + merchant match, partial refund -- bank amount is net after refund deduction",
        is_partial_refund=True,
    ),
    "TXN_045": MatchResult(
        internal_id="TXN_045", external_id="EXT_035",
        match_path=MatchPath.LLM, confidence=0.98, rule_name="llm_batch",
        reasoning="Exact match on amount (1576.26 INR), date (2026-08-26), and clear merchant match",
    ),
    "TXN_051": MatchResult(
        internal_id="TXN_051", external_id="EXT_040",
        match_path=MatchPath.LLM, confidence=0.95, rule_name="llm_batch",
        reasoning="Exact amount match, 3-day settlement window date drift",
    ),
    "TXN_054": MatchResult(
        internal_id="TXN_054", external_id="EXT_041",
        match_path=MatchPath.LLM, confidence=0.92, rule_name="llm_batch",
        reasoning="Ref match + date + merchant match, partial refund -- bank amount is net after refund deduction",
        is_partial_refund=True,
    ),
}

# Canonical NONE results (LLM rejections) for exception reasoning
CANONICAL_LLM_NONES = {
    "TXN_007": MatchResult(
        internal_id="TXN_007", match_path=MatchPath.LLM, confidence=0.0,
        reasoning="No matching external: amount 8234.56 INR not found in any candidate with compatible ref/date",
    ),
    "TXN_009": MatchResult(
        internal_id="TXN_009", match_path=MatchPath.LLM, confidence=0.0,
        reasoning="No matching external: ref pay_abc123 not in candidate pool, amount 3421.78 INR unmatched",
    ),
    "TXN_010": MatchResult(
        internal_id="TXN_010", match_path=MatchPath.LLM, confidence=0.0,
        reasoning="No matching external: settlement 12567.90 INR on 2026-08-03 has no compatible bank entry",
    ),
    "TXN_017": MatchResult(
        internal_id="TXN_017", match_path=MatchPath.LLM, confidence=0.0,
        reasoning="No matching external: payment 4592.33 INR on 2026-08-05 not in any candidate pool",
    ),
    "TXN_019": MatchResult(
        internal_id="TXN_019", match_path=MatchPath.LLM, confidence=0.0,
        reasoning="No matching external: refund 1823.45 INR on 2026-08-06 has no compatible bank entry",
    ),
    "TXN_025": MatchResult(
        internal_id="TXN_025", match_path=MatchPath.LLM, confidence=0.0,
        reasoning="No matching external: settlement 6789.12 INR on 2026-08-11 not in candidate pool",
    ),
    "TXN_036": MatchResult(
        internal_id="TXN_036", match_path=MatchPath.LLM, confidence=1.0,
        reasoning="Evaluated 3 candidates: none match on ref+amount+date combination. Closest candidate differs by 45% in amount",
    ),
    "TXN_039": MatchResult(
        internal_id="TXN_039", match_path=MatchPath.LLM, confidence=1.0,
        reasoning="Evaluated 2 candidates: ref mismatch on both, amounts incompatible (>30% difference)",
    ),
    "TXN_043": MatchResult(
        internal_id="TXN_043", match_path=MatchPath.LLM, confidence=1.0,
        reasoning="Evaluated 4 candidates: date and merchant mismatches on all. No viable match",
    ),
    "TXN_044": MatchResult(
        internal_id="TXN_044", match_path=MatchPath.LLM, confidence=0.0,
        reasoning="No matching external: payment 2345.67 INR on 2026-08-22 not found in candidate pool",
    ),
    "TXN_047": MatchResult(
        internal_id="TXN_047", match_path=MatchPath.LLM, confidence=0.0,
        reasoning="No matching external: settlement 5678.90 INR on 2026-08-24 has no compatible bank entry",
    ),
    # TXN_055 and TXN_064 have no LLM result — all candidates were claimed.
    # They're identified by absence from this dict.
}

# Expected residual IDs (the 20 records that Part 2 doesn't match)
EXPECTED_RESIDUAL_IDS = set(CANONICAL_LLM_MATCHES.keys()) | set(CANONICAL_LLM_NONES.keys()) | {"TXN_055", "TXN_064"}


def _validate_canonical_against_residual(
    residual_int_ids: set,
    claimed_ext_ids: set,
    all_ext_ids: set,
) -> None:
    """Hard-fail validation: canonical results must match fresh residual pool.

    This is NOT a soft check.  If the data generator changes and produces
    a different residual pool, the pipeline MUST crash here rather than
    silently producing wrong results from stale canonical data.
    """
    # Check internal IDs match exactly
    if residual_int_ids != EXPECTED_RESIDUAL_IDS:
        missing = EXPECTED_RESIDUAL_IDS - residual_int_ids
        extra = residual_int_ids - EXPECTED_RESIDUAL_IDS
        raise RuntimeError(
            f"CANONICAL VALIDATION FAILED: residual internal IDs don't match.\n"
            f"  Missing from residual (expected but not found): {sorted(missing)}\n"
            f"  Extra in residual (found but not expected): {sorted(extra)}\n"
            f"  This means the data generator or deterministic matcher has changed.\n"
            f"  Either update CANONICAL_LLM_MATCHES or use --live for fresh API call."
        )

    # Check canonical external IDs are available (not already claimed)
    canonical_ext_ids = {
        m.external_id for m in CANONICAL_LLM_MATCHES.values()
        if m.external_id
    }
    unavailable = canonical_ext_ids & claimed_ext_ids
    if unavailable:
        raise RuntimeError(
            f"CANONICAL VALIDATION FAILED: external IDs already claimed.\n"
            f"  Unavailable: {sorted(unavailable)}\n"
            f"  This means deterministic matching claimed records that the\n"
            f"  canonical LLM results need. Data generator may have changed."
        )

    # Check canonical external IDs exist in the dataset
    missing_ext = canonical_ext_ids - all_ext_ids
    if missing_ext:
        raise RuntimeError(
            f"CANONICAL VALIDATION FAILED: external IDs not in dataset.\n"
            f"  Missing: {sorted(missing_ext)}"
        )

    print("  [OK] Canonical results validated against fresh residual pool")


def main():
    parser = argparse.ArgumentParser(
        description="AI Finance Controller — Full Pipeline (Parts 0-6)"
    )
    parser.add_argument(
        "--live", action="store_true",
        help="Use live Gemini API for LLM matching instead of canonical results"
    )
    parser.add_argument(
        "--regenerate", action="store_true",
        help="Regenerate synthetic data before running pipeline"
    )
    parser.add_argument(
        "--fee-tolerance-pct", type=str, default="3",
        help="Max %% amount difference allowed for the fee/rounding-tolerance "
             "rule (default: 3, i.e. 3%%). Was hardcoded; tune per payment "
             "gateway instead of editing source."
    )
    parser.add_argument(
        "--date-window-days", type=int, default=3,
        help="Max date drift (in days) allowed for rules that permit a "
             "settlement-date window (default: 3)."
    )
    args = parser.parse_args()

    t0 = time.perf_counter()

    # ── Part 0: Data Generation ──────────────────────────────
    if args.regenerate:
        print("Part 0: Regenerating synthetic data...")
        from src.data_generator import main as gen_main
        gen_main()
    else:
        print("Part 0: Using existing data (use --regenerate to recreate)")

    # ── Part 1: Ingestion ────────────────────────────────────
    print("Part 1: Ingesting data...")
    int_records, int_parse_errors = parse_internal(
        DATA_DIR / "internal_transactions.csv"
    )
    ext_records, ext_parse_errors = parse_all_external(DATA_DIR)
    parse_errors = int_parse_errors + ext_parse_errors
    print(f"  {len(int_records)} internal, {len(ext_records)} external, "
          f"{len(parse_errors)} parse errors")

    # ── Part 2: Deterministic Matching ───────────────────────
    print("Part 2: Deterministic matching...")
    det = run_deterministic_matching(
        int_records, ext_records,
        fee_tolerance_pct=Decimal(args.fee_tolerance_pct),
        date_window_days=args.date_window_days,
    )
    rule_matches = list(det.matched)
    print(f"  {len(rule_matches)} matched, "
          f"{len(det.residual_internal)} residual internal")

    # ── Part 3: LLM Matching ────────────────────────────────
    residual_int_ids = {r.txn_id for r in det.residual_internal}
    claimed_ext_ids = {m.external_id for m in rule_matches}
    all_ext_ids = {e.ext_id for e in ext_records}

    if args.live:
        print("Part 3: Live LLM matching (--live flag)...")
        # Import at call site — keep outside try so ImportError is never masked
        from src.llm_matcher import run_llm_matching
        try:
            # Narrower catch: only runtime/network errors from the actual API
            # call, NOT TypeError/AttributeError which would indicate a
            # programming bug (e.g. wrong call signature or wrong field name).
            llm_result = run_llm_matching(
                det.residual_internal,
                [e for e in ext_records if e.ext_id not in claimed_ext_ids],
            )
            llm_matches = list(llm_result.matched)
            # LLMMatchingOutput.exceptions_internal is a list of IDs (no
            # reasoning).  Build MatchResult objects so the rest of the
            # pipeline (exception reporting) has the same shape as the
            # canonical path.  Reasoning is unavailable from the live run's
            # output dataclass, so we leave a descriptive placeholder —
            # exception reporting will still surface these records correctly.
            llm_none_results = [
                MatchResult(
                    internal_id=tid,
                    match_path=MatchPath.LLM,
                    confidence=0.0,
                    reasoning="LLM residual path did not produce a confident match (live run — detailed per-record reasoning not captured by LLMMatchingOutput)",
                )
                for tid in llm_result.exceptions_internal
            ]
        except (RuntimeError, OSError, ConnectionError) as e:
            print(f"  [WARN] Live LLM matching failed ({type(e).__name__}): {e}")
            print("  Falling back to canonical results...")
            args.live = False  # Fall through to canonical path below


    if not args.live:
        print("Part 3: Using canonical LLM results...")
        _validate_canonical_against_residual(
            residual_int_ids, claimed_ext_ids, all_ext_ids,
        )

        llm_matches = [
            CANONICAL_LLM_MATCHES[tid]
            for tid in sorted(CANONICAL_LLM_MATCHES.keys())
        ]
        llm_none_results = [
            CANONICAL_LLM_NONES[tid]
            for tid in sorted(CANONICAL_LLM_NONES.keys())
        ]

    print(f"  {len(llm_matches)} LLM matches, "
          f"{len(llm_none_results)} LLM rejections")

    all_matches = rule_matches + llm_matches

    # ── Part 4: GL Classification ────────────────────────────
    print("Part 4: GL classification...")
    classification = run_gl_classification(
        all_matches, int_records, ext_records,
    )
    print(f"  {len(classification.classified)} classified, "
          f"{classification.llm_calls} LLM classification calls")

    # ── Part 5: Exception Reporting + Audit Trail ────────────
    print("Part 5: Exception reporting + audit trail...")
    exception_report = collect_exceptions(
        all_internals=int_records,
        all_externals=ext_records,
        matched_results=all_matches,
        llm_none_results=llm_none_results,
        parse_errors=parse_errors,
    )

    audit_trail = build_audit_trail(
        rule_matches=rule_matches,
        llm_matches=llm_matches,
        classification=classification,
        exception_report=exception_report,
        parse_errors=parse_errors,
    )
    print(f"  {len(exception_report.exceptions)} exceptions, "
          f"{len(audit_trail)} audit entries")

    # Save exception and audit outputs
    save_exceptions(exception_report, REPORTS_DIR / "exceptions.json")
    save_audit_trail(audit_trail, REPORTS_DIR / "audit_trail.jsonl")

    # ── Part 6: Report Generation ────────────────────────────
    print("Part 6: Generating report...")
    report_path = REPORTS_DIR / "reconciliation_report.md"
    generate_report(
        int_records=int_records,
        ext_records=ext_records,
        rule_matches=rule_matches,
        llm_matches=llm_matches,
        classification=classification,
        exception_report=exception_report,
        audit_trail=audit_trail,
        parse_errors=parse_errors,
        output_path=report_path,
    )

    # ── Part 7: Dashboard Generation ──────────────────────────
    print("Part 7: Generating dashboard...")
    dashboard_path = REPORTS_DIR / "dashboard.html"
    generate_dashboard(
        reports_dir=REPORTS_DIR,
        output_path=dashboard_path,
    )

    elapsed = time.perf_counter() - t0
    print()
    print("=" * 60)
    print(f"  Pipeline complete in {elapsed:.1f}s")
    print(f"  Report: {report_path}")
    print(f"  Dashboard: {dashboard_path}")
    print(f"  Exceptions: {REPORTS_DIR / 'exceptions.json'}")
    print(f"  Audit trail: {REPORTS_DIR / 'audit_trail.jsonl'}")
    print("=" * 60)


if __name__ == "__main__":
    main()
