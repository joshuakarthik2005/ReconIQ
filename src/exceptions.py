"""
Exception Reporting (Part 5a)
==============================

Collects every unresolved record from the pipeline with a specific,
human-readable reason.  Never uses generic messages like "no match found".

Exception categories (exactly 3):
  - unmatched_internal: internal record with no matching external
  - unmatched_external: external record not claimed by any match
  - parse_error: row/entry that failed ingestion

"llm_rejected" and "no_candidates" are reason-text explanations nested
inside unmatched_internal, not peer categories — this avoids double-listing.

LLM rejection reasoning is pulled directly from Part 3's stored output,
not recomputed independently.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set

from .schemas import (
    ExceptionRecord,
    ExternalTransaction,
    InternalTransaction,
    MatchResult,
    ParseError,
)


@dataclass
class ExceptionReport:
    """Complete exception report from the pipeline."""
    exceptions: List[ExceptionRecord] = field(default_factory=list)
    internal_count: int = 0       # total internal records
    external_count: int = 0       # total external records
    matched_count: int = 0        # matched records
    unmatched_internal: int = 0   # unmatched internal records
    unmatched_external: int = 0   # unmatched external records
    parse_error_count: int = 0    # parse errors


def _build_unmatched_internal_reason(
    internal: InternalTransaction,
    llm_result: Optional[MatchResult],
) -> str:
    """Build a specific reason for an unmatched internal record.

    Pulls LLM rejection reasoning directly from Part 3's stored output
    (item 6 from user feedback).  Two sub-cases:
      - LLM evaluated candidates and rejected all (11 records)
      - No candidates available after claim filtering (2 records)
    """
    if llm_result is not None and llm_result.reasoning:
        # LLM evaluated this record — use its specific reasoning
        return (
            f"LLM evaluated candidates and rejected all: "
            f"{llm_result.reasoning}"
        )

    # No LLM result or empty reasoning — no candidates were available
    return (
        f"No matching external record for {internal.txn_id} "
        f"({internal.txn_type}, {internal.amount} {internal.currency}, "
        f"{internal.date}, merchant={internal.merchant_name}): "
        f"all shortlist candidates were already claimed by earlier matches"
    )


def _build_unmatched_external_reason(
    external: ExternalTransaction,
) -> str:
    """Build a specific reason for an unmatched external record.

    Each reason cites the specific record's own amount/date/description
    (item 4 from user feedback — no identical templated sentences).
    """
    ref_note = f"ref={external.reference_id}" if external.reference_id else "no reference ID"
    return (
        f"External record {external.ext_id} not claimed by any internal match: "
        f"{external.amount} INR on {external.date}, {ref_note}, "
        f"description=\"{external.description}\" (format {external.source_format}). "
        f"No internal record references this entry with compatible amount/date"
    )


def collect_exceptions(
    all_internals: List[InternalTransaction],
    all_externals: List[ExternalTransaction],
    matched_results: List[MatchResult],
    llm_none_results: List[MatchResult],
    parse_errors: List[ParseError],
) -> ExceptionReport:
    """
    Collect all exception records from the pipeline.

    Full accounting constraint: every internal and external record must
    appear in either the matched set OR the exception set.  No silent drops.

    Args:
        all_internals: All 65 internal records from Part 1
        all_externals: All 55 external records from Part 1
        matched_results: All 52 matched MatchResults (45 rule + 7 LLM)
        llm_none_results: MatchResults where LLM said NONE (for reasoning)
        parse_errors: ParseErrors from Part 1 ingestion
    """
    now = datetime.now().isoformat()

    # Build lookup sets
    matched_int_ids: Set[str] = {m.internal_id for m in matched_results}
    matched_ext_ids: Set[str] = {
        m.external_id for m in matched_results if m.external_id
    }

    # LLM NONE results indexed by internal_id (for reasoning retrieval)
    llm_none_by_id: Dict[str, MatchResult] = {
        m.internal_id: m for m in llm_none_results
    }

    int_by_id: Dict[str, InternalTransaction] = {
        t.txn_id: t for t in all_internals
    }
    ext_by_id: Dict[str, ExternalTransaction] = {
        e.ext_id: e for e in all_externals
    }

    exceptions: List[ExceptionRecord] = []

    # ── Unmatched internal records ───────────────────────────
    unmatched_int_ids = {t.txn_id for t in all_internals} - matched_int_ids
    for txn_id in sorted(unmatched_int_ids):
        internal = int_by_id[txn_id]
        llm_result = llm_none_by_id.get(txn_id)

        reason = _build_unmatched_internal_reason(internal, llm_result)

        # Build attempted_matches list from LLM result if available
        attempted = []
        if llm_result is not None:
            attempted.append({
                "match_path": "llm",
                "decision": "NONE",
                "confidence": llm_result.confidence,
                "reasoning": llm_result.reasoning,
            })

        exceptions.append(ExceptionRecord(
            record_id=txn_id,
            record_type="unmatched_internal",
            reason=reason,
            attempted_matches=attempted,
            timestamp=now,
        ))

    # ── Unmatched external records ───────────────────────────
    unmatched_ext_ids = {e.ext_id for e in all_externals} - matched_ext_ids
    for ext_id in sorted(unmatched_ext_ids):
        external = ext_by_id[ext_id]
        reason = _build_unmatched_external_reason(external)

        exceptions.append(ExceptionRecord(
            record_id=ext_id,
            record_type="unmatched_external",
            reason=reason,
            attempted_matches=[],
            timestamp=now,
        ))

    # ── Parse errors ─────────────────────────────────────────
    for pe in parse_errors:
        exceptions.append(ExceptionRecord(
            record_id=f"{pe.source_file}:row{pe.row_number}",
            record_type="parse_error",
            reason=(
                f"Parse failure in {pe.source_file} row {pe.row_number}: "
                f"{pe.error_message}"
            ),
            attempted_matches=[],
            timestamp=now,
        ))

    return ExceptionReport(
        exceptions=exceptions,
        internal_count=len(all_internals),
        external_count=len(all_externals),
        matched_count=len(matched_results),
        unmatched_internal=len(unmatched_int_ids),
        unmatched_external=len(unmatched_ext_ids),
        parse_error_count=len(parse_errors),
    )


def save_exceptions(report: ExceptionReport, output_path: Path) -> None:
    """Save exception report as JSON."""
    data = {
        "summary": {
            "internal_count": report.internal_count,
            "external_count": report.external_count,
            "matched_count": report.matched_count,
            "unmatched_internal": report.unmatched_internal,
            "unmatched_external": report.unmatched_external,
            "parse_error_count": report.parse_error_count,
        },
        "exceptions": [
            {
                "record_id": e.record_id,
                "record_type": e.record_type,
                "reason": e.reason,
                "attempted_matches": e.attempted_matches,
                "timestamp": e.timestamp,
            }
            for e in report.exceptions
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
