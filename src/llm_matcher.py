"""
LLM-Assisted Matching (Tier 2)
==============================

Handles the residual records that the deterministic matcher could not resolve.
Uses Gemini 2.5 Flash to reason over candidate shortlists.

Key design choices:
  - Explicit "no confident match" option in every prompt
  - Bounded shortlists (max 5 candidates per record)
  - Group assignment for duplicate_amount pairs (resolved FIRST,
    before any individual shortlists are built)
  - Code-level double-claim and confidence-threshold validation
    on every LLM response (never trust the prompt instruction alone)
  - Generic-term stoplist for description-overlap shortlisting

Sequencing
----------
  Phase 1 — detect ambiguity groups (same amount+date in residual pool),
            resolve via joint LLM call, claim externals
  Phase 2 — build individual shortlists (with updated claimed set),
            resolve via single LLM calls
"""

import json
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Set, Tuple

from google import genai
from google.genai import types

from .schemas import (
    ExternalTransaction,
    InternalTransaction,
    MatchPath,
    MatchResult,
)


# ── Configuration ────────────────────────────────────────────

MODEL = "gemini-3.6-flash"  # gemini-2.5-flash is 404 for this key

# confidence_threshold = 0.7: provisional — high enough to reject random guesses
# (LLM tends to output 0.3-0.5 for non-matches) but low enough to accept
# partial-refund matches where the LLM is reasonably confident.
DEFAULT_CONFIDENCE_THRESHOLD = 0.7

# max_candidates = 5: provisional — keeps prompt length manageable (~500 tokens
# per candidate x 5 = 2500 tokens) while covering the full candidate space
# for our residual pool sizes (10 residual externals).
DEFAULT_MAX_CANDIDATES = 5

# Stoplist: generic terms that appear in nearly every transaction and provide
# no discriminative signal for description-overlap shortlisting.
_DESCRIPTION_STOPWORDS = frozenset({
    "payment", "payments", "settlement", "settlements",
    "razorpay", "transaction", "transactions", "transfer",
    "credit", "debit", "amount", "charges", "charge",
    "processing", "net", "bank", "account",
    "refund", "refunds", "online", "india",
    "pvt", "ltd", "private", "limited", "services",
    "the", "and", "for", "from", "of", "to", "in", "on",
    "software", "fee", "fees", "gst", "tax",
    "current", "after", "partial",
})


# ── Output dataclass ─────────────────────────────────────────

@dataclass
class LLMMatchingOutput:
    """Complete output of LLM-assisted matching."""
    matched: List[MatchResult] = field(default_factory=list)
    exceptions_internal: List[str] = field(default_factory=list)
    exceptions_external: List[str] = field(default_factory=list)
    llm_calls: int = 0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    elapsed_seconds: float = 0.0


# ── Shortlist Building ───────────────────────────────────────

def _distinctive_words(text: str) -> set:
    """Extract distinctive words (lowercase, >=4 chars, not in stoplist)."""
    words = set(re.findall(r"[a-zA-Z]{4,}", text.lower()))
    return words - _DESCRIPTION_STOPWORDS


def _ref_similar(ref_a: Optional[str], ref_b: Optional[str]) -> bool:
    """Check if two refs are similar (substring or common-prefix match)."""
    if not ref_a or not ref_b:
        return False
    a, b = ref_a.strip().lower(), ref_b.strip().lower()
    if a in b or b in a:
        return True
    # Common prefix of at least 6 chars
    prefix_len = min(len(a), len(b))
    if prefix_len >= 6 and a[:prefix_len] == b[:prefix_len]:
        return True
    return False


