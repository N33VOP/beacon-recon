"""
Beacon PO Reconciliation — Lisa's app.

Drop the PO list, the vendor master, and the confirmation PDFs in one box.
The app sorts them out, runs the same verified engine, and gives back the
action list on screen plus a downloadable Excel.

Run locally:   streamlit run app.py
"""

import io
import pandas as pd
import streamlit as st

import extract
from reconcile import load_reference, reconcile

st.set_page_config(page_title="Beacon PO Reconciliation", layout="wide")


def extract_one(f):
    """Wrap an uploaded PDF so the engine's extractor can read it."""
    data = io.BytesIO(f.getvalue())
    return extract.extract_confirmation(data, name=f.name)


# Severity -> soft tint for the on-screen table (dark text forced for contrast)
SEV_COLOR = {
    "DROPPED LINE": "#f8c9c9",
    "QTY MISMATCH": "#fbd9b5",
    "UNKNOWN PO": "#fbd9b5",
    "PRICE DRIFT": "#fcec9e",
    "FX CHECK": "#dcd2f5",
    "CURRENCY — REVIEW": "#dcd2f5",
    "DATE SLIP": "#c9def8",
    "NO PRICE SHOWN": "#e6e6e6",
    "NEEDS REVIEW": "#e6e6e6",
    "OK": "#cde8c5",
}

st.title("Beacon PO Confirmation Reconciliation")
st.caption("Catches silently dropped lines, price drift, date slips, and quantity shorts across vendor confirmations.")

# --- API key: from deployment secret if present, else ask (local/dev) ---
try:
    SECRET_KEY = st.secrets["GROQ_API_KEY"]
except Exception:
    SECRET_KEY = ""

with st.sidebar:
    st.subheader("Setup")
    if SECRET_KEY:
        key = SECRET_KEY
        st.success("Connected and ready.")
    else:
        key = st.text_input("Groq API key", type="password",
                            help="Used only to read the PDFs.")
    st.markdown("**How to use**")
    st.markdown("Drop your open PO list, the vendor master, and all confirmation PDFs below, then press Reconcile.")

uploaded = st.file_uploader(
    "Drop the PO list (CSV), vendor master (CSV), and all confirmation PDFs here",
    type=["csv", "pdf"],
    accept_multiple_files=True,
)

if uploaded and st.button("Reconcile", type="primary"):
    if not key:
        st.error("Enter your Groq API key in the sidebar first.")
        st.stop()
    extract.set_api_key(key)

    # --- sort the dropped files ---
    po_file, vendor_file, pdfs = None, None, []
    for f in uploaded:
        if f.name.lower().endswith(".csv"):
            df = pd.read_csv(f)
            f.seek(0)
            if "po_number" in df.columns:
                po_file = f
            elif "vendor_id" in df.columns:
                vendor_file = f
        else:
            pdfs.append(f)

    if po_file is None or vendor_file is None:
        st.error("Need both CSVs: the open PO list (has a 'po_number' column) and the vendor master (has a 'vendor_id' column).")
        st.stop()

    pos, vendors = load_reference(po_file, vendor_file)

    # --- extract every PDF ---
    confirmations, scans, errors = [], [], []
    progress = st.progress(0.0, text="Reading confirmations...")
    for i, f in enumerate(pdfs):
        try:
            conf, status = extract_one(f)
            conf["_file"] = f.name
            if status == "scan_review":
                scans.append(f.name)
            else:
                confirmations.append(conf)
        except Exception as e:
            errors.append({"file": f.name, "error": str(e)[:300]})
        progress.progress((i + 1) / max(len(pdfs), 1), text=f"Read {f.name}")
    progress.empty()

    if errors:
        st.warning(f"{len(errors)} file(s) couldn't be processed (likely API rate limit). "
                   "They're listed below and in the Excel — the rest still reconciled.")
        st.dataframe(pd.DataFrame(errors), use_container_width=True)

    from currency import usd_per_eur_detailed
    fx_rate, fx_live = usd_per_eur_detailed()
    src = "live ECB" if fx_live else "fallback (live FX unreachable)"
    st.caption(f"FX applied: 1 EUR = ${fx_rate:.4f} USD ({src}). EUR prices converted and checked within 2%.")

    results = reconcile(pos, vendors, confirmations, usd_per_eur=fx_rate)
    action = results[results["issue"] != "OK"].reset_index(drop=True)

    # --- summary metrics ---
    counts = results["issue"].value_counts().to_dict()
    cols = st.columns(6)
    for col, label in zip(cols, ["DROPPED LINE", "QTY MISMATCH", "PRICE DRIFT",
                                  "FX CHECK", "DATE SLIP", "OK"]):
        col.metric(label.title(), counts.get(label, 0))

    # --- action table (coloured) ---
    st.subheader(f"Action Needed — {len(action)} lines")

    def _highlight(row):
        c = SEV_COLOR.get(row["issue"], "#ffffff")
        return [f"background-color: {c}; color: #14181f"] * len(row)

    if len(action):
        st.dataframe(action.style.apply(_highlight, axis=1), use_container_width=True, height=480)
    else:
        st.success("Every confirmed line matches its PO. Nothing to chase.")

    # --- scanned / unreadable ---
    if scans:
        st.subheader(f"Couldn't read — {len(scans)} file(s) need a human")
        st.write(", ".join(scans))

    # --- Excel download (shared, formatted writer) ---
    from excel_out import write_workbook
    buf = io.BytesIO()
    write_workbook(buf, action, results, scans, errors)
    st.download_button("Download Excel for Lisa", buf.getvalue(),
                       file_name="reconciliation.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")