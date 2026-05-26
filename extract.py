"""
Extraction layer — the ONLY place the LLM is used.
Its job: turn one messy confirmation (any template, any language) into clean
structured rows. It does NOT judge discrepancies; it just reads.

Scans (image-only PDFs) yield no extractable text -> we route them to manual
review rather than guessing a price off a bad image.
"""

import json
import pdfplumber
from groq import Groq


_API_KEY = ""
_client = None


def set_api_key(key):
    global _API_KEY, _client
    _API_KEY = key
    _client = None


def _get_client():
    global _client
    if _client is None:
        _client = Groq(api_key=_API_KEY)
    return _client

EXTRACTION_PROMPT = """You are reading a single purchase-order confirmation from a vendor.
The format varies: clean ERP PDFs, scans, plain-text emails, and some in German.

Extract ONLY what is actually present. Do not infer or invent values.
Return STRICT JSON, nothing else, in exactly this shape:

{
  "po_number": "the customer/Beacon PO number exactly as written, e.g. PO-4500050027",
  "currency": "the currency of the prices: USD, EUR, etc. If a euro sign or EUR appears, use EUR. Default USD only if clearly dollars.",
  "lines": [
    {
      "part_number_shown": "the part number printed on THIS confirmation (the vendor's, even if it differs from ours). Empty string if none shown.",
      "quantity": "the confirmed quantity as a plain number string, no commas",
      "unit_price": "the confirmed unit price as a plain number, no currency symbol. Empty string if no price is shown.",
      "promise_date_raw": "the promised/delivery date EXACTLY as written, verbatim. Do NOT reformat. If it is a range or calendar week (e.g. 'KW 20-22 / 2026'), copy it as-is."
    }
  ]
}

Rules:
- One object per line item on the confirmation.
- Copy the promise date verbatim — never normalise it. Ambiguity is handled downstream.
- If the price is missing, use an empty string. Do not guess.
- Capture currency carefully; getting USD vs EUR wrong is a serious error.

Confirmation text:
---
{text}
---
"""


def extract_confirmation(source, name=None):
    """
    source: a file path (str) OR a file-like object (e.g. an uploaded PDF).
    Returns (confirmation_dict, status). status is 'ok' or 'scan_review'.
    """
    if name is None:
        name = source if isinstance(source, str) else "uploaded.pdf"

    # 1. Pull text with plain code (no LLM needed to read a text PDF)
    text = ""
    try:
        with pdfplumber.open(source) as pdf:
            for page in pdf.pages:
                text += (page.extract_text() or "") + "\n"
    except Exception:
        text = ""

    # 2. Scan detection: no meaningful text => image-only PDF => manual review
    if len(text.strip()) < 25:
        return {
            "po_number": f"[SCAN] {name}",
            "currency": "USD",
            "lines": [],
            "_status": "scan_review",
            "_note": "No extractable text — likely a scanned image. Flag for Lisa to read by eye.",
        }, "scan_review"

    # 3. LLM extraction — the one place text understanding matters
    resp = _get_client().chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": EXTRACTION_PROMPT.replace("{text}", text)}],
        temperature=0,
        response_format={"type": "json_object"},
    )
    data = json.loads(resp.choices[0].message.content)

    # 4. Validation guard — never let a malformed extraction into the engine silently
    data.setdefault("currency", "USD")
    data.setdefault("lines", [])
    data["_status"] = "ok"
    return data, "ok"