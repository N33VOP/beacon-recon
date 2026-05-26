"""
Beacon Fasteners — PO Confirmation Reconciliation Engine

Architecture:
  1. LLM extracts structured data from each confirmation (text understanding)
  2. This engine matches + flags discrepancies (deterministic, verifiable)
  3. Validation sits between: anything that doesn't parse -> manual review

The LLM NEVER decides whether a price drifted. Code does that. The LLM
only reads messy PDFs into clean rows.
"""

import re
import pandas as pd
from datetime import datetime


def _norm_po(x):
    """Canonicalise a PO number so 'PO PO-4500050030', ' po-4500050030 ' etc all match."""
    s = str(x).upper()
    m = re.search(r"(\d{6,})", s)
    return "PO-" + m.group(1) if m else s.strip()

# Percentage threshold for flagging a price drift (handles the wide price range)
PRICE_DRIFT_PCT = 0.02  # 2%

# Severity ranking for sorting Lisa's action list (lower = more urgent)
SEVERITY_ORDER = {
    "DROPPED LINE": 0,
    "QTY MISMATCH": 1,
    "UNKNOWN PO": 2,
    "PRICE DRIFT": 3,
    "CURRENCY — REVIEW": 4,
    "DATE SLIP": 5,
    "NEEDS REVIEW": 6,
    "OK": 9,
}


def load_reference(po_csv, vendor_csv):
    pos = pd.read_csv(po_csv)
    vendors = pd.read_csv(vendor_csv)
    # which vendors are known to substitute their own part numbers
    vendors["own_pn"] = vendors["known_pn_mapping_note"].fillna("").str.contains(
        "own part numbers", case=False
    )
    return pos, vendors


def _parse_date(raw):
    """Best-effort date parse. Returns (date_or_None, was_ambiguous)."""
    if raw is None or str(raw).strip() == "":
        return None, True
    raw = str(raw).strip()

    # German calendar weeks, e.g. "KW 20-22 / 2026" -> take end of latest week (conservative)
    kw = re.search(r"KW\s*(\d{1,2})\s*[-–]?\s*(\d{1,2})?\s*/?\s*(\d{4})", raw, re.I)
    if kw:
        wk = int(kw.group(2) or kw.group(1))  # latest week in the range
        yr = int(kw.group(3))
        try:
            return datetime.fromisocalendar(yr, wk, 7).date(), False  # Sunday of that week
        except ValueError:
            return None, True

    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date(), False
        except ValueError:
            continue
    return None, True


def _match_lines(po_lines, conf_lines, vendor_uses_own_pn):
    """
    Match confirmation lines to PO lines for a single PO.
    Strategy: exact part-number match first; then positional match for the
    leftovers (vendors keep line order). Returns list of (po_row, conf_row_or_None).
    """
    po_remaining = po_lines.copy()
    matched = []

    # Pass 1: exact part-number match
    used_conf = set()
    for i, po in po_remaining.iterrows():
        for j, conf in conf_lines.iterrows():
            if j in used_conf:
                continue
            if str(conf.get("part_number_shown", "")).strip().upper() == str(po["our_pn"]).strip().upper():
                matched.append((po, conf))
                used_conf.add(j)
                po_remaining = po_remaining.drop(i)
                break

    # Pass 2: positional match for whatever is left (own-PN vendors, missing PNs)
    leftover_conf = [j for j in conf_lines.index if j not in used_conf]
    for i, po in po_remaining.iterrows():
        if leftover_conf:
            j = leftover_conf.pop(0)
            conf = conf_lines.loc[j]
            matched.append((po, conf))
            used_conf.add(j)
        else:
            # no confirmation line left for this PO line -> DROPPED
            matched.append((po, None))

    return matched, used_conf


