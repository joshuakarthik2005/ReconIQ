"""Confirm exact residual pool sizes and composition for Part 3."""
import json, sys
from pathlib import Path

sys.path.insert(0, ".")
from src.ingestion import parse_internal, parse_all_external
from src.deterministic_matcher import run_deterministic_matching

DATA = Path("data")
int_list, _ = parse_internal(DATA / "internal_transactions.csv")
ext_list, _ = parse_all_external(DATA)
output = run_deterministic_matching(int_list, ext_list)
gt = json.load(open(DATA / "ground_truth.json"))

gt_lookup = {m["internal_id"]: m["external_id"] for m in gt["matches"]}
gt_noise = {m["internal_id"]: m["noise_type"] for m in gt["matches"]}
gt_unmatched_int = set(gt["unmatched_internal"])
gt_unmatched_ext = set(gt["unmatched_external"])

print(f"=== RESIDUAL POOL SIZES ===")
print(f"  residual_internal: {len(output.residual_internal)}")
print(f"  residual_external: {len(output.residual_external)}")

print(f"\n=== RESIDUAL INTERNAL BREAKDOWN ===")
has_gt_match = []
no_gt_match = []
for r in output.residual_internal:
    if r.txn_id in gt_lookup:
        has_gt_match.append(r.txn_id)
    else:
        no_gt_match.append(r.txn_id)

print(f"  Has ground-truth match (should be LLM-matchable): {len(has_gt_match)}")
for iid in has_gt_match:
    print(f"    {iid}  noise={gt_noise[iid]}  expected_ext={gt_lookup[iid]}")

print(f"  No ground-truth match (should become exceptions): {len(no_gt_match)}")
for iid in no_gt_match:
    in_gt_unmatched = iid in gt_unmatched_int
    print(f"    {iid}  in_gt_unmatched_internal={in_gt_unmatched}")

print(f"\n=== RESIDUAL EXTERNAL BREAKDOWN ===")
ext_has_match = []
ext_no_match = []
for e in output.residual_external:
    paired_internals = [m["internal_id"] for m in gt["matches"] if m["external_id"] == e.ext_id]
    if paired_internals:
        ext_has_match.append((e.ext_id, paired_internals[0]))
    else:
        ext_no_match.append(e.ext_id)

print(f"  Has ground-truth match (partner in residual_internal): {len(ext_has_match)}")
for eid, iid in ext_has_match:
    print(f"    {eid} <-> {iid}  noise={gt_noise.get(iid, '?')}")

print(f"  No ground-truth match (should become exceptions): {len(ext_no_match)}")
for eid in ext_no_match:
    in_gt_unmatched = eid in gt_unmatched_ext
    print(f"    {eid}  in_gt_unmatched_external={in_gt_unmatched}")

print(f"\n=== CONFIRMATION ===")
print(f"  GT unmatched internal (13): all in residual? "
      f"{gt_unmatched_int.issubset({r.txn_id for r in output.residual_internal})}")
print(f"  GT unmatched external (3): all in residual? "
      f"{gt_unmatched_ext.issubset({e.ext_id for e in output.residual_external})}")

print(f"\n=== WALL CLOCK TIME ===")
print(f"  Elapsed: {output.elapsed_seconds:.6f}s")
total = len(int_list) + len(ext_list)
print(f"  Records: {total}")
print(f"  Throughput: {total/output.elapsed_seconds:.0f} records/sec")
