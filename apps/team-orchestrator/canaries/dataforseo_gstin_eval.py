#!/usr/bin/env python3
"""Evaluate DataForSEO (Google Organic + AI Mode) as a name -> GSTIN discovery leg.

Scored against KNOWN TRUTH, not eyeballed, because the question that matters is PRECISION:
every earlier candidate source we tried returned GSTINs belonging to OTHER companies, and a
plausible GSTIN for the wrong legal entity is the VT-406 Sundaram failure — Sandbox confirms it
as real and ACTIVE because it belongs to somebody, and cannot know the owner picked wrong.

So each source is measured on:
  * HIT     — did the business's REAL GSTIN come back at all?
  * NOISE   — how many OTHER companies' GSTINs rode along?
  * PAIRED  — was the GSTIN attached to a company NAME we can match, or floating loose in text?

A source that returns the right answer buried in 8 wrong ones is not usable as-is; it needs the
same name-relevance discipline the web leg already has (VT-448 `_significant_tokens`).

Credentials: DATAFORSEO_LOGIN + DATAFORSEO_PASSWORD, consumed OS-env -> process. Never printed.
Put them in .viabe/secrets/dataforseo.env and source it before running.

Usage:
    set -a; . .viabe/secrets/dataforseo.env; set +a
    uv run --no-project --with httpx python canaries/dataforseo_gstin_eval.py
"""
from __future__ import annotations

import base64
import os
import re
import sys
import time

import httpx

BASE = "https://api.dataforseo.com/v3"
GSTIN_RE = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]\b")

# Ground truth. RKeCom is Fazal's own company and the case every other leg failed on;
# Balaji Wafers is a multi-registration business (one PAN, many state GSTINs).
TRUTH: dict[str, dict[str, object]] = {
    "RKeCom Services": {
        "gstins": {"27AAKCR3738B1ZE"},
        "pan": "AAKCR3738B",
        "note": "Maharashtra. Found by the knowyourgst FORM scrape; Google does not index it.",
    },
    "Balaji Wafers": {
        "gstins": {"24AAACB8755A2Z0", "24AAACB8755A4ZY", "24AAACB8755A3ZZ", "24AAACB8755A1Z1"},
        "pan": "AAACB8755A",
        "note": "Gujarat + other states, all one PAN.",
    },
}

_GENERIC = {"services", "service", "pvt", "ltd", "private", "limited", "company", "co"}


def _auth_header() -> str:
    """Basic auth from either form: a pre-encoded DATAFORSEO_API_BASE64 (what our secrets file
    carries) or a raw DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD pair. Never logged either way."""
    encoded = os.environ.get("DATAFORSEO_API_BASE64", "").strip()
    if encoded:
        return "Basic " + encoded
    login = os.environ.get("DATAFORSEO_LOGIN", "").strip()
    password = os.environ.get("DATAFORSEO_PASSWORD", "").strip()
    if not login or not password:
        sys.exit(
            "No DataForSEO credential. Set DATAFORSEO_API_BASE64, or DATAFORSEO_LOGIN + "
            "DATAFORSEO_PASSWORD.\n"
            "  set -a; . .viabe/secrets/dataforseo.env; set +a"
        )
    return "Basic " + base64.b64encode(f"{login}:{password}".encode()).decode()


def _post(path: str, payload: list[dict], hdr: str) -> tuple[dict | None, float, str]:
    t0 = time.time()
    try:
        r = httpx.post(f"{BASE}{path}", headers={"Authorization": hdr,
                                                 "Content-Type": "application/json"},
                       json=payload, timeout=180.0)
    except Exception as exc:  # noqa: BLE001
        return None, round(time.time() - t0, 1), f"transport:{type(exc).__name__}"
    dt = round(time.time() - t0, 1)
    if r.is_error:
        # status only — never echo a body that could carry the credential back
        return None, dt, f"HTTP {r.status_code}"
    return r.json(), dt, "ok"


def _walk_strings(node, out: list[str]) -> None:
    """Every string in the response tree — GSTINs hide in titles, snippets and AI answer text."""
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, dict):
        for v in node.values():
            _walk_strings(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_strings(v, out)


def _significant(name: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", name.lower()) if len(t) >= 3 and t not in _GENERIC}


def _score(label: str, name: str, strings: list[str], secs: float) -> None:
    truth = TRUTH[name]
    real: set[str] = truth["gstins"]          # type: ignore[assignment]
    pan: str = truth["pan"]                    # type: ignore[assignment]
    sig = _significant(name)

    found: set[str] = set()
    paired: set[str] = set()                   # GSTIN sitting in a string that also names the business
    for s in strings:
        up = s.upper()
        hits = GSTIN_RE.findall(up)
        if not hits:
            continue
        relevant = any(t in s.lower() for t in sig)
        for g in hits:
            found.add(g)
            if relevant:
                paired.add(g)

    correct = found & real
    same_pan = {g for g in found if pan in g} - correct
    noise = found - correct - same_pan

    verdict = "HIT" if correct else ("PAN-ONLY" if same_pan else ("NOISE-ONLY" if found else "NOTHING"))
    print(f"  {label:22} {secs:>5}s  {verdict:11} "
          f"correct={len(correct)} same_pan={len(same_pan)} noise={len(noise)} paired={len(paired)}")
    if correct:
        print(f"      correct : {sorted(correct)}")
    if same_pan:
        print(f"      same PAN: {sorted(same_pan)[:4]}")
    if noise:
        print(f"      NOISE   : {sorted(noise)[:6]}  <- other companies' registrations")


def organic(name: str, hdr: str) -> None:
    for q in (f'"{name}" GST number', f"{name} GSTIN"):
        payload = [{"keyword": q, "language_code": "en", "location_code": 2356, "depth": 20}]
        data, secs, status = _post("/serp/google/organic/live/advanced", payload, hdr)
        if data is None:
            print(f"  {'organic':22} {secs:>5}s  {status}")
            continue
        strings: list[str] = []
        _walk_strings(data, strings)
        _score(f"organic {q[:12]!r}", name, strings, secs)


def ai_mode(name: str, hdr: str) -> None:
    """AI Mode returns a synthesised answer. Interesting BECAUSE it is synthesised: if it states a
    GSTIN the underlying pages never carried, that is the same fabrication class as ungrounded
    Gemini (VT-777) and it must be treated as a hint, never as a source of truth."""
    q = f"What is the GSTIN of {name}?"
    for path in ("/serp/google/ai_mode/live/advanced", "/serp/google/ai_mode/live/html"):
        payload = [{"keyword": q, "language_code": "en", "location_code": 2356}]
        data, secs, status = _post(path, payload, hdr)
        if data is None:
            print(f"  {'ai_mode':22} {secs:>5}s  {status}  ({path.rsplit('/', 1)[-1]})")
            continue
        strings: list[str] = []
        _walk_strings(data, strings)
        _score("ai_mode", name, strings, secs)
        return


def main() -> None:
    hdr = _auth_header()
    data, secs, status = _post("/appendix/user_data", [], hdr)
    print(f"auth check: {status} ({secs}s)")
    if status != "ok":
        sys.exit("DataForSEO auth failed — check the credentials (value withheld).")
    for name in TRUTH:
        print(f"\n== {name!r}  — {TRUTH[name]['note']}")
        organic(name, hdr)
        ai_mode(name, hdr)
    print("\nReminder: every leg here is HINTS-ONLY. Sandbox stays the sole authoritative gate,")
    print("and it cannot detect a valid GSTIN belonging to the WRONG company (VT-406 / VT-777).")


if __name__ == "__main__":
    main()
