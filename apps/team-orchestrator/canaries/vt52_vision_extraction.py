#!/usr/bin/env python3
"""VT-52 Vision-LLM extraction canary (Rule #15 / DR-15).

Default = MOCK (no network, 0 paise): injects a fake Anthropic client + an
allow-consent predicate, exercises the per-field-confidence parse + the
fail-closed consent gate deterministically.

REAL mode — set:
    VT52_REAL_VISION=1
Makes a LIVE Anthropic VISION call (Haiku — the canary slot, CL-274) on a
SYNTHETIC ledger image GENERATED HERE with Pillow (CL-422: no real names/phones —
the rows are obviously fake: "TEST CUSTOMER A / 9000000001"). Asserts the result
shape: every field carries a float confidence in [0,1]. FAIL-NOT-SKIP: if
VT52_REAL_VISION=1 but ANTHROPIC_API_KEY is absent/invalid, the canary EXITS
NON-ZERO (no silent fallback to mock).

Consent: the consent gate is unit-tested separately (test_vision_extraction.py);
here it is injected as allow so the canary targets the Anthropic call (the
Rule #15 external dependency). The image NEVER carries real PII (CL-422).

    cd apps/team-orchestrator
    uv run --no-project --with anthropic --with pillow python canaries/vt52_vision_extraction.py            # mock
    (set -a; source ../../.viabe/secrets/anthropic.env; set +a; VT52_REAL_VISION=1 \
       uv run --no-project --with anthropic --with pillow python canaries/vt52_vision_extraction.py)         # real
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

RESULTS: dict[str, dict[str, Any]] = {}
_TENANT = UUID("11111111-1111-4111-8111-111111111111")  # synthetic (CL-422)


def assertion(key: str, name: str, passed: bool, *, observed=None) -> None:
    RESULTS[key] = {"name": name, "status": "PASS" if passed else "FAIL"}
    print(f"[{key}] {'PASS' if passed else 'FAIL'} — {name}")
    print(f"    observed: {observed}")


def _real() -> bool:
    return os.environ.get("VT52_REAL_VISION", "0") == "1"


def _synthetic_ledger_png() -> bytes:
    """Generate an UNMISTAKABLY SYNTHETIC ledger image (CL-422). No real PII."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (700, 300), (250, 248, 240))
    d = ImageDraw.Draw(img)
    d.text((20, 20), "SYNTHETIC TEST LEDGER - NOT REAL DATA", fill=(0, 0, 0))
    d.text((20, 70), "Name: TEST CUSTOMER A", fill=(0, 0, 0))
    d.text((20, 110), "Phone: 9000000001", fill=(0, 0, 0))
    d.text((20, 150), "Balance: 1250", fill=(0, 0, 0))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def run_canary() -> int:
    from orchestrator.integrations.vision_extraction import (
        ConsentRejectedError,
        extract_from_image,
        route_field,
    )

    real = _real()
    target = ["customer_name", "phone", "balance"]

    # A0: consent fail-closed (deterministic, both modes) — deny => no transmit.
    class _Boom:
        class _M:
            def create(self, **kw):  # noqa: ARG002
                raise AssertionError("transmitted despite no consent")

        def __init__(self):
            self.messages = _Boom._M()

    denied = False
    try:
        extract_from_image(
            _synthetic_ledger_png(), tenant_id=_TENANT, target_fields=target,
            acquired_via="paper_book", media_type="image/png",
            client=_Boom(), consent_check=lambda _t: False,
        )
    except ConsentRejectedError:
        denied = True
    assertion("A0", "consent absent -> ConsentRejectedError, no transmission", denied,
              observed={"raised": denied})

    if not real:
        # A1 (mock): fake client returns per-field-confidence JSON.
        payload = json.dumps({"fields": [
            {"name": "customer_name", "value": "TEST CUSTOMER A", "confidence": 0.93},
            {"name": "phone", "value": "9000000001", "confidence": 0.74},
            {"name": "balance", "value": "1250", "confidence": 0.88},
        ]})
        client = SimpleNamespace(messages=SimpleNamespace(
            create=lambda **kw: SimpleNamespace(
                content=[SimpleNamespace(type="text", text=payload)])))
        out = extract_from_image(
            _synthetic_ledger_png(), tenant_id=_TENANT, target_fields=target,
            acquired_via="paper_book", media_type="image/png",
            client=client, consent_check=lambda _t: True,
        )
        shape_ok = all(isinstance(f.confidence, float) and 0.0 <= f.confidence <= 1.0
                       for f in out.fields) and len(out.fields) == 3
        assertion("A1", "mock extraction -> 3 fields, each confidence float in [0,1]",
                  shape_ok, observed={"fields": [(f.name, f.confidence) for f in out.fields]})
        routes = {f.name: route_field(f) for f in out.fields}
        assertion("A2", "route_field maps via shared thresholds",
                  routes["customer_name"] == "commit_silently"
                  and routes["phone"] == "commit_with_notification",
                  observed=routes)
        return _finalise(real)

    # REAL mode — fail-not-skip on missing creds.
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key.startswith("sk-ant-"):
        print("PREFLIGHT FAIL (real mode) — ANTHROPIC_API_KEY absent/invalid. "
              "Real mode is fail-not-skip (Rule #15).", file=sys.stderr)
        return 2
    print("PREFLIGHT OK (real mode) — live Anthropic vision call on SYNTHETIC image.")
    out = extract_from_image(
        _synthetic_ledger_png(), tenant_id=_TENANT, target_fields=target,
        acquired_via="paper_book", media_type="image/png",
        consent_check=lambda _t: True,  # consent unit-tested separately
        model="claude-haiku-4-5",       # canary slot
    )
    shape_ok = len(out.fields) >= 1 and all(
        isinstance(f.confidence, float) and 0.0 <= f.confidence <= 1.0 for f in out.fields
    )
    assertion("A1-REAL", "live vision -> fields each with confidence float in [0,1]",
              shape_ok, observed={"model": out.model,
                                  "fields": [(f.name, f.value, f.confidence) for f in out.fields]})
    return _finalise(real)


def _finalise(real: bool) -> int:
    print("\n=== CANARY SUMMARY ===")
    for k, r in RESULTS.items():
        print(f"  [{k}] {r['status']} — {r['name']}")
    print(f"\n=== mode: {'REAL (live Anthropic vision)' if real else 'MOCK (no network)'} ===")
    if not real:
        print("=== cost: 0 paise (mock) ===")
    failed = [k for k, r in RESULTS.items() if r["status"] != "PASS"]
    if failed:
        print(f"\nFAILED: {failed}", file=sys.stderr)
        return 1
    print(f"\nALL {len(RESULTS)} ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_canary())
