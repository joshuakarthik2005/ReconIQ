"""Quick analysis of the 7 residual records and 9 amount_date_unique matches."""
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

int_map = {r.txn_id: r for r in int_list}
ext_map = {r.ext_id: r for r in ext_list}
gt_noise = {m["internal_id"]: m["noise_type"] for m in gt["matches"]}
gt_lookup = {m["internal_id"]: m["external_id"] for m in gt["matches"]}

print("=== 9 amount_date_unique matches ===")
adu = [m for m in output.matched if m.rule_name == "amount_date_unique"]
for m in adu:
    noise = gt_noise.get(m.internal_id, "unmatched")
    itxn = int_map[m.internal_id]
    etxn = ext_map[m.external_id]
    print(f"  {m.internal_id} <-> {m.external_id}  noise={noise:18s}  "
          f"int_ref={itxn.reference_id[:20]:20s}  ext_ref={str(etxn.reference_id)[:20] if etxn.reference_id else 'None':20s}")

print("\n=== 7 residual matched records (missed by rules) ===")
matched_ids = {m.internal_id for m in output.matched}
for gm in gt["matches"]:
    if gm["internal_id"] not in matched_ids:
        iid = gm["internal_id"]
        eid = gm["external_id"]
        itxn = int_map[iid]
        etxn = ext_map.get(eid)
        noise = gm["noise_type"]
        if etxn:
            ref_match = itxn.reference_id == etxn.reference_id
            amt_diff = abs(itxn.amount - etxn.amount)
            date_diff = abs((
                __import__("datetime").datetime.strptime(itxn.date, "%Y-%m-%d") -
                __import__("datetime").datetime.strptime(etxn.date, "%Y-%m-%d")
            ).days)
            print(f"  {iid} <-> {eid}  noise={noise:18s}  "
                  f"ref_match={str(ref_match):5s}  "
                  f"amt_diff={amt_diff}  date_diff={date_diff}d  "
                  f"ext_ref={str(etxn.reference_id)[:20] if etxn.reference_id else 'None'}")
