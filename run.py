"""
run.py — point it at the confirmations folder, get Lisa's Excel.

Produces reconciliation.xlsx with three sheets:
  - Action Needed   (severity-sorted; dropped lines on top)
  - All Results     (everything, including the clean matches)
  - Needs Review    (scans + anything the tool wouldn't guess on)
"""

import sys
import glob
import os
import pandas as pd
from extract import extract_confirmation
from reconcile import load_reference, reconcile


def main(folder, po_csv, vendor_csv, out="reconciliation.xlsx"):
    pos, vendors = load_reference(po_csv, vendor_csv)

    confirmations = []
    review = []
    pdfs = sorted(glob.glob(os.path.join(folder, "*.pdf")))
    print(f"Found {len(pdfs)} confirmations. Extracting...")

    for path in pdfs:
        try:
            conf, status = extract_confirmation(path)
            if status == "scan_review":
                review.append({"file": os.path.basename(path), "reason": conf["_note"]})
            else:
                conf["_file"] = os.path.basename(path)
                confirmations.append(conf)
            print(f"  {os.path.basename(path):30s} {status}  PO={conf.get('po_number')}")
        except Exception as e:
            review.append({"file": os.path.basename(path), "reason": f"extraction error: {e}"})
            print(f"  {os.path.basename(path):30s} ERROR {e}")

    results = reconcile(pos, vendors, confirmations)

    action = results[results["issue"] != "OK"]
    review_df = pd.DataFrame(review) if review else pd.DataFrame(columns=["file", "reason"])

    with pd.ExcelWriter(out, engine="openpyxl") as xl:
        action.to_excel(xl, sheet_name="Action Needed", index=False)
        results.to_excel(xl, sheet_name="All Results", index=False)
        review_df.to_excel(xl, sheet_name="Needs Review", index=False)

    print(f"\nWrote {out}")
    print(f"  {len(action)} lines need action, {len(review_df)} need manual review")


if __name__ == "__main__":
    folder = sys.argv[1] if len(sys.argv) > 1 else "data/confirmations"
    po_csv = sys.argv[2] if len(sys.argv) > 2 else "open_pos.csv"
    vendor_csv = sys.argv[3] if len(sys.argv) > 3 else "vendor_master.csv"
    main(folder, po_csv, vendor_csv)