def _build_candidate_shortlist(
    txn: InternalTransaction,
    residual_external: List[ExternalTransaction],
    claimed: Set[str],
    max_candidates: int,
) -> List[ExternalTransaction]:
    """
    Build a bounded shortlist of plausible candidates.

    Criteria (OR — any one qualifies a candidate):
      - Ref similarity (substring or prefix)
      - Amount within 60% (catches partial refunds at 25-55%)
      - Date within 7 days
      - Distinctive description-word overlap (stoplist-filtered)
    """
    scored: List[Tuple[int, ExternalTransaction]] = []
    txn_words = _distinctive_words(txn.description + " " + txn.merchant_name)

    for ext in residual_external:
        if ext.ext_id in claimed:
            continue

        score = 0

        # Ref similarity
        if _ref_similar(txn.reference_id, ext.reference_id):
            score += 3

        # Amount within 60%
        if txn.amount != 0:
            pct = abs(txn.amount - ext.amount) / abs(txn.amount)
            if pct <= Decimal("0.60"):
                score += 2

        # Date within 7 days
        try:
            d1 = datetime.strptime(txn.date, "%Y-%m-%d")
            d2 = datetime.strptime(ext.date, "%Y-%m-%d")
            if abs((d1 - d2).days) <= 7:
                score += 2
        except ValueError:
            pass

        # Distinctive description overlap
        ext_words = _distinctive_words(
            ext.description + " " + ext.raw_description
        )
        overlap = txn_words & ext_words
        if overlap:
            score += len(overlap)

        if score > 0:
            scored.append((score, ext))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [ext for _, ext in scored[:max_candidates]]


# ── Group Detection ──────────────────────────────────────────

@dataclass
class _AmbiguityGroup:
    """A set of residual internals competing for the same external candidates."""
    internals: List[InternalTransaction]
    candidates: List[ExternalTransaction]


def _detect_ambiguity_groups(
    residual_internal: List[InternalTransaction],
    residual_external: List[ExternalTransaction],
    claimed: Set[str],
) -> Tuple[List[_AmbiguityGroup], Set[str]]:
    """
    Detect groups of residual internals that share candidate externals
    by (amount, date).

    Returns (groups, set_of_grouped_internal_ids).
    """
    ext_by_ad: Dict[Tuple[Decimal, str], List[ExternalTransaction]] = defaultdict(list)
    for ext in residual_external:
        if ext.ext_id not in claimed:
            ext_by_ad[(ext.amount, ext.date)].append(ext)

    int_by_ad: Dict[Tuple[Decimal, str], List[InternalTransaction]] = defaultdict(list)
    for txn in residual_internal:
        key = (txn.amount, txn.date)
        if key in ext_by_ad:
            int_by_ad[key].append(txn)

    groups = []
    grouped_ids: Set[str] = set()
    for key, int_txns in int_by_ad.items():
        ext_cands = [e for e in ext_by_ad[key] if e.ext_id not in claimed]
        if len(int_txns) >= 2 and len(ext_cands) >= 1:
            groups.append(_AmbiguityGroup(internals=int_txns, candidates=ext_cands))
            for t in int_txns:
                grouped_ids.add(t.txn_id)

    return groups, grouped_ids


# ── Prompt Construction ──────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a financial reconciliation specialist matching internal payment \
system records against bank statement entries.

Domain knowledge:
- Payment gateway fees (1.5-3%) cause bank amounts to be slightly less \
than internal amounts.
- Partial refunds show as significantly smaller amounts on the bank side \
(the bank entry is the NET after subtracting the refund from the original).
- Settlement dates may differ by 1-3 business days.
- Reference IDs may be truncated, prefixed (NEFT/, IMPS/), or absent.
- Merchant names may appear in different formats.

