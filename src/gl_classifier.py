"""
GL Classification (Part 4)
===========================

Classifies each matched reconciliation entry into chart-of-accounts-style
GL categories.  Two-tier pattern: deterministic rules first, LLM for
ambiguous residual only.

Two-phase approach within Tier 1:
  Phase A — Primary category (rule cascade, first match wins):
    1. refund_type         — txn_type == "refund" → REFUND
                             (+ partial note if reasoning mentions "partial refund")
    2. partial_refund_llm  — LLM match + "partial refund" in reasoning → REFUND
    3. clean_settlement    — catch-all payment/settlement → SETTLEMENT

  Phase B — Fee/tax sub-entry attachment (orthogonal, runs after Phase A):
    If match.rule_name == "ref_fee_tolerance", attach GATEWAY_FEE and
    TAX_ADJUSTMENT sub-entries regardless of primary category.
    This is decoupled from category selection because fee deductions can
    occur on any transaction type (e.g. TXN_020 is a refund WITH a fee).

LLM fallback:
  Any record not classified by the rules above gets sent to Gemini.
  Expected residual on this dataset: 0 records.

Limitation (stated in README):
  Rule 2's "partial refund" keyword check is verified on this dataset's
  2 records (TXN_005, TXN_031) but would need hardening (negation handling,
  multi-language) to generalise.
"""

import os
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List, Optional

from google import genai
from google.genai import types

from .schemas import (
    ExternalTransaction,
    GLCategory,
    InternalTransaction,
    MatchPath,
    MatchResult,
)


# ── Configuration ────────────────────────────────────────────

_TWO_DP = Decimal("0.01")

# Reuse Part 3's model config
MODEL = "gemini-3.6-flash"

_CLASSIFICATION_SYSTEM_PROMPT = """\
You are a financial controller classifying reconciled payment entries into \
General Ledger categories.

Available categories:
- SETTLEMENT: Standard payment settlement or credit (payment or settlement type)
- GATEWAY_FEE: Gateway processing fee deduction
- REFUND: Full or partial customer refund
- TAX_ADJUSTMENT: Tax component (GST/TDS) of a fee
- CHARGEBACK: Chargeback or dispute reversal
- BANK_CHARGE: Bank service charge
- INTEREST_INCOME: Interest credit
- MISCELLANEOUS: Does not fit any other category
- UNCLASSIFIED: Insufficient information to classify

Given the internal record, external record, and matching reasoning, \
classify into the most appropriate GL category.

Respond with JSON:
{"category": "<one of the category names above>", "reasoning": "<brief explanation>"}
"""


# ── Output dataclasses ───────────────────────────────────────

@dataclass
class GLSubEntry:
    """Sub-line-item within a classified entry (e.g., fee/tax split)."""
    category: GLCategory
    amount: Decimal
    description: str


@dataclass
class ClassifiedEntry:
    """A matched record with its GL classification.

    For fee-deduction records, sub_entries contains the GATEWAY_FEE and
    TAX_ADJUSTMENT line items.  These are nested, not flattened into
    additional top-level ClassifiedEntry objects.
    """
    match_result: MatchResult
    gl_category: GLCategory
    sub_entries: List[GLSubEntry] = field(default_factory=list)
    classification_path: str = ""   # "rule" or "llm"
    rule_name: str = ""             # which classification rule fired
    reasoning: str = ""


@dataclass
class ClassificationOutput:
    """Complete output of GL classification."""
    classified: List[ClassifiedEntry] = field(default_factory=list)
    llm_calls: int = 0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    elapsed_seconds: float = 0.0


# ── Fee / Tax split arithmetic ───────────────────────────────

# GST rate applied to gateway fees.  Tuned to this dataset's
# Razorpay settlement records — do not change without re-validating
# the fee-split tests and the 8 ref_fee_tolerance matches.
GST_RATE_PCT = Decimal("18")

_GST_INCLUSIVE_DIVISOR = Decimal("100") + GST_RATE_PCT   # 118


def compute_fee_tax_split(
    internal_amount: Decimal,
    external_amount: Decimal,
) -> tuple:
    """
    Compute gateway-fee and GST-on-fee sub-entries.

    Returns (gateway_fee, tax_adjustment) where:
      - total_fee = internal_amount - external_amount
      - gateway_fee = total_fee * 100 / (100 + GST_RATE_PCT), quantized
        to 2dp with ROUND_HALF_UP
      - tax_adjustment = total_fee - gateway_fee  (remainder — guarantees zero
        rounding leakage by construction)
    """
    total_fee = internal_amount - external_amount

    gateway_fee = (total_fee * Decimal("100") / _GST_INCLUSIVE_DIVISOR).quantize(
        _TWO_DP, rounding=ROUND_HALF_UP
    )
    tax_adjustment = total_fee - gateway_fee

    return gateway_fee, tax_adjustment



