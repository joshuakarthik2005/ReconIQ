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
from datetime import datetime
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
    ext_index: _ExternalIndex,
    int_index: _InternalIndex,
    claimed: Set[str],
) -> Optional[MatchResult]:
    """
    Rule 4: amount + date match, no ref needed, but unique in BOTH directions.

    - Exactly one unclaimed external with this (amount, date)
    - Exactly one internal with this (amount, date)
    If either side has multiple candidates, the match is ambiguous -> skip.
    """
    key = (txn.amount, txn.date)

    # Check internal-side uniqueness
    int_candidates = int_index.by_amount_date.get(key, [])
    if len(int_candidates) != 1:
        return None     # ambiguous on internal side

    # Check external-side uniqueness (excluding already-claimed)
    ext_candidates = ext_index.by_amount_date.get(key, [])
    unclaimed = [e for e in ext_candidates if e.ext_id not in claimed]
    if len(unclaimed) != 1:
        return None     # ambiguous or no match on external side

    ext = unclaimed[0]
    return MatchResult(
        internal_id=txn.txn_id,
        external_id=ext.ext_id,
        match_path=MatchPath.RULE,
        confidence=0.95,
        rule_name="amount_date_unique",
        reasoning=(
            f"Amount+date match, bidirectionally unique: "
            f"amount={txn.amount}, date={txn.date}, "
            f"no ref on external (ext has ref={ext.reference_id!r})"
        ),
        timestamp=datetime.now().isoformat(),
    )


# ═══════════════════════════════════════════════════════════════
# Optimal assignment (Hungarian algorithm)
# ═══════════════════════════════════════════════════════════════

def _ext_id(c: MatchResult) -> str:
    """Extract a candidate's external_id as a guaranteed non-None str.

    MatchResult.external_id is Optional at the schema level -- exception
    records legitimately carry none. But every candidate fed into optimal
    assignment is supposed to already represent a real proposed pairing,
    so None here means an upstream contract was violated. Fail loudly
    instead of silently mis-keying the assignment.
    """
    if c.external_id is None:
        raise ValueError(
            f"candidate for internal_id={c.internal_id!r} has no "
            f"external_id -- only real candidate pairings should reach "
            f"optimal assignment"
        )
    return c.external_id


def _optimal_assign_tier(
    candidates: List[MatchResult],
) -> List[MatchResult]:
    """
    Given a list of candidate MatchResults (possibly many-to-many between
    internal and external IDs), find the maximum-cardinality 1:1 assignment
    using the Hungarian algorithm.

    Ties are broken by preferring higher confidence, then alphabetical
    internal_id for reproducibility.

    Returns the subset of *candidates* that form the optimal assignment.
    """
    if not candidates:
        return []

    # Deduplicate: for each (internal, external) pair, keep the best candidate
    best: Dict[Tuple[str, str], MatchResult] = {}
    for c in candidates:
        key = (c.internal_id, _ext_id(c))
        if key not in best or c.confidence > best[key].confidence:
            best[key] = c

    candidates = list(best.values())

    # Collect unique internal/external IDs
    int_ids = sorted({c.internal_id for c in candidates})
    ext_ids = sorted({_ext_id(c) for c in candidates})

    if len(int_ids) == 0 or len(ext_ids) == 0:
        return []

    # Fast path: if every internal has exactly one candidate external and
    # no external is contested, Hungarian is unnecessary
    int_to_ext: Dict[str, Set[str]] = {}
    ext_to_int: Dict[str, Set[str]] = {}
    for c in candidates:
        int_to_ext.setdefault(c.internal_id, set()).add(_ext_id(c))
        ext_to_int.setdefault(_ext_id(c), set()).add(c.internal_id)

    all_uncontested = all(
        len(exts) == 1 for exts in int_to_ext.values()
    ) and all(
        len(ints) == 1 for ints in ext_to_int.values()
    )

    if all_uncontested:
        # Each internal has exactly one external, no conflicts — take them all
        result_map: Dict[str, MatchResult] = {}
        for c in candidates:
            if c.internal_id not in result_map or c.confidence > result_map[c.internal_id].confidence:
                result_map[c.internal_id] = c
        return list(result_map.values())

    # Build cost matrix for Hungarian algorithm
    # scipy minimizes cost, so we use (1 - confidence) as cost.
    # Disallowed assignments get a large cost (BIG).
    from scipy.optimize import linear_sum_assignment
    import numpy as np

    int_idx = {iid: i for i, iid in enumerate(int_ids)}
    ext_idx = {eid: j for j, eid in enumerate(ext_ids)}

    BIG = 1e9
    n_int, n_ext = len(int_ids), len(ext_ids)
    cost = np.full((n_int, n_ext), BIG)

    cand_lookup: Dict[Tuple[int, int], MatchResult] = {}
    for c in candidates:
        i, j = int_idx[c.internal_id], ext_idx[_ext_id(c)]
        this_cost = 1.0 - c.confidence
        if cost[i, j] == BIG or this_cost < cost[i, j]:
            cost[i, j] = this_cost
            cand_lookup[(i, j)] = c

    row_ind, col_ind = linear_sum_assignment(cost)

    assigned: List[MatchResult] = []
    for i, j in zip(row_ind, col_ind):
        if cost[i, j] < BIG:
            assigned.append(cand_lookup[(i, j)])

    return assigned


