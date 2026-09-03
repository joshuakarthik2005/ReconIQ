#!/usr/bin/env python3
"""
Mock-based LLM matcher test: validates full pipeline logic without API calls.

Simulates LLM responses to verify:
  - Correct sequencing (groups → individuals)
  - Code-level double-claim validation
  - Confidence threshold enforcement
  - Exception collection
  - Combined Part2+Part3 metrics against ground truth

Run:  python -m pytest tests/test_llm_mock.py -v
      python tests/test_llm_mock.py          (legacy script mode)
"""

import json
import re
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.ingestion import parse_internal, parse_all_external
from src.deterministic_matcher import run_deterministic_matching
from src.llm_matcher import run_llm_matching

DATA_DIR = PROJECT_ROOT / "data"


# ── Mock helpers ─────────────────────────────────────────────

def make_mock_response(text):
    """Create a mock Gemini response object."""
    resp = MagicMock()
    resp.text = text
    resp.usage_metadata = MagicMock()
    resp.usage_metadata.prompt_token_count = 500
    resp.usage_metadata.candidates_token_count = 100
    return resp


# ── Fixtures ─────────────────────────────────────────────────

@pytest.fixture(scope="module")
def pipeline_data():
    int_records, _ = parse_internal(DATA_DIR / "internal_transactions.csv")
    ext_records, _ = parse_all_external(DATA_DIR)
    det = run_deterministic_matching(int_records, ext_records)
    gt = json.loads((DATA_DIR / "ground_truth.json").read_text(encoding="utf-8"))
    return int_records, ext_records, det, gt


@pytest.fixture(scope="module")
def mock_result(pipeline_data):
    """Run LLM matching with mock API responses."""
    _, _, det, gt = pipeline_data
    {m["internal_id"]: m["external_id"] for m in gt["matches"]}
    set(gt["unmatched_internal"])
    residual_int_ids = {r.txn_id for r in det.residual_internal}

    expected_llm_matches = {}
    for m in gt["matches"]:
        if m["internal_id"] in residual_int_ids:
            expected_llm_matches[m["internal_id"]] = m["external_id"]

    def mock_call_llm(client, system, user, **kwargs):
        prompt = user
        internal_ids = re.findall(r"ID: (TXN_\d+)", prompt)
        scoped_cands = re.findall(r"\[([A-Z])(\d+)\] ID: (EXT_\d+)", prompt)

        if "BATCH MATCHING" in prompt or "GROUP ASSIGNMENT" in prompt or len(internal_ids) > 1:
            matches = []
            claimed_in_response = set()
            for i, int_id in enumerate(internal_ids):
                label = chr(65 + i)
                ext_target = expected_llm_matches.get(int_id)
                match_label = f"{label}0"
                conf = 0.2

                if ext_target and ext_target not in claimed_in_response:
                    for clabel, cidx, cext in scoped_cands:
                        if clabel == label and cext == ext_target:
                            match_label = f"{clabel}{cidx}"
                            conf = 0.9
                            claimed_in_response.add(ext_target)
                            break
                    else:
                        global_cands = re.findall(r"\[(\d+)\] ID: (EXT_\d+)", prompt)
                        for idx_str, ext_id in global_cands:
                            if ext_id == ext_target:
                                match_label = f"{label}{idx_str}"
                                conf = 0.9
                                claimed_in_response.add(ext_target)
                                break

                matches.append({
                    "internal_label": label,
                    "match_label": match_label,
                    "confidence": conf,
                    "reasoning": f"Mock: {'matched' if conf > 0.5 else 'no match'}",
                })

            return json.dumps({"matches": matches}), 500, 100
        else:
            int_id = internal_ids[0] if internal_ids else None
            ext_target = expected_llm_matches.get(int_id)
            candidate_ids = re.findall(r"\[(\d+)\] ID: (EXT_\d+)", prompt)

            if ext_target:
                for idx_str, ext_id in candidate_ids:
                    if ext_id == ext_target:
                        return json.dumps({
                            "match_index": int(idx_str),
                            "confidence": 0.9,
                            "reasoning": "Mock: matched",
                        }), 500, 100

            return json.dumps({
                "match_index": None,
                "confidence": 0.15,
                "reasoning": "Mock: no confident match",
            }), 500, 100

    with patch("src.llm_matcher._call_llm", side_effect=mock_call_llm), \
         patch("src.llm_matcher.genai"):
        result = run_llm_matching(
            det.residual_internal,
            det.residual_external,
            api_key="mock-key",
        )

    return result


# ── Structural Checks ───────────────────────────────────────