CRITICAL: If NO candidate is a confident match, you MUST return null. \
Do NOT force a match when evidence is weak."""


def _fmt_internal(txn: InternalTransaction, label: str = "") -> str:
    prefix = f"[{label}] " if label else ""
    return (
        f"{prefix}ID: {txn.txn_id}\n"
        f"{prefix}Ref: {txn.reference_id}\n"
        f"{prefix}Amount: {txn.amount} {txn.currency}\n"
        f"{prefix}Date: {txn.date}\n"
        f"{prefix}Type: {txn.txn_type}\n"
        f"{prefix}Merchant: {txn.merchant_name} ({txn.merchant_category})\n"
        f"{prefix}Method: {txn.payment_method}\n"
        f"{prefix}Description: {txn.description}"
    )


def _fmt_external(ext: ExternalTransaction, index: int) -> str:
    ref = ext.reference_id or "(none)"
    desc = ext.raw_description or ext.description or "(none)"
    return (
        f"  [{index}] ID: {ext.ext_id}, Ref: {ref}, "
        f"Amount: {ext.amount}, Date: {ext.date}, "
        f"Format: {ext.source_format}, Description: {desc}"
    )


def _single_prompt(
    txn: InternalTransaction,
    candidates: List[ExternalTransaction],
) -> str:
    cands = "\n".join(_fmt_external(c, i + 1) for i, c in enumerate(candidates))
    return (
        f"INTERNAL RECORD:\n{_fmt_internal(txn)}\n\n"
        f"CANDIDATE EXTERNAL RECORDS:\n{cands}\n"
        f"  [NONE] -- No confident match exists\n\n"
        f"Which candidate (if any) matches this internal record?\n\n"
        f"Respond with JSON:\n"
        f'{{"match_index": <1-based integer or null if NONE>, '
        f'"confidence": <0.0 to 1.0>, '
        f'"is_partial_refund": <true if the bank amount is net after a partial refund deduction, false otherwise>, '
        f'"reasoning": "<brief explanation>"}}'
    )


def _group_prompt(group: _AmbiguityGroup) -> str:
    int_blocks = "\n\n".join(
        _fmt_internal(txn, chr(65 + i))
        for i, txn in enumerate(group.internals)
    )
    cands = "\n".join(
        _fmt_external(c, i + 1) for i, c in enumerate(group.candidates)
    )
    labels = ", ".join(chr(65 + i) for i in range(len(group.internals)))
    return (
        f"GROUP ASSIGNMENT TASK\n"
        f"Assign each internal record to exactly one external, or NONE.\n"
        f"Each external can be assigned to AT MOST one internal.\n\n"
        f"INTERNAL RECORDS:\n{int_blocks}\n\n"
        f"CANDIDATE EXTERNAL RECORDS:\n{cands}\n"
        f"  [NONE] -- No confident match\n\n"
        f"Respond with JSON:\n"
        f'{{"assignments": [\n'
        f'  {{"internal_label": "<one of {labels}>", '
        f'"match_index": <1-based or null>, '
        f'"confidence": <0.0-1.0>, '
        f'"is_partial_refund": <true/false>, '
        f'"reasoning": "<explanation>"}},\n'
        f"  ... one entry per internal record\n"
        f"]}}"
    )


# Type alias for batch items: (internal_txn, its_candidates)
BatchItem = Tuple[InternalTransaction, List[ExternalTransaction]]


def _batch_prompt(items: List[BatchItem]) -> str:
    """
    Build a prompt for multiple records, each with its OWN scoped candidates.

    Unlike _group_prompt (shared pool), this keeps each record's shortlist
    separate so the LLM knows which candidates belong to which record.
    """
    blocks = []
    for i, (txn, cands) in enumerate(items):
        label = chr(65 + i)  # A, B, C, D
        record_block = _fmt_internal(txn, label)
        cand_lines = "\n".join(
            f"    [{label}{j+1}] ID: {c.ext_id}, Ref: {c.reference_id or '(none)'}, "
            f"Amount: {c.amount}, Date: {c.date}, "
            f"Format: {c.source_format}, "
            f"Description: {c.raw_description or c.description or '(none)'}"
            for j, c in enumerate(cands)
        )
        blocks.append(
            f"RECORD {label}:\n{record_block}\n"
            f"  Candidates for {label}:\n{cand_lines}\n"
            f"    [{label}0] NONE -- No confident match"
        )

    labels = ", ".join(chr(65 + i) for i in range(len(items)))
    all_blocks = "\n\n".join(blocks)
    return (
        f"BATCH MATCHING TASK\n"
        f"For each internal record, select the best match from ITS OWN candidates,\n"
        f"or NONE if no candidate is a confident match.\n"
        f"Each external ID can be assigned to at most one internal record.\n\n"
        f"{all_blocks}\n\n"
        f"Respond with JSON:\n"
        f'{{"matches": [\n'
        f'  {{"internal_label": "<one of {labels}>", '
        f'"match_label": "<e.g. A2 or A0 for NONE>", '
        f'"confidence": <0.0-1.0>, '
        f'"is_partial_refund": <true/false>, '
        f'"reasoning": "<explanation>"}},\n'
        f"  ... one entry per record\n"
        f"]}}")


def _parse_batch_response(
    raw: str,
    items: List[BatchItem],
    confidence_threshold: float,
) -> List[Tuple[str, Optional[str], float, str, bool]]:
    """
    Parse batch response with per-record fault tolerance.

    If one record's entry is malformed, the others are still processed.
    Returns list of (internal_id, ext_id | None, confidence, reasoning, is_partial_refund).
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return [
            (txn.txn_id, None, 0.0, f"JSON parse error: {raw[:200]}", False)
            for txn, _ in items
        ]

    matches_list = data.get("matches", data.get("assignments", []))

    # Build lookup: label -> (txn, candidates)
    label_to_item = {chr(65 + i): item for i, item in enumerate(items)}

    results: List[Tuple[str, Optional[str], float, str, bool]] = []
    claimed_in_batch: Set[str] = set()

    for entry in matches_list:
        try:
            label = str(entry.get("internal_label", "")).strip().upper()
            item = label_to_item.get(label)
            if not item:
                continue

            txn, cands = item
            match_label = str(entry.get("match_label", "")).strip().upper()
            confidence = float(entry.get("confidence", 0.0))
            reasoning = str(entry.get("reasoning", ""))
            is_partial_refund = bool(entry.get("is_partial_refund", False))

            ext_id = None

            # Parse match_label: "A2" means record A, candidate index 2
            # "A0" or label ending in 0 means NONE
            if match_label.endswith("0") or "NONE" in match_label.upper():
                pass  # explicit no-match
            elif len(match_label) >= 2:
                try:
                    idx = int(match_label[1:]) - 1  # A1 -> index 0
                    if 0 <= idx < len(cands):
                        candidate = cands[idx]
                        if confidence < confidence_threshold:
                            reasoning = (
                                f"Below threshold ({confidence:.2f} < "
                                f"{confidence_threshold}): {reasoning}"
                            )
                            is_partial_refund = False
                        elif candidate.ext_id in claimed_in_batch:
                            reasoning = (
                                f"Rejected: {candidate.ext_id} already assigned "
                                f"in this batch: {reasoning}"
                            )
                            is_partial_refund = False
                        else:
                            ext_id = candidate.ext_id
                            claimed_in_batch.add(ext_id)
                    else:
                        reasoning = f"Index {idx+1} out of range (max {len(cands)}): {reasoning}"
                        is_partial_refund = False
                except ValueError:
                    reasoning = f"Unparseable match_label '{match_label}': {reasoning}"
                    is_partial_refund = False
            # Also handle numeric match_index as fallback
            elif entry.get("match_index") is not None:
                try:
                    idx = int(entry["match_index"]) - 1
                    if 0 <= idx < len(cands):
                        candidate = cands[idx]
                        if confidence < confidence_threshold:
                            reasoning = f"Below threshold: {reasoning}"
                            is_partial_refund = False
                        elif candidate.ext_id in claimed_in_batch:
                            reasoning = f"Double-claim rejected: {reasoning}"
                            is_partial_refund = False
                        else:
                            ext_id = candidate.ext_id
                            claimed_in_batch.add(ext_id)
                except (ValueError, IndexError):
                    pass

            results.append((txn.txn_id, ext_id, confidence, reasoning, is_partial_refund))

        except Exception as e:
            # Per-record fault tolerance: skip this entry, don't crash batch
            # Try to extract which record it was for
            try:
                label = str(entry.get("internal_label", "?"))
                item = label_to_item.get(label.upper())
                if item:
                    results.append(
                        (item[0].txn_id, None, 0.0, f"Parse error: {e}", False)
                    )
            except Exception:
                pass

    # Handle records missing from the response
    responded_ids = {r[0] for r in results}
    for txn, _ in items:
        if txn.txn_id not in responded_ids:
            results.append(
                (txn.txn_id, None, 0.0, "Not included in LLM response", False)
            )

    return results



# ── LLM Interaction ──────────────────────────────────────────

def _call_llm(
    client: genai.Client,
    system: str,
    user: str,
    *,
    _last_call_time: List = [],  # mutable default for cross-call state
    max_retries: int = 5,
    min_interval: float = 13.0,  # 5 req/min free tier -> 12s + 1s buffer
) -> Tuple[str, int, int]:
    """
    Call Gemini with rate-limiting and retry on 429.

    Rate limit: free tier allows 5 requests/minute.
    We space calls by at least *min_interval* seconds and retry with