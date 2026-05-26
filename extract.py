"""
Extraction layer — the ONLY place the LLM is used.
Its job: turn one messy confirmation (any template, any language) into clean
structured rows. It does NOT judge discrepancies; it just reads.

Scans (image-only PDFs) yield no extractable text -> we route them to manual
review rather than guessing a price off a bad image.
"""

import json
import io
import base64
import pdfplumber
from groq import Groq

# Key can be hardcoded for local script use, or set at runtime (e.g. from the app).
_API_KEY = ""  # set via app or Streamlit secret
_client = None

VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"


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


def _read_bytes(source):
    if isinstance(source, str):
        with open(source, "rb") as fh:
            return fh.read()
    if hasattr(source, "getvalue"):
        return source.getvalue()
    return source.read()


def _text_from_pdf(pdf_bytes):
    text = ""
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                text += (page.extract_text() or "") + "\n"
    except Exception:
        text = ""
    return text


def _extract_via_text(text):
    resp = _get_client().chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": EXTRACTION_PROMPT.replace("{text}", text)}],
        temperature=0,
        response_format={"type": "json_object"},
    )
    return json.loads(resp.choices[0].message.content)


def _extract_via_vision(pdf_bytes):
    """Scanned PDF: render the first page to an image and read it with a vision model."""
    import fitz  # PyMuPDF — renders without system deps
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    png = doc[0].get_pixmap(dpi=200).tobytes("png")
    b64 = base64.b64encode(png).decode()
    resp = _get_client().chat.completions.create(
        model=VISION_MODEL,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": EXTRACTION_PROMPT.replace(
                "{text}", "(The confirmation is the attached scanned image. Read it carefully.)")},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]}],
        temperature=0,
        response_format={"type": "json_object"},
    )
    return json.loads(resp.choices[0].message.content)


def extract_confirmation(source, name=None):
    """
    source: a file path (str) OR a file-like object (e.g. an uploaded PDF).
    Cascade: text extraction -> vision (for scans) -> manual review (truly unreadable).
    Returns (confirmation_dict, status). status is 'ok' or 'scan_review'.
    """
    if name is None:
        name = source if isinstance(source, str) else "uploaded.pdf"
    pdf_bytes = _read_bytes(source)

    # 1. Text path — no LLM needed to read a text PDF
    text = _text_from_pdf(pdf_bytes)
    if len(text.strip()) >= 25:
        data = _extract_via_text(text)
        data.setdefault("currency", "USD")
        data.setdefault("lines", [])
        data["_status"], data["_method"] = "ok", "text"
        return data, "ok"

    # 2. Vision path — scanned/image PDF, read the rendered page
    try:
        data = _extract_via_vision(pdf_bytes)
        if data.get("lines"):
            data.setdefault("currency", "USD")
            data["_status"], data["_method"] = "ok", "vision"
            return data, "ok"
    except Exception:
        pass  # fall through to manual review

    # 3. Truly unreadable — leave it as an error file for Lisa to eyeball
    return {
        "po_number": f"[UNREADABLE] {name}",
        "currency": "USD",
        "lines": [],
        "_status": "scan_review",
        "_note": "No text and vision could not read it — flag for Lisa to check by eye.",
    }, "scan_review"