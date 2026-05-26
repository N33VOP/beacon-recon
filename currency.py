"""
Live FX rate (USD per 1 EUR) from the ECB via frankfurter.app — free, no key.
Falls back to None if offline, in which case the engine flags EUR for manual review
rather than guessing.
"""

import json
import urllib.request


def usd_per_eur(default=None):
    try:
        url = "https://api.frankfurter.app/latest?from=EUR&to=USD"
        with urllib.request.urlopen(url, timeout=6) as r:
            return json.loads(r.read())["rates"]["USD"]
    except Exception:
        return default