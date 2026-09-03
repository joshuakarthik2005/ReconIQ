"""
Property-based invariant tests for the deterministic matcher.

test_deterministic.py proves one hand-built Hall's-condition case is handled
correctly. This file complements it with Hypothesis: instead of one crafted
scenario, it throws hundreds of randomly generated small internal/external
sets — many deliberately ambiguous (shared amounts, shared dates, reused or
missing ref IDs, so double-claim opportunities are common, not rare) — at
run_deterministic_matching() and checks the invariants that must hold no
matter what the input looks like:

  1. No external record is claimed by more than one match.
  2. No internal record is matched more than once.
  3. Every internal record ends up in exactly one of {matched, residual}.
  4. Every external record ends up in exactly one of {claimed, residual}.

These are exactly the invariants the greedy-claiming -> Hungarian-algorithm
rewrite (see deterministic_matcher.py's _optimal_assign_tier) exists to
guarantee. A regression back to greedy, sequential, or otherwise
order-dependent claiming would very likely be caught here even in cases the
single hand-written adversarial test doesn't happen to cover.
"""

from decimal import Decimal

from hypothesis import given, settings, strategies as st

from src.deterministic_matcher import run_deterministic_matching
from src.schemas import ExternalTransaction, InternalTransaction

# Small, deliberately overlapping pools -- the point is to make collisions
# (shared amount+date, reused ref, missing ref) common, not rare, since
# that's where claiming bugs live.
_AMOUNTS = [Decimal(a) for a in ("100.00", "250.50", "999.99", "50.00")]
_DATES = ["2026-01-01", "2026-01-02", "2026-01-03"]
_REFS = ["ref_a", "ref_b", "ref_c", ""]


def _make_internal(i: int, ref: str, amount: Decimal, date: str, txn_type: str) -> InternalTransaction:
    return InternalTransaction(
        txn_id=f"TXN_{i}",
        reference_id=ref,
        amount=amount,
        currency="INR",
        txn_type=txn_type,
        date=date,
        merchant_name="Test Merchant",
        merchant_category="tech",
        payment_method="upi",
        status="captured",
    )


def _make_external(i: int, ref, amount: Decimal, date: str) -> ExternalTransaction:
    return ExternalTransaction(
        ext_id=f"EXT_{i}",
        reference_id=ref or None,
        amount=amount,
        date=date,
        description="",
        source_format="A",
    )


@st.composite
def _internal_list(draw, n):
    return [
        _make_internal(
            i,
            draw(st.sampled_from(_REFS)),
            draw(st.sampled_from(_AMOUNTS)),
            draw(st.sampled_from(_DATES)),
            draw(st.sampled_from(["payment", "settlement", "refund"])),
        )
        for i in range(n)
    ]


@st.composite
def _external_list(draw, n):
    return [
        _make_external(
            i,
            draw(st.sampled_from(_REFS)),
            draw(st.sampled_from(_AMOUNTS)),
            draw(st.sampled_from(_DATES)),
        )
        for i in range(n)
    ]


@given(n_int=st.integers(0, 6), n_ext=st.integers(0, 6), data=st.data())
@settings(max_examples=150, deadline=None)
def test_matching_never_double_claims_and_fully_partitions(n_int, n_ext, data):
    internals = data.draw(_internal_list(n_int))
    externals = data.draw(_external_list(n_ext))

    output = run_deterministic_matching(internals, externals)

    matched_ext_ids = [m.external_id for m in output.matched]
    matched_int_ids = [m.internal_id for m in output.matched]

    # Invariant 1 & 2: no double-claim on either side.
    assert len(matched_ext_ids) == len(set(matched_ext_ids)), (
        f"same external claimed twice: {matched_ext_ids}"
    )
    assert len(matched_int_ids) == len(set(matched_int_ids)), (
        f"same internal matched twice: {matched_int_ids}"
    )

    # Invariant 3: every internal is matched XOR residual, never both/neither.
    all_int_ids = {t.txn_id for t in internals}
    residual_int_ids = {t.txn_id for t in output.residual_internal}
    matched_int_set = set(matched_int_ids)
    assert matched_int_set | residual_int_ids == all_int_ids
    assert matched_int_set & residual_int_ids == set()

    # Invariant 4: every external is claimed XOR residual, never both/neither.
    all_ext_ids = {e.ext_id for e in externals}
    residual_ext_ids = {e.ext_id for e in output.residual_external}
    matched_ext_set = set(matched_ext_ids)
    assert matched_ext_set | residual_ext_ids == all_ext_ids
    assert matched_ext_set & residual_ext_ids == set()