def run_deterministic_matching(
    internals: List[InternalTransaction],
    externals: List[ExternalTransaction],
    *,
    # Fee tolerance tuned to this dataset's Razorpay gateway fees (1.5-2.5%).
    # Do not change without re-validating the 8 ref_fee_tolerance matches.
    fee_tolerance_pct: Decimal = Decimal("3"),
    date_window_days: int = 3,
) -> MatchingOutput:
    """
    Run the deterministic (rule-based) matching engine.

    Uses a tiered optimal assignment strategy:
    1. For each rule tier (in priority order), collect ALL candidate edges
       among unclaimed records.
    2. Use the Hungarian algorithm (scipy.optimize.linear_sum_assignment) to
       find the maximum-cardinality 1:1 assignment within that tier.
    3. Claim the optimal set, then proceed to the next tier with the remaining
       unclaimed records.

    This eliminates order-dependent starvation where an earlier internal
    grabs an external that a later internal needs more.

    Parameters
    ----------
    internals : parsed internal transactions
    externals : parsed external transactions (all formats combined)
    fee_tolerance_pct : maximum allowed percentage difference for fee/rounding
    date_window_days : maximum allowed date difference in calendar days

    Returns
    -------
    MatchingOutput with matched results, residual records, and stats.
    """
    t0 = time.perf_counter()

    ext_index = _ExternalIndex.build(externals)
    int_index = _InternalIndex.build(internals)
    claimed: Set[str] = set()
    matched_int_ids: Set[str] = set()
    matched: List[MatchResult] = []

    rule_counts = {
        "exact_ref_amount_date": 0,
        "exact_ref_amount_window": 0,
        "ref_fee_tolerance": 0,
        "amount_date_unique": 0,
    }

    # Define rule tiers: each tier is a function that takes
    # (txn, ext_index, int_index, claimed, params) and returns Optional[MatchResult]
    # We process tiers in order; within each tier, we collect all candidates
    # then use Hungarian to find the optimal assignment.

    def _collect_tier_candidates(
        rule_fn,
        unmatched_internals: List[InternalTransaction],
    ) -> List[MatchResult]:
        """Collect all candidate matches for a rule across unmatched internals."""
        candidates = []
        for txn in unmatched_internals:
            result = rule_fn(txn)
            if result is not None:
                candidates.append(result)
        return candidates

    def _collect_all_candidates_for_rule(
        rule_fn,
        unmatched_internals: List[InternalTransaction],
    ) -> List[MatchResult]:
        """Collect ALL candidate matches (not just first) for a rule.

        For rules 1-3 (ref-based), a single internal may match multiple
        unclaimed externals. We collect all of them so Hungarian can pick
        the optimal assignment.
        """
        candidates = []
        for txn in unmatched_internals:
            for cand in rule_fn(txn):
                candidates.append(cand)
        return candidates

    # Rule candidate generators: yield ALL matches (not just first)
    def _gen_rule_1(txn: InternalTransaction):
        cands = ext_index.by_ref.get(txn.reference_id, [])
        for ext in cands:
            if ext.ext_id in claimed:
                continue
            if ext.amount == txn.amount and ext.date == txn.date:
                yield MatchResult(
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

    def _gen_rule_2(txn: InternalTransaction):
        cands = ext_index.by_ref.get(txn.reference_id, [])
        for ext in cands:
            if ext.ext_id in claimed:
                continue
            if ext.amount == txn.amount and _date_within_window(
                txn.date, ext.date, date_window_days
            ):
                yield MatchResult(
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

    def _gen_rule_3(txn: InternalTransaction):
        cands = ext_index.by_ref.get(txn.reference_id, [])
        for ext in cands:
            if ext.ext_id in claimed:
                continue
            if (
                _amount_within_tolerance(txn.amount, ext.amount, fee_tolerance_pct)
                and _date_within_window(txn.date, ext.date, date_window_days)
            ):
                diff_pct = (
                    abs(txn.amount - ext.amount) / abs(txn.amount) * 100
                    if txn.amount != 0
                    else Decimal("0")
                )
                yield MatchResult(
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

    def _gen_rule_4(txn: InternalTransaction):
        key = (txn.amount, txn.date)
        int_candidates = int_index.by_amount_date.get(key, [])
        if len(int_candidates) != 1:
            return
        ext_candidates = ext_index.by_amount_date.get(key, [])
        unclaimed = [e for e in ext_candidates if e.ext_id not in claimed]
        if len(unclaimed) != 1:
            return
        ext = unclaimed[0]
        yield MatchResult(
            internal_id=txn.txn_id,
            external_id=ext.ext_id,
            match_path=MatchPath.RULE,
            confidence=0.95,
            rule_name="amount_date_unique",
            reasoning=(
                f"Amount+date match, bidirectionally unique: "
                f"amount={txn.amount}, date={txn.date}, "
                f"no ref on external (ext has ref={ext.reference_id!r})"
            ),
            timestamp=datetime.now().isoformat(),
        )

    # Process each rule tier in order
    rule_generators = [_gen_rule_1, _gen_rule_2, _gen_rule_3, _gen_rule_4]

    for gen_fn in rule_generators:
        # Collect all candidates for this tier among unmatched internals
        unmatched = [t for t in internals if t.txn_id not in matched_int_ids]
        tier_candidates = _collect_all_candidates_for_rule(gen_fn, unmatched)

        # Optimal assignment within this tier
        tier_assigned = _optimal_assign_tier(tier_candidates)

        for result in tier_assigned:
            matched.append(result)
            claimed.add(_ext_id(result))
            matched_int_ids.add(result.internal_id)
            # rule_name is Optional at the schema level, but every
            # _gen_rule_* generator always sets a real rule name -- a
            # candidate reaching here with none would mean a rule
            # generator regressed, so fail loudly rather than KeyError
            # on rule_counts or silently miscounting.
            if result.rule_name is None:
                raise ValueError(
                    f"matched result for internal_id={result.internal_id!r} "
                    f"has no rule_name -- every rule generator must set one"
                )
            rule_counts[result.rule_name] += 1

    # Residuals
    residual_internal = [t for t in internals if t.txn_id not in matched_int_ids]
    residual_external = [e for e in externals if e.ext_id not in claimed]

    elapsed = time.perf_counter() - t0

    stats = {
        **rule_counts,
        "total_matched": len(matched),
        "total_residual_internal": len(residual_internal),
        "total_residual_external": len(residual_external),
        "total_internal": len(internals),
        "total_external": len(externals),
    }

    return MatchingOutput(
        matched=matched,
        residual_internal=residual_internal,
        residual_external=residual_external,
        stats=stats,
        elapsed_seconds=elapsed,
    )

