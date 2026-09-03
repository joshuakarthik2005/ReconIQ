"""
Audit Trail (Part 5b)
======================

Builds an immutable, chronological audit trail for every pipeline decision.
Output is JSON-lines (one AuditEntry per line) for machine consumption.

Every record in the pipeline gets at least one audit entry:
  - Matched internals: match entry + GL classification entry
  - Unmatched internals: exception entry
  - Unmatched externals: exception entry
  - Parse errors: parse_error entry

Expected total on this dataset: exactly 120 entries
  = 65 (one per internal: 52 match + 13 exception)
  + 3 (unmatched externals)
  + 52 (GL classification, one per match)
  + 0 (parse errors)
"""

import json
from datetime import datetime
from pathlib import Path
from typing import List

from .gl_classifier import ClassificationOutput, ClassifiedEntry
from .exceptions import ExceptionReport
from .schemas import (
    AuditEntry,
    ExceptionRecord,
    MatchPath,
    MatchResult,
    ParseError,
)


def build_audit_trail(
    rule_matches: List[MatchResult],
    llm_matches: List[MatchResult],
    classification: ClassificationOutput,
    exception_report: ExceptionReport,
    parse_errors: List[ParseError],
) -> List[AuditEntry]:
    """
    Build a complete audit trail covering every pipeline decision.

    Returns exactly one AuditEntry per decision point.  The total count
    on this dataset is exactly 120 (0 parse errors).
    """
    entries: List[AuditEntry] = []

    # ── Match entries (one per matched internal) ─────────────
    for match in rule_matches:
        entries.append(AuditEntry(
            record_id=match.internal_id,
            record_type="internal",
            resolution_path="rule",
            detail=f"Rule {match.rule_name}: {match.reasoning}",
            matched_to=match.external_id,
            confidence=match.confidence,
            gl_category="",  # filled in classification pass
            rule_name=match.rule_name or "",
            timestamp=match.timestamp or datetime.now().isoformat(),
        ))

    for match in llm_matches:
        entries.append(AuditEntry(
            record_id=match.internal_id,
            record_type="internal",
            resolution_path="llm",
            detail=(
                f"LLM match (confidence={match.confidence}): "
                f"{match.reasoning}"
            ),
            matched_to=match.external_id,
            confidence=match.confidence,
            gl_category="",  # filled in classification pass
            rule_name=match.rule_name or "",
            timestamp=match.timestamp or datetime.now().isoformat(),
        ))

    # ── GL classification entries (one per classified record) ─
    for entry in classification.classified:
        sub_detail = ""
        if entry.sub_entries:
            sub_amounts = ", ".join(
                f"{s.category.value}={s.amount}"
                for s in entry.sub_entries
            )
            sub_detail = f"; Phase B fee split: {sub_amounts}"

        entries.append(AuditEntry(
            record_id=entry.match_result.internal_id,
            record_type="internal",
            resolution_path="classification",
            detail=(
                f"Phase A: {entry.rule_name} -> {entry.gl_category.value}"
                f"{sub_detail}"
            ),
            matched_to=entry.match_result.external_id,
            confidence=entry.match_result.confidence,
            gl_category=entry.gl_category.value,
            rule_name=entry.rule_name,
            timestamp=datetime.now().isoformat(),
        ))

    # ── Exception entries ────────────────────────────────────
    for exc in exception_report.exceptions:
        entries.append(AuditEntry(
            record_id=exc.record_id,
            record_type=exc.record_type,
            resolution_path="exception",
            detail=exc.reason,
            matched_to=None,
            confidence=0.0,
            gl_category="",
            timestamp=exc.timestamp or datetime.now().isoformat(),
        ))

    # ── Parse error entries ──────────────────────────────────
    for pe in parse_errors:
        entries.append(AuditEntry(
            record_id=f"{pe.source_file}:row{pe.row_number}",
            record_type="parse_error",
            resolution_path="parse_error",
            detail=f"{pe.source_file} row {pe.row_number}: {pe.error_message}",
            matched_to=None,
            confidence=0.0,
            gl_category="",
            timestamp=pe.timestamp or datetime.now().isoformat(),
        ))

    return entries


def save_audit_trail(entries: List[AuditEntry], output_path: Path) -> None:
    """Save audit trail as JSON-lines (one entry per line)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        for entry in entries:
            line = json.dumps({
                "record_id": entry.record_id,
                "record_type": entry.record_type,
                "resolution_path": entry.resolution_path,
                "detail": entry.detail,
                "matched_to": entry.matched_to,
                "confidence": entry.confidence,
                "gl_category": entry.gl_category,
                "rule_name": entry.rule_name,
                "timestamp": entry.timestamp,
            }, ensure_ascii=False)
            f.write(line + "\n")
# _version: per-decision-timestamps