def reconcile(pos, vendors, confirmations, usd_per_eur=None):
    """
    confirmations: list of dicts, each:
      {po_number, currency, lines: [{part_number_shown, quantity, unit_price,
                                      promise_date_raw}], _confidence, _note}
    usd_per_eur: live FX rate (USD per 1 EUR). If given, EUR prices are converted
                 and checked against the tolerance band instead of just flagged.
    Returns a results DataFrame.
    """
    results = []
    vendor_lookup = vendors.set_index("vendor_name")["own_pn"].to_dict()
    confs_by_po = {}
    for c in confirmations:
        confs_by_po.setdefault(_norm_po(c["po_number"]), []).append(c)

    # Edge case: a confirmation for a PO that isn't on the open list (closed/duplicate/typo)
    po_keys = {_norm_po(p) for p in pos["po_number"].unique()}
    for c in confirmations:
        if _norm_po(c["po_number"]) not in po_keys:
            results.append({
                "po_number": c["po_number"], "vendor": "(unknown)", "line": "-",
                "our_pn": "-", "issue": "UNKNOWN PO",
                "po_says": "not on open PO list",
                "vendor_says": "vendor sent a confirmation",
                "detail": "Confirmation references a PO not on the open list — closed, duplicate, or wrong number",
            })

    for po_number, po_group in pos.groupby("po_number"):
        po_lines = po_group.reset_index(drop=True)
        vendor_name = po_lines.iloc[0]["vendor_name"]
        uses_own = vendor_lookup.get(vendor_name, False)
        po_key = _norm_po(po_number)

        # Validation: did we even get a confirmation for this PO?
        if po_key not in confs_by_po:
            for _, po in po_lines.iterrows():
                results.append(_row(po_number, po, None, "DROPPED LINE",
                                    "On PO; no confirmation found for this PO"))
            continue

        # gather all confirmed lines for this PO
        conf_rows = []
        currency = "USD"
        for c in confs_by_po[po_key]:
            currency = c.get("currency", "USD")
            for ln in c["lines"]:
                conf_rows.append(ln)
        conf_lines = pd.DataFrame(conf_rows)

        matched, _ = _match_lines(po_lines, conf_lines, uses_own)

        for po, conf in matched:
            if conf is None:
                results.append(_row(po_number, po, None, "DROPPED LINE",
                                    "On PO; not confirmed by vendor"))
                continue

            issues = []

            # --- QUANTITY (deterministic) ---
            try:
                cq = float(str(conf.get("quantity")).replace(",", ""))
                if cq != float(po["qty_ordered"]):
                    issues.append(("QTY MISMATCH",
                                   f"ordered {int(po['qty_ordered'])}, confirmed {int(cq)}"))
            except (TypeError, ValueError):
                issues.append(("NEEDS REVIEW", "could not parse confirmed quantity"))

            # --- PRICE ---
            cur = currency.upper()
            if cur == "EUR" and usd_per_eur:
                # convert at live rate, then apply the same tolerance band
                try:
                    cp_eur = float(str(conf.get("unit_price")).replace("€", "").replace(",", ""))
                    cp_usd = cp_eur * usd_per_eur
                    po_price = float(po["unit_price"])
                    if po_price > 0 and abs(cp_usd - po_price) / po_price > PRICE_DRIFT_PCT:
                        issues.append(("PRICE DRIFT",
                                       f"PO ${po_price:.4f} vs €{cp_eur:.4f} ≈ ${cp_usd:.2f} "
                                       f"(@ {usd_per_eur:.4f} USD/EUR) — outside {PRICE_DRIFT_PCT*100:.0f}%"))
                    # within band -> not an issue
                except (TypeError, ValueError):
                    issues.append(("NEEDS REVIEW", "no/unparseable EUR price"))
            elif cur != "USD":
                issues.append(("CURRENCY — REVIEW",
                               f"confirmed in {currency}; no live rate available to convert"))
            else:
                try:
                    cp = float(str(conf.get("unit_price")).replace("$", "").replace(",", ""))
                    po_price = float(po["unit_price"])
                    if po_price > 0 and abs(cp - po_price) / po_price > PRICE_DRIFT_PCT:
                        issues.append(("PRICE DRIFT",
                                       f"PO ${po_price:.4f}, confirmed ${cp:.4f}"))
                except (TypeError, ValueError):
                    issues.append(("NEEDS REVIEW", "no/unparseable price on confirmation"))

            # --- DATE ---
            cd, ambiguous = _parse_date(conf.get("promise_date_raw"))
            req, _ = _parse_date(po["required_date"])
            if ambiguous:
                issues.append(("NEEDS REVIEW",
                               f"promise date unclear: '{conf.get('promise_date_raw')}'"))
            elif cd and req and cd > req:
                issues.append(("DATE SLIP", f"need {req}, promised {cd}"))

            if not issues:
                results.append(_row(po_number, po, conf, "OK", "matches"))
            else:
                # one row per line: worst issue becomes the status, all flags combined
                worst = min(issues, key=lambda x: SEVERITY_ORDER.get(x[0], 5))[0]
                detail = "; ".join(d for _, d in issues)
                results.append(_row(po_number, po, conf, worst, detail))

    df = pd.DataFrame(results)
    df["_sev"] = df["issue"].map(SEVERITY_ORDER).fillna(5)
    df = df.sort_values(["_sev", "po_number"]).drop(columns="_sev").reset_index(drop=True)
    return df


def _row(po_number, po, conf, issue, detail):
    return {
        "po_number": po_number,
        "vendor": po["vendor_name"],
        "line": po["line_number"],
        "our_pn": po["our_pn"],
        "issue": issue,
        "po_says": f"qty {int(po['qty_ordered'])} @ ${po['unit_price']} by {po['required_date']}",
        "vendor_says": (f"qty {conf.get('quantity')} @ {conf.get('unit_price')} by {conf.get('promise_date_raw')}"
                        if conf is not None else "— nothing —"),
        "detail": detail,
    }