# ── Deterministic classification rules ───────────────────────

def _is_partial_refund_reasoning(match: MatchResult) -> bool:
    """Check if the match identifies a partial refund.

    Primary: structured ``is_partial_refund`` field on MatchResult, set by the
    LLM response parser when the LLM explicitly flags the match as a partial
    refund via the ``is_partial_refund`` JSON key.

    Fallback: keyword heuristic on free-text reasoning (legacy — kept for
    backward compatibility with canonical results that predate the field).
    Verified on this dataset (TXN_005, TXN_026, TXN_031, TXN_054).
    See README Known Limitations for caveats.
    """
    # Primary: structured bool field from LLM response
    if match.is_partial_refund:
        return True
    # Fallback: keyword heuristic (legacy — for canonical/pre-field records)
    return "partial refund" in match.reasoning.lower()


def _rule_1_refund_type(
    match: MatchResult,
    internal: InternalTransaction,
    external: ExternalTransaction,
) -> Optional[ClassifiedEntry]:
    """Rule 1: txn_type == 'refund' → REFUND.

    Also checks for 'partial refund' in reasoning to add a consistent
    partial-refund annotation (same as Rule 3 uses for non-refund txn_types).
    """
    if internal.txn_type != "refund":
        return None

    is_partial = _is_partial_refund_reasoning(match)
    note = " (partial refund — bank amount is net after refund)" if is_partial else ""

    return ClassifiedEntry(
        match_result=match,
        gl_category=GLCategory.REFUND,
        classification_path="rule",
        rule_name="refund_type",
        reasoning=f"txn_type=refund{note}",
    )


def _attach_fee_sub_entries(
    entry: ClassifiedEntry,
    internal: InternalTransaction,
    external: ExternalTransaction,
) -> None:
    """Phase B: attach GATEWAY_FEE + TAX_ADJUSTMENT sub-entries.

    Called after primary category selection, only when
    match.rule_name == 'ref_fee_tolerance'.  Mutates entry in place.

    This is orthogonal to category selection: a refund can have a fee
    deduction (TXN_020), and a settlement can have a fee deduction
    (the other 7 ref_fee_tolerance records).
    """
    gateway_fee, tax_adjustment = compute_fee_tax_split(
        internal.amount, external.amount
    )
    total_fee = internal.amount - external.amount

    entry.sub_entries = [
        GLSubEntry(
            category=GLCategory.GATEWAY_FEE,
            amount=gateway_fee,
            description=(
                f"Gateway fee (ex-GST): {gateway_fee} INR "
                f"on {internal.txn_type} of {internal.amount} INR"
            ),
        ),
        GLSubEntry(
            category=GLCategory.TAX_ADJUSTMENT,
            amount=tax_adjustment,
            description=(
                f"GST on gateway fee: {tax_adjustment} INR "
                f"(total fee incl. GST: {total_fee} INR)"
            ),
        ),
    ]
    entry.reasoning += (
        f". Fee deduction: {total_fee} INR "
        f"(gateway: {gateway_fee} + GST: {tax_adjustment})"
    )


def _rule_3_partial_refund_llm(
    match: MatchResult,
    internal: InternalTransaction,
    external: ExternalTransaction,
) -> Optional[ClassifiedEntry]:
    """Rule 3: LLM-matched + reasoning mentions 'partial refund' → REFUND.

    Catches partial-refund-noise records with non-refund txn_type
    (e.g., TXN_005=payment, TXN_031=settlement).  Rule 1 already catches
    the txn_type=refund cases, so this only fires for the remainder.
    """
    if match.match_path != MatchPath.LLM:
        return None
    if not _is_partial_refund_reasoning(match):
        return None

    return ClassifiedEntry(
        match_result=match,
        gl_category=GLCategory.REFUND,
        classification_path="rule",
        rule_name="partial_refund_llm",
        reasoning=(
            f"LLM match reasoning identifies partial refund "
            f"(partial refund — bank amount is net after refund). "
            f"txn_type={internal.txn_type}, classified as refund based on "
            f"reconciliation evidence"
        ),
    )


