#!/usr/bin/env python3
"""
Run ONLY batch 1 (first 4 residual records) against the live API.
Shows the raw LLM response for review before running remaining batches.

Requires GEMINI_API_KEY or GOOGLE_API_KEY in the environment.
Skipped automatically when no API key is available.

Run:  python -m pytest tests/test_batch1_live.py -v
      python tests/test_batch1_live.py          (legacy script mode)
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
    _parse_batch_response,
    _SYSTEM_PROMPT,
    DEFAULT_CONFIDENCE_THRESHOLD,
    DEFAULT_MAX_CANDIDATES,
    BatchItem,
)

DATA_DIR = PROJECT_ROOT / "data"

_HAS_API_KEY = bool(
    os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
)
_skip_no_key = pytest.mark.skipif(
    not _HAS_API_KEY,
    reason="No GEMINI_API_KEY / GOOGLE_API_KEY in environment"
)


# ── Fixtures ─────────────────────────────────────────────────

@pytest.fixture(scope="module")
def batch1_setup():
    int_records, _ = parse_internal(DATA_DIR / "internal_transactions.csv")
    ext_records, _ = parse_all_external(DATA_DIR)
    det = run_deterministic_matching(int_records, ext_records)
    gt = json.loads((DATA_DIR / "ground_truth.json").read_text(encoding="utf-8"))
    gt_lookup = {m["internal_id"]: m["external_id"] for m in gt["matches"]}

    # Build batch 1: first 4 residual records with candidates
    phase2_records = list(det.residual_internal)
    batch1_items = []
    for txn in phase2_records:
        cands = _build_candidate_shortlist(
            txn, det.residual_external, set(), DEFAULT_MAX_CANDIDATES
        )
        if cands:
            batch1_items.append((txn, cands))
        if len(batch1_items) >= 4:
            break

    return batch1_items, gt_lookup


@pytest.fixture(scope="module")
def batch1_results(batch1_setup):
    """Call live API and parse results."""
    batch1_items, _ = batch1_setup

    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        pytest.skip("No API key available")

    from google import genai
    from google.genai import types

    prompt = _batch_prompt(batch1_items)
    client = genai.Client(api_key=key)

    response = client.models.generate_content(
        model="gemini-3.6-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=_SYSTEM_PROMPT,
            temperature=0.0,
            response_mime_type="application/json",
        ),
    )

    raw = response.text or ""
    results = _parse_batch_response(raw, batch1_items, DEFAULT_CONFIDENCE_THRESHOLD)
    return results


# ── Tests ────────────────────────────────────────────────────

@_skip_no_key
class TestBatch1Live:
    def test_results_cover_all_batch_records(self, batch1_setup, batch1_results):
        batch1_items, _ = batch1_setup
        result_ids = {r[0] for r in batch1_results}
        input_ids = {txn.txn_id for txn, _ in batch1_items}
        assert result_ids == input_ids

    def test_no_false_positives(self, batch1_setup, batch1_results):
        _, gt_lookup = batch1_setup
        wrong = []
        for internal_id, ext_id, confidence, reasoning in batch1_results:
            if ext_id:
                expected = gt_lookup.get(internal_id)
                if ext_id != expected:
                    wrong.append(
                        f"{internal_id} -> {ext_id} (expected {expected})"
                    )
        assert len(wrong) == 0, "\n".join(wrong)


# ── Legacy script mode ───────────────────────────────────────

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