class TestMockStructural:
    def test_no_internal_matched_twice(self, mock_result):
        llm_int_ids = {m.internal_id for m in mock_result.matched}
        assert len(llm_int_ids) == len(mock_result.matched)

    def test_no_external_matched_twice(self, mock_result):
        llm_ext_ids = {m.external_id for m in mock_result.matched}
        assert len(llm_ext_ids) == len(mock_result.matched)

    def test_matched_plus_exceptions_equals_residual(self, mock_result, pipeline_data):
        _, _, det, _ = pipeline_data
        llm_int_ids = {m.internal_id for m in mock_result.matched}
        assert (
            len(llm_int_ids) + len(mock_result.exceptions_internal)
            == len(det.residual_internal)
        ), (
            f"{len(llm_int_ids)} + {len(mock_result.exceptions_internal)} "
            f"!= {len(det.residual_internal)}"
        )

    def test_all_matches_have_llm_path(self, mock_result):
        assert all(m.match_path.value == "llm" for m in mock_result.matched)

    def test_no_overlap_with_part2_internal(self, mock_result, pipeline_data):
        _, _, det, _ = pipeline_data
        llm_int_ids = {m.internal_id for m in mock_result.matched}
        det_int_ids = {m.internal_id for m in det.matched}
        assert llm_int_ids.isdisjoint(det_int_ids)

    def test_no_overlap_with_part2_external(self, mock_result, pipeline_data):
        _, _, det, _ = pipeline_data
        llm_ext_ids = {m.external_id for m in mock_result.matched}
        det_ext_ids = {m.external_id for m in det.matched}
        assert llm_ext_ids.isdisjoint(det_ext_ids)


# ── Ground Truth Validation ─────────────────────────────────

class TestMockGroundTruth:
    def test_zero_false_positives(self, mock_result, pipeline_data):
        _, _, _, gt = pipeline_data
        gt_lookup = {m["internal_id"]: m["external_id"] for m in gt["matches"]}
        fp = 0
        wrong = []
        for m in mock_result.matched:
            expected = gt_lookup.get(m.internal_id)
            if not (expected and m.external_id == expected):
                fp += 1
                wrong.append(
                    f"{m.internal_id} -> {m.external_id} (expected {expected})"
                )
        assert fp == 0, "\n".join(wrong)

    def test_all_matchable_records_matched(self, mock_result, pipeline_data):
        _, _, det, gt = pipeline_data
        gt_lookup = {m["internal_id"]: m["external_id"] for m in gt["matches"]}
        gt_unmatched_int = set(gt["unmatched_internal"])
        residual_int_ids = {r.txn_id for r in det.residual_internal}
        matchable = {
            k: v for k, v in gt_lookup.items()
            if k in residual_int_ids and k not in gt_unmatched_int
        }
        tp = sum(
            1 for m in mock_result.matched
            if gt_lookup.get(m.internal_id) == m.external_id
        )
        assert tp == len(matchable), f"got {tp}, expected {len(matchable)}"


# ── Exception Validation ────────────────────────────────────

class TestMockExceptions:
    def test_all_unmatched_internals_are_exceptions(self, mock_result, pipeline_data):
        _, _, _, gt = pipeline_data
        gt_unmatched_int = set(gt["unmatched_internal"])
        exc_int = set(mock_result.exceptions_internal)
        assert gt_unmatched_int.issubset(exc_int), (
            f"missing: {gt_unmatched_int - exc_int}"
        )

    def test_all_unmatched_externals_are_exceptions(self, mock_result, pipeline_data):
        _, _, _, gt = pipeline_data
        gt_unmatched_ext = set(gt["unmatched_external"])
        exc_ext = set(mock_result.exceptions_external)
        assert gt_unmatched_ext.issubset(exc_ext), (
            f"missing: {gt_unmatched_ext - exc_ext}"
        )


# ── Combined Pipeline Metrics ───────────────────────────────

class TestMockCombined:
    def test_combined_match_rate_at_least_95_pct(self, mock_result, pipeline_data):
        _, _, det, gt = pipeline_data
        gt_lookup = {m["internal_id"]: m["external_id"] for m in gt["matches"]}
        total_gt = len(gt["matches"])
        tp = sum(
            1 for m in mock_result.matched
            if gt_lookup.get(m.internal_id) == m.external_id
        )
        combined_tp = len(det.matched) + tp
        assert combined_tp / total_gt >= 0.95, (
            f"got {combined_tp/total_gt*100:.1f}%"
        )


# ── Legacy script mode ───────────────────────────────────────

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