def _rule_4_clean_settlement(
    match: MatchResult,
    internal: InternalTransaction,
    external: ExternalTransaction,
) -> Optional[ClassifiedEntry]:
    """Rule 4: catch-all for payment/settlement → SETTLEMENT_REVENUE."""
    if internal.txn_type in ("payment", "settlement"):
        return ClassifiedEntry(
            match_result=match,
            gl_category=GLCategory.SETTLEMENT,
            classification_path="rule",
            rule_name="clean_settlement",
            reasoning=f"Standard {internal.txn_type} — no fee deduction or refund",
        )
    return None


# ── LLM classification fallback ──────────────────────────────

def _build_classification_prompt(
    match: MatchResult,
    internal: InternalTransaction,
    external: ExternalTransaction,
) -> str:
    """Build a prompt for LLM classification of a single record."""
    return (
        f"INTERNAL RECORD:\n"
        f"  ID: {internal.txn_id}\n"
        f"  Type: {internal.txn_type}\n"
        f"  Amount: {internal.amount} {internal.currency}\n"
        f"  Date: {internal.date}\n"
        f"  Merchant: {internal.merchant_name} ({internal.merchant_category})\n"
        f"  Method: {internal.payment_method}\n"
        f"  Description: {internal.description}\n\n"
        f"EXTERNAL RECORD:\n"
        f"  ID: {external.ext_id}\n"
        f"  Amount: {external.amount}\n"
        f"  Date: {external.date}\n"
        f"  Description: {external.description}\n"
        f"  Raw: {external.raw_description}\n\n"
        f"MATCHING DETAILS:\n"
        f"  Match path: {match.match_path.value}\n"
        f"  Rule: {match.rule_name}\n"
        f"  Confidence: {match.confidence}\n"
        f"  Reasoning: {match.reasoning}\n\n"
        f"What GL category should this entry be classified under?"
    )


def _build_batch_classification_prompt(
    items: list,
) -> str:
    """Build a batched prompt for LLM classification of multiple records.

    Reuses Part 3's scoped-candidate batching pattern — each record is
    labelled A, B, C, ... and the LLM returns per-record classifications.
    """
    blocks = []
    for i, (match, internal, external) in enumerate(items):
        label = chr(65 + i)
        blocks.append(
            f"RECORD {label}:\n"
            f"  Internal ID: {internal.txn_id}\n"
            f"  Type: {internal.txn_type}\n"
            f"  Amount: {internal.amount} {internal.currency}\n"
            f"  Date: {internal.date}\n"
            f"  Merchant: {internal.merchant_name} ({internal.merchant_category})\n"
            f"  Method: {internal.payment_method}\n"
            f"  Description: {internal.description}\n"
            f"  External Amount: {external.amount}\n"
            f"  External Date: {external.date}\n"
            f"  External Description: {external.description}\n"
            f"  Match path: {match.match_path.value}\n"
            f"  Match reasoning: {match.reasoning}"
        )

    labels = ", ".join(chr(65 + i) for i in range(len(items)))
    all_blocks = "\n\n".join(blocks)

    return (
        f"BATCH CLASSIFICATION TASK\n"
        f"Classify each record into a GL category.\n\n"
        f"{all_blocks}\n\n"
        f"Respond with JSON:\n"
        f'{{"classifications": [\n'
        f'  {{"label": "<one of {labels}>", '
        f'"category": "<GL category>", '
        f'"reasoning": "<explanation>"}},\n'
        f"  ... one entry per record\n"
        f"]}}"
    )


_GL_CATEGORY_MAP = {cat.name: cat for cat in GLCategory}


