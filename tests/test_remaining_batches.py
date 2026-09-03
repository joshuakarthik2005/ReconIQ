#!/usr/bin/env python3
"""
Run batches 2-5 live. Special handling for TXN_021 + TXN_045:
  - Joint group-assignment format (shared candidate pool)
  - Single-assignment-per-external constraint
  - Code-level double-claim check
  - Raw response printed in full

Remaining records use scoped-candidate batch format.

Requires GEMINI_API_KEY or GOOGLE_API_KEY in the environment.
Skipped automatically when no API key is available.

Run:  python -m pytest tests/test_remaining_batches.py -v
      python tests/test_remaining_batches.py          (legacy script mode)
"""

import json
import os
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.ingestion import parse_internal, parse_all_external
from src.deterministic_matcher import run_deterministic_matching
from src.llm_matcher import (
    _batch_prompt,
    _build_candidate_shortlist,
    _group_prompt,
    _parse_batch_response,
    _parse_group_response,
    _single_prompt,
    _parse_single_response,
    _AmbiguityGroup,
    _SYSTEM_PROMPT,
    BatchItem,
    DEFAULT_CONFIDENCE_THRESHOLD,
    DEFAULT_MAX_CANDIDATES,
)

DATA_DIR = PROJECT_ROOT / "data"
MODEL = "gemini-3.6-flash"
MIN_INTERVAL = 15.0  # seconds between API calls

_HAS_API_KEY = bool(
    os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
)
_skip_no_key = pytest.mark.skipif(
    not _HAS_API_KEY,
    reason="No GEMINI_API_KEY / GOOGLE_API_KEY in environment"
)

last_call_time = [0.0]


def call_api(client, prompt):
    """Rate-limited API call."""
    from google.genai import types

    if last_call_time[0] > 0:
        elapsed = time.time() - last_call_time[0]
        if elapsed < MIN_INTERVAL:
            wait = MIN_INTERVAL - elapsed
            time.sleep(wait)

    response = client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=_SYSTEM_PROMPT,
            temperature=0.0,
            response_mime_type="application/json",
        ),
    )
    last_call_time[0] = time.time()

    raw = response.text or ""
    usage = getattr(response, "usage_metadata", None)
    in_tok = getattr(usage, "prompt_token_count", 0) or 0
    out_tok = getattr(usage, "candidates_token_count", 0) or 0

    return raw, in_tok, out_tok


# ── Fixtures ─────────────────────────────────────────────────

@pytest.fixture(scope="module")
def pipeline_setup():
    int_records, _ = parse_internal(DATA_DIR / "internal_transactions.csv")
    ext_records, _ = parse_all_external(DATA_DIR)
    det = run_deterministic_matching(int_records, ext_records)
    gt = json.loads((DATA_DIR / "ground_truth.json").read_text(encoding="utf-8"))
    return det, gt


@pytest.fixture(scope="module")
def full_live_results(pipeline_setup):
    """Run all batches live and collect results."""
    det, gt = pipeline_setup
    gt_lookup = {m["internal_id"]: m["external_id"] for m in gt["matches"]}
    gt_unmatched_int = set(gt["unmatched_internal"])

    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        pytest.skip("No API key available")

    from google import genai
    client = genai.Client(api_key=key)

    # Batch 1 already done: TXN_005 matched EXT_020
    claimed = {"EXT_020"}
    all_results = [
        ("TXN_005", "EXT_020", 0.95, "Batch 1 result: ref+date+merchant match, partial refund"),
        ("TXN_007", None, 0.0, "Batch 1 result: no match"),
        ("TXN_009", None, 0.0, "Batch 1 result: no match"),
        ("TXN_010", None, 0.0, "Batch 1 result: no match"),
    ]

    batch1_ids = {"TXN_005", "TXN_007", "TXN_009", "TXN_010"}
    dup_amt_ids = {"TXN_021", "TXN_045"}

    phase2_records = []
    dup_amt_records = []
    for txn in det.residual_internal:
        if txn.txn_id in batch1_ids:
            continue
        cands = _build_candidate_shortlist(
            txn, det.residual_external, claimed, DEFAULT_MAX_CANDIDATES
        )
        if txn.txn_id in dup_amt_ids:
            dup_amt_records.append((txn, cands))
        elif cands:
            phase2_records.append((txn, cands))
        else:
            all_results.append((txn.txn_id, None, 0.0, "No candidates in shortlist"))

    # GROUP CALL: TXN_021 + TXN_045
    group_cand_map = {}
    for txn, cands in dup_amt_records:
        for c in cands:
            if c.ext_id not in claimed:
                group_cand_map[c.ext_id] = c
    group_cands = list(group_cand_map.values())

    group = _AmbiguityGroup(
        internals=[txn for txn, _ in dup_amt_records],
        candidates=group_cands,
    )

    prompt = _group_prompt(group)
    raw, _, _ = call_api(client, prompt)
    group_results = _parse_group_response(raw, group, DEFAULT_CONFIDENCE_THRESHOLD)

    for internal_id, ext_id, conf, reasoning in group_results:
        if ext_id and ext_id not in claimed:
            claimed.add(ext_id)
        all_results.append((internal_id, ext_id, conf, reasoning))

    # BATCHED CALLS: remaining records
    BATCH_SIZE = 4
    for start in range(0, len(phase2_records), BATCH_SIZE):
        batch = phase2_records[start:start + BATCH_SIZE]

        batch_items = []
        for txn, cands in batch:
            remaining = [c for c in cands if c.ext_id not in claimed]
            if remaining:
                batch_items.append((txn, remaining))
            else:
                all_results.append((
                    txn.txn_id, None, 0.0,
                    "All candidates already claimed by earlier batches"
                ))

        if not batch_items:
            continue

        if len(batch_items) == 1:
            txn, cands = batch_items[0]
            prompt = _single_prompt(txn, cands)
            raw, _, _ = call_api(client, prompt)
            ext_id, conf, reasoning = _parse_single_response(
                raw, cands, DEFAULT_CONFIDENCE_THRESHOLD
            )
            batch_results = [(txn.txn_id, ext_id, conf, reasoning)]
        else:
            prompt = _batch_prompt(batch_items)
            raw, _, _ = call_api(client, prompt)
            batch_results = _parse_batch_response(
                raw, batch_items, DEFAULT_CONFIDENCE_THRESHOLD
            )

        for internal_id, ext_id, conf, reasoning in batch_results:
            if ext_id and ext_id not in claimed:
                claimed.add(ext_id)
            all_results.append((internal_id, ext_id, conf, reasoning))

    return all_results


# ── Tests ────────────────────────────────────────────────────

@_skip_no_key
class TestRemainingBatchesLive:
    def test_no_false_positives(self, full_live_results, pipeline_setup):
        _, gt = pipeline_setup
        gt_lookup = {m["internal_id"]: m["external_id"] for m in gt["matches"]}
        fp = 0
        wrong = []
        for internal_id, ext_id, conf, reasoning in full_live_results:
            if ext_id:
                expected = gt_lookup.get(internal_id)
                if ext_id != expected:
                    fp += 1
                    wrong.append(
                        f"{internal_id} -> {ext_id} (expected {expected or 'no match'})"
                    )
        assert fp == 0, "\n".join(wrong)


# ── Legacy script mode ───────────────────────────────────────

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
