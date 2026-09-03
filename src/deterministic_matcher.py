"""
Deterministic Matching Engine (fast path)
=========================================

Two-tier reconciliation -- this module is Tier 1 (rule-based).
Only the *residual* (unresolved records) passes to the Tier 2 LLM matcher.

Rules are fired in order for each internal record; first match wins.
Each external record can only be claimed once.

Rules
-----
1. exact_ref_amount_date  -- ref + exact amount + exact date
2. exact_ref_amount_window -- ref + exact amount + date within N days
3. ref_fee_tolerance      -- ref + amount within T% + date within N days
4. amount_date_unique     -- no ref needed, but amount+date must be
                             unique in BOTH directions (internal and external)

Design constraints
------------------
- Partial refunds are NOT caught here (amount diff 25-55%) -- they require
  combinatorial netting reasoning and go straight to LLM residual.
- No rule inspects description/narrative text -- that's reserved for the LLM.
- No rule uses merchant name -- prevents overfitting to this dataset.
- The tolerance band (Rule 3) ONLY applies when ref_id already matches.
"""

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Set, Tuple

from .schemas import (
    ExternalTransaction,
    InternalTransaction,
    MatchPath,
    MatchResult,
)


@dataclass
class MatchingOutput:
    """Complete output of deterministic matching."""
    matched: List[MatchResult] = field(default_factory=list)
    residual_internal: List[InternalTransaction] = field(default_factory=list)
    residual_external: List[ExternalTransaction] = field(default_factory=list)
    stats: Dict[str, int] = field(default_factory=dict)
    elapsed_seconds: float = 0.0


# ── Index structures ─────────────────────────────────────────

@dataclass
class _ExternalIndex:
    """Pre-built lookup indexes over external records."""
    by_ref: Dict[str, List[ExternalTransaction]]                # ref_id -> [ext]
    by_amount_date: Dict[Tuple[Decimal, str], List[ExternalTransaction]]  # (amount, date) -> [ext]

    @staticmethod
    def build(externals: List[ExternalTransaction]) -> "_ExternalIndex":
        by_ref: Dict[str, List[ExternalTransaction]] = {}
        by_ad: Dict[Tuple[Decimal, str], List[ExternalTransaction]] = {}

        for ext in externals:
            if ext.reference_id:
                by_ref.setdefault(ext.reference_id, []).append(ext)
            by_ad.setdefault((ext.amount, ext.date), []).append(ext)

        return _ExternalIndex(by_ref=by_ref, by_amount_date=by_ad)


@dataclass
class _InternalIndex:
    """Pre-built lookup indexes over internal records."""
    by_amount_date: Dict[Tuple[Decimal, str], List[InternalTransaction]]

    @staticmethod
    def build(internals: List[InternalTransaction]) -> "_InternalIndex":
        by_ad: Dict[Tuple[Decimal, str], List[InternalTransaction]] = {}
        for txn in internals:
            by_ad.setdefault((txn.amount, txn.date), []).append(txn)
        return _InternalIndex(by_amount_date=by_ad)


# ── Rule implementations ─────────────────────────────────────

def _date_within_window(d1: str, d2: str, window_days: int) -> bool:
    """Check if two YYYY-MM-DD dates are within *window_days* of each other."""
    dt1 = datetime.strptime(d1, "%Y-%m-%d")
    dt2 = datetime.strptime(d2, "%Y-%m-%d")
    return abs((dt1 - dt2).days) <= window_days


def _amount_within_tolerance(
    a: Decimal, b: Decimal, tolerance_pct: Decimal
) -> bool:
    """Check if *b* is within *tolerance_pct* % of *a* (relative to *a*)."""
    if a == 0:
        return b == 0
    return abs(a - b) / abs(a) <= tolerance_pct / Decimal("100")


def _try_rule_1(
    txn: InternalTransaction,
    ext_index: _ExternalIndex,
    claimed: Set[str],
) -> Optional[MatchResult]:
    """Rule 1: exact ref + exact amount + exact date."""
    candidates = ext_index.by_ref.get(txn.reference_id, [])
    for ext in candidates:
        if ext.ext_id in claimed:
            continue
        if ext.amount == txn.amount and ext.date == txn.date:
            return MatchResult(
                internal_id=txn.txn_id,
                external_id=ext.ext_id,
                match_path=MatchPath.RULE,
                confidence=1.0,
                rule_name="exact_ref_amount_date",
                reasoning=(
                    f"Exact match: ref={txn.reference_id}, "
                    f"amount={txn.amount}, date={txn.date}"
                ),
                timestamp=datetime.now().isoformat(),
            )
    return None


def _try_rule_2(
    txn: InternalTransaction,
    ext_index: _ExternalIndex,
    claimed: Set[str],
    date_window: int,
) -> Optional[MatchResult]:
    """Rule 2: exact ref + exact amount + date within window."""
    candidates = ext_index.by_ref.get(txn.reference_id, [])
    for ext in candidates:
        if ext.ext_id in claimed:
            continue
        if ext.amount == txn.amount and _date_within_window(
            txn.date, ext.date, date_window
        ):
            return MatchResult(
                internal_id=txn.txn_id,
                external_id=ext.ext_id,
                match_path=MatchPath.RULE,
                confidence=1.0,
                rule_name="exact_ref_amount_window",
                reasoning=(
                    f"Ref match with date drift: ref={txn.reference_id}, "
                    f"amount={txn.amount}, "
                    f"int_date={txn.date}, ext_date={ext.date}"
                ),
                timestamp=datetime.now().isoformat(),
            )
    return None


def _try_rule_3(
    txn: InternalTransaction,
    ext_index: _ExternalIndex,
    claimed: Set[str],
    fee_tolerance_pct: Decimal,
    date_window: int,
) -> Optional[MatchResult]:
    """Rule 3: ref match + amount within tolerance + date within window."""
    candidates = ext_index.by_ref.get(txn.reference_id, [])
    for ext in candidates:
        if ext.ext_id in claimed:
            continue
        if (
            _amount_within_tolerance(txn.amount, ext.amount, fee_tolerance_pct)
            and _date_within_window(txn.date, ext.date, date_window)
        ):
            diff_pct = (
                abs(txn.amount - ext.amount) / abs(txn.amount) * 100
                if txn.amount != 0
                else Decimal("0")
            )
            return MatchResult(
                internal_id=txn.txn_id,
                external_id=ext.ext_id,
                match_path=MatchPath.RULE,
                confidence=1.0,
                rule_name="ref_fee_tolerance",
                reasoning=(
                    f"Ref match with fee/rounding difference: "
                    f"ref={txn.reference_id}, "
                    f"int_amount={txn.amount}, ext_amount={ext.amount}, "
                    f"diff={diff_pct:.2f}%, "
                    f"int_date={txn.date}, ext_date={ext.date}"
                ),
                timestamp=datetime.now().isoformat(),
            )
    return None


def _try_rule_4(
    txn: InternalTransaction,