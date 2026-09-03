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