def _parse_classification_response(
    raw: str,
    items: list,
) -> List[ClassifiedEntry]:
    """Parse LLM classification response.

    Fault-tolerant: if one record's entry is malformed, others still process.
    Unclassified records get UNCLASSIFIED with an error reason.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return [
            ClassifiedEntry(
                match_result=match,
                gl_category=GLCategory.UNCLASSIFIED,
                classification_path="llm",
                rule_name="llm_classification",
                reasoning=f"JSON parse error: {raw[:200]}",
            )
            for match, _, _ in items
        ]

    classifications = data.get("classifications", [])
    label_to_item = {chr(65 + i): item for i, item in enumerate(items)}

    results = []
    responded_labels = set()

    for entry in classifications:
        try:
            label = str(entry.get("label", "")).strip().upper()
            item = label_to_item.get(label)
            if not item:
                continue

            match, internal, external = item
            cat_name = str(entry.get("category", "UNCLASSIFIED")).strip().upper()
            reasoning = str(entry.get("reasoning", ""))

            # Map category string to enum
            gl_cat = _GL_CATEGORY_MAP.get(cat_name, GLCategory.UNCLASSIFIED)

            results.append(ClassifiedEntry(
                match_result=match,
                gl_category=gl_cat,
                classification_path="llm",
                rule_name="llm_classification",
                reasoning=reasoning,
            ))
            responded_labels.add(label)

        except Exception as e:
            try:
                label = str(entry.get("label", "?")).strip().upper()
                item = label_to_item.get(label)
                if item:
                    results.append(ClassifiedEntry(
                        match_result=item[0],
                        gl_category=GLCategory.UNCLASSIFIED,
                        classification_path="llm",
                        rule_name="llm_classification",
                        reasoning=f"Parse error: {e}",
                    ))
                    responded_labels.add(label)
            except Exception:
                pass

    # Handle records missing from response
    for label, item in label_to_item.items():
        if label not in responded_labels:
            match, _, _ = item
            results.append(ClassifiedEntry(
                match_result=match,
                gl_category=GLCategory.UNCLASSIFIED,
                classification_path="llm",
                rule_name="llm_classification",
                reasoning="Not included in LLM response",
            ))

    return results


def _call_llm_classification(
    client,
    prompt: str,
    *,
    _last_call_time: list = [],
    min_interval: float = 13.0,
    max_retries: int = 5,
) -> tuple:
    """Call Gemini for classification with rate limiting.

    Same rate-limiting pattern as Part 3's _call_llm.
    """
    import sys

    if _last_call_time:
        elapsed = time.time() - _last_call_time[0]
        if elapsed < min_interval:
            wait = min_interval - elapsed
            print(f"    [rate-limit] waiting {wait:.1f}s...", file=sys.stderr)
            time.sleep(wait)

    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=_CLASSIFICATION_SYSTEM_PROMPT,
                    temperature=0.0,
                    response_mime_type="application/json",
                ),
            )
            _last_call_time[:] = [time.time()]

            text = response.text or ""
            usage = getattr(response, "usage_metadata", None)
            in_tok = getattr(usage, "prompt_token_count", 0) or 0
            out_tok = getattr(usage, "candidates_token_count", 0) or 0

            return text, in_tok, out_tok

        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                backoff = min_interval * (2 ** attempt)
                print(
                    f"    [retry {attempt+1}/{max_retries}] "
                    f"rate limited, waiting {backoff:.0f}s...",
                    file=sys.stderr,
                )
                time.sleep(backoff)
                _last_call_time[:] = [time.time()]
            else:
                raise

    raise RuntimeError(f"Failed after {max_retries} retries")


# ═══════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════

# Phase A: Primary category cascade (first match wins).
# Fee/tax sub-entries are attached in Phase B, after this cascade.
_CATEGORY_RULES = [
    _rule_1_refund_type,         # txn_type=refund
    _rule_3_partial_refund_llm,  # LLM reasoning keyword
    _rule_4_clean_settlement,    # Catch-all
]

BATCH_SIZE = 4  # Reuse Part 3's batch size for LLM calls


def run_gl_classification(
    matches: List[MatchResult],
    internals: List[InternalTransaction],
    externals: List[ExternalTransaction],
    *,
    api_key: Optional[str] = None,
) -> ClassificationOutput:
    """
    Classify all matched records into GL categories.

    Two-tier approach:
      Tier 1 — deterministic rules (expected to handle 100% of this dataset)
      Tier 2 — LLM fallback (safety net for future ambiguous cases)

    Returns ClassificationOutput with exactly len(matches) ClassifiedEntry
    objects — one per match, no flattening of sub-entries.
    """
    t0 = time.perf_counter()

    int_by_id: Dict[str, InternalTransaction] = {t.txn_id: t for t in internals}
    ext_by_id: Dict[str, ExternalTransaction] = {e.ext_id: e for e in externals}

    classified: List[ClassifiedEntry] = []
    llm_residual: list = []  # (match, internal, external)

    total_calls = 0
    total_in_tokens = 0
    total_out_tokens = 0

    # ── Tier 1: Deterministic rules ──────────────────────────
    for match in matches:
        internal = int_by_id.get(match.internal_id)
        external = ext_by_id.get(match.external_id)

        if not internal or not external:
            # Defensive: shouldn't happen, but don't silently drop
            classified.append(ClassifiedEntry(