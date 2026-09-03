"""
Settlement Q&A Layer (Part 7)
==============================

Post-pipeline Q&A interface grounded in the pipeline's own outputs.
Reads ``reports/exceptions.json`` and ``reports/audit_trail.jsonl`` —
no pipeline modules imported at runtime, no re-running, no invented facts.

Three tiers:
  1. Deterministic ID lookup  — "Why didn't TXN_007 match?"
  2. Deterministic aggregation — "Breakdown by GL category"
  3. Grounded LLM fallback    — free-text, but context-only (no parametric)
"""

import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional


# ── Data Loading ─────────────────────────────────────────────

def _load_exceptions(reports_dir: Path) -> dict:
    """Load exceptions.json into a dict."""
    path = reports_dir / "exceptions.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_audit_trail(reports_dir: Path) -> List[dict]:
    """Load audit_trail.jsonl into a list of dicts."""
    path = reports_dir / "audit_trail.jsonl"
    entries = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


# ── SettlementQA ─────────────────────────────────────────────

class SettlementQA:
    """Post-pipeline Q&A interface grounded in structured pipeline outputs.

    Loads ``exceptions.json`` and ``audit_trail.jsonl`` at init time.
    All deterministic methods work without an API key.  The ``answer()``
    method's LLM fallback requires a Gemini API key.
    """

    def __init__(self, reports_dir: Path):
        self._reports_dir = Path(reports_dir)
        self._exceptions = _load_exceptions(self._reports_dir)
        self._audit = _load_audit_trail(self._reports_dir)

        # Index: record_id → list of audit entries
        self._audit_by_id: Dict[str, List[dict]] = {}
        for entry in self._audit:
            rid = entry["record_id"]
            self._audit_by_id.setdefault(rid, []).append(entry)

        # Index: record_id → exception record
        self._exception_by_id: Dict[str, dict] = {
            e["record_id"]: e for e in self._exceptions.get("exceptions", [])
        }

    # ── Deterministic lookups ────────────────────────────────

    def why_unmatched(self, record_id: str) -> str:
        """Explain why a record didn't match, or confirm it did match.

        Returns the exception reason verbatim for unmatched records,
        or a "was matched" message for matched records.
        """
        # Check exceptions first
        exc = self._exception_by_id.get(record_id)
        if exc is not None:
            return exc["reason"]

        # Check audit trail for a match entry
        entries = self._audit_by_id.get(record_id, [])
        match_entries = [
            e for e in entries
            if e["resolution_path"] in ("rule", "llm")
        ]
        if match_entries:
            e = match_entries[0]
            return (
                f"{record_id} was successfully matched to "
                f"{e['matched_to']} via {e['resolution_path']} "
                f"(rule: {e.get('rule_name', 'N/A')}, "
                f"confidence: {e['confidence']})"
            )

        return f"Unknown record ID: {record_id}"

    def record_detail(self, record_id: str) -> str:
        """Return all audit trail entries for a record, formatted."""
        entries = self._audit_by_id.get(record_id, [])
        if not entries:
            # Check exceptions too
            exc = self._exception_by_id.get(record_id)
            if exc:
                return (
                    f"{record_id} ({exc['record_type']}):\n"
                    f"  Reason: {exc['reason']}"
                )
            return f"No records found for: {record_id}"

        lines = [f"{record_id} — {len(entries)} audit entries:"]
        for i, e in enumerate(entries, 1):
            lines.append(
                f"  [{i}] path={e['resolution_path']}, "
                f"rule={e.get('rule_name', '')}, "
                f"matched_to={e.get('matched_to', 'N/A')}, "
                f"confidence={e['confidence']}, "
                f"gl_category={e.get('gl_category', '')}"
            )
            lines.append(f"      detail: {e['detail']}")
        return "\n".join(lines)

    def count_by_gl_category(self) -> Dict[str, int]:
        """Aggregate classification audit entries by gl_category."""
        counter: Counter = Counter()
        for entry in self._audit:
            if entry["resolution_path"] == "classification":
                cat = entry.get("gl_category", "")
                if cat:
                    counter[cat] += 1
        return dict(counter.most_common())

    def count_by_rule(self) -> Dict[str, int]:
        """Aggregate match audit entries by rule_name."""
        counter: Counter = Counter()
        for entry in self._audit:
            if entry["resolution_path"] in ("rule", "llm"):
                rule = entry.get("rule_name", "")
                if rule:
                    counter[rule] += 1
        return dict(counter.most_common())

    def count_by_resolution_path(self) -> Dict[str, int]:
        """Aggregate match/exception audit entries by resolution_path."""
        counter: Counter = Counter()
        for entry in self._audit:
            path = entry["resolution_path"]
            # Skip classification entries (they're secondary annotations,
            # not resolution decisions)
            if path != "classification":
                counter[path] += 1
        return dict(counter.most_common())

    def summary(self) -> str:
        """Return the pipeline summary from exceptions.json."""
        s = self._exceptions.get("summary", {})
        path_counts = self.count_by_resolution_path()

        lines = [
            "Pipeline Summary:",
            f"  {s.get('internal_count', '?')} internal records",
            f"  {s.get('external_count', '?')} external records",
            f"  {s.get('matched_count', '?')} matched",
            f"  {s.get('unmatched_internal', '?')} unmatched internal",
            f"  {s.get('unmatched_external', '?')} unmatched external",
            f"  {s.get('parse_error_count', '?')} parse errors",
            "",
            "Resolution path breakdown:",
        ]
        for path, count in sorted(path_counts.items()):
            lines.append(f"  {path}: {count}")
        return "\n".join(lines)

    # ── Intent router ────────────────────────────────────────

    # Patterns checked in order; first match wins.
    _INTENT_PATTERNS = [
        # "why didn't TXN_007 match" / "why was EXT_050 unmatched"
        (
            re.compile(
                r"\b(?:why|how)\b.+?(TXN_\d+|EXT_\d+)",
                re.IGNORECASE,
            ),
            "why_unmatched",
        ),
        # "show me TXN_001" / "detail for EXT_020" / "what happened to TXN_007"
        (
            re.compile(
                r"(?:detail|show|tell|what|info).+?(TXN_\d+|EXT_\d+)",
                re.IGNORECASE,
            ),
            "record_detail",
        ),
        # "breakdown by GL category" / "how many per category"
        (
            re.compile(
                r"(?:count|how many|breakdown|distribution).+?"
                r"(?:categor|gl|classification)",
                re.IGNORECASE,
            ),
            "count_by_gl_category",
        ),
        # "breakdown by rule" / "which rules fired"
        (
            re.compile(
                r"(?:count|how many|breakdown|distribution|which).+?"
                r"(?:rule|matcher)",
                re.IGNORECASE,
            ),
            "count_by_rule",
        ),
        # "breakdown by resolution path" / "how many matched vs exceptions"
        (
            re.compile(
                r"(?:count|how many|breakdown|distribution).+?"
                r"(?:path|resolution|matched|exception)",
                re.IGNORECASE,
            ),
            "count_by_resolution_path",
        ),
        # "summary" / "overview" / "status"
        (
            re.compile(
                r"\b(?:summary|overview|status)\b",
                re.IGNORECASE,
            ),
            "summary",
        ),
    ]

    def answer(self, question: str, *, api_key: Optional[str] = None) -> str:
        """Answer a question about the reconciliation results.

        Tries deterministic intent matching first.  Falls back to LLM
        with grounded context if no pattern matches.
        """
        question = question.strip()
        if not question:
            return "Please provide a question."

        # ── Deterministic intent matching ────────────────────
        for pattern, method_name in self._INTENT_PATTERNS:
            m = pattern.search(question)
            if m:
                if method_name in ("why_unmatched", "record_detail"):
                    record_id = m.group(1)
                    return getattr(self, method_name)(record_id)
                elif method_name in ("count_by_gl_category", "count_by_rule",
                                     "count_by_resolution_path"):
                    counts = getattr(self, method_name)()
                    return self._format_counts(counts, method_name)
                else:
                    return getattr(self, method_name)()

        # ── LLM fallback (grounded) ──────────────────────────
        return self._llm_fallback(question, api_key=api_key)

    # ── Helpers ──────────────────────────────────────────────

    @staticmethod
    def _format_counts(counts: Dict[str, int], label: str) -> str:
        """Format a count dict for human display."""
        total = sum(counts.values())
        header = label.replace("count_by_", "").replace("_", " ").title()
        lines = [f"{header} breakdown ({total} total):"]
        for key, count in counts.items():
            lines.append(f"  {key}: {count}")
        return "\n".join(lines)

    def _llm_fallback(
        self,
        question: str,
        *,
        api_key: Optional[str] = None,
    ) -> str:
        """LLM fallback: answer using only the provided structured data.

        Extracts any record IDs mentioned in the question, gathers their
        audit entries as context, and sends to Gemini with strict grounding
        instructions.
        """
        key = (
            api_key
            or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
        )
        if not key:
            return (
                "I can only answer structured queries without an API key. "
                "Try: 'Why didn't TXN_007 match?', 'Breakdown by GL category', "
                "or 'Summary'."
            )

        # Gather context: any TXN_/EXT_ IDs mentioned → their audit entries
        mentioned_ids = re.findall(r"(TXN_\d+|EXT_\d+)", question, re.IGNORECASE)
        context_parts = []
        if mentioned_ids:
            for rid in mentioned_ids:
                rid = rid.upper()
                entries = self._audit_by_id.get(rid, [])
                exc = self._exception_by_id.get(rid)
                if entries:
                    context_parts.append(
                        f"Audit entries for {rid}:\n"
                        + json.dumps(entries, indent=2)
                    )
                if exc:
                    context_parts.append(
                        f"Exception for {rid}:\n"
                        + json.dumps(exc, indent=2)
                    )
        if not context_parts:
            # No specific IDs mentioned — provide the summary
            context_parts.append(
                "Pipeline summary:\n"
                + json.dumps(self._exceptions.get("summary", {}), indent=2)
            )

        context = "\n\n".join(context_parts)

        system_prompt = (
            "You are a financial reconciliation assistant. "
            "Answer the user's question ONLY based on the provided data. "
            "Do NOT invent facts, speculate, or use knowledge not present "
            "in the context below. If the data doesn't contain the answer, "
            "say so explicitly."
        )
        user_prompt = (
            f"DATA CONTEXT:\n{context}\n\n"
            f"QUESTION: {question}"
        )

        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=key)
            response = client.models.generate_content(
                model="gemini-2.0-flash",
                contents=user_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=0.0,
                ),
            )
            return response.text or "No response from LLM."
        except Exception as e:
            return f"LLM fallback error: {e}"
"""
Post-pipeline Q&A layer. Provides deterministic lookups, aggregation queries,
and a grounded LLM fallback — all reading from pre-computed pipeline outputs.
"""
