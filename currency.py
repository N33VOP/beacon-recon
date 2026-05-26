"""
Live FX rate (USD per 1 EUR). Tries multiple free, no-key sources; if all are
unreachable, falls back to a recent rate so EUR conversion still runs (clearly
flagged as a fallback). Never returns None, so the engine always converts.
"""

import json
import urllib.request

FALLBACK_USD_PER_EUR = 1.08  # recent ECB-ballpark; used only if live sources fail

_SOURCES = [
    ("https://api.frankfurter.app/latest?from=EUR&to=USD",
     lambda d: d["rates"]["USD"]),
    ("https://open.er-api.com/v6/latest/EUR",
     lambda d: d["rates"]["USD"]),
]


def _fetch(url, pick):
    req = urllib.request.Request(url, headers={"User-Agent": "beacon-recon/1.0"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return float(pick(json.loads(r.read())))


def usd_per_eur_detailed():
    """Returns (rate, is_live)."""
    for url, pick in _SOURCES:
        try:
            return _fetch(url, pick), True
        except Exception:
            continue
    return FALLBACK_USD_PER_EUR, False


def usd_per_eur(default=FALLBACK_USD_PER_EUR):
    rate, _ = usd_per_eur_detailed()
    return rate