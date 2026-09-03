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