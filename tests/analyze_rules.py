"""Rule 4 + Rule 5 analysis against actual dataset."""
import json, csv, sys
from decimal import Decimal
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, ".")
from src.ingestion import parse_internal, parse_all_external

DATA = Path("data")

# Load everything
gt = json.load(open(DATA / "ground_truth.json"))
int_list, _ = parse_internal(DATA / "internal_transactions.csv")
ext_list, _ = parse_all_external(DATA)
int_map = {r.txn_id: r for r in int_list}
ext_map = {r.ext_id: r for r in ext_list}

print("=" * 70)
print("RULE 4 ANALYSIS: ref_id match with amount > 3% difference")
print("=" * 70)

rule4_targets = []
for m in gt["matches"]:
    itxn = int_map[m["internal_id"]]
    etxn = ext_map.get(m["external_id"])
    if not etxn or not etxn.reference_id:
        continue
    if itxn.reference_id == etxn.reference_id:
        if itxn.amount != 0:
            pct = abs(itxn.amount - etxn.amount) / itxn.amount * 100
            if pct > 3:
                rule4_targets.append((m, itxn, etxn, pct))
                print(f"  {m['internal_id']:8s} <-> {m['external_id']:8s}  "
                      f"noise={m['noise_type']:18s}  "
                      f"int={itxn.amount}  ext={etxn.amount}  diff={pct:.1f}%")

if not rule4_targets:
    print("  (none found)")

print(f"\nTotal Rule 4 targets: {len(rule4_targets)}")
print(f"Of which partial_refund: "
      f"{sum(1 for m,_,_,_ in rule4_targets if m['noise_type']=='partial_refund')}")

print()
print("=" * 70)
print("PARTIAL REFUND RECORDS: ref_id status")
print("=" * 70)
for m in gt["matches"]:
    if m["noise_type"] != "partial_refund":
        continue
    itxn = int_map[m["internal_id"]]
    etxn = ext_map.get(m["external_id"])
    if etxn:
        ref_match = itxn.reference_id == etxn.reference_id
        pct = abs(itxn.amount - etxn.amount) / itxn.amount * 100 if itxn.amount else 0
        print(f"  {m['internal_id']:8s} <-> {m['external_id']:8s}  "
              f"ref_match={str(ref_match):5s}  "
              f"int={itxn.amount}  ext={etxn.amount}  diff={pct:.1f}%")

print()
print("=" * 70)
print("RULE 5 ANALYSIS: amount+date uniqueness (bidirectional)")
print("=" * 70)

# Build amount+date index for both sides
int_by_amt_date = defaultdict(list)
for r in int_list:
    int_by_amt_date[(r.amount, r.date)].append(r.txn_id)

ext_by_amt_date = defaultdict(list)
for r in ext_list:
    ext_by_amt_date[(r.amount, r.date)].append(r.ext_id)

# Find records with no ref that could match by unique amount+date
no_ref_ext = [e for e in ext_list if not e.reference_id]
print(f"\nExternal records with no ref_id: {len(no_ref_ext)}")

for e in no_ref_ext:
    key = (e.amount, e.date)
    int_candidates = int_by_amt_date.get(key, [])
    ext_candidates = ext_by_amt_date.get(key, [])

    unique_int = len(int_candidates) == 1
    unique_ext = len(ext_candidates) == 1

    # Find if this is a ground-truth match
    gt_match = next((m for m in gt["matches"] if m["external_id"] == e.ext_id), None)
    gt_internal = gt_match["internal_id"] if gt_match else None
    noise = gt_match["noise_type"] if gt_match else "unmatched"

    status = "BOTH_UNIQUE" if (unique_int and unique_ext) else \
             "INT_AMBIG" if not unique_int else \
             "EXT_AMBIG" if not unique_ext else "NONE"

    print(f"  {e.ext_id:8s}  amt={str(e.amount):>10s}  date={e.date}  "
          f"int_cands={len(int_candidates)}  ext_cands={len(ext_candidates)}  "
          f"status={status:12s}  noise={noise:18s}  gt_int={gt_internal}")
