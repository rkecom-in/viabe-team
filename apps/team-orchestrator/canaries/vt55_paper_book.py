#!/usr/bin/env python3
"""VT-55 paper-book ingestion canary (Rule #15 / DR-15).

PURE (default): mock multi-entry vision parse (no network/DB).
REAL (VT55_REAL_DB=1 + DATABASE_URL [+ ANTHROPIC_API_KEY]): generate a SYNTHETIC
MULTI-row ledger image (CL-422 — obviously fake), run ingest_paper_book against a
real DB → assert N entries → committed customers + ledger rows. Uses real Anthropic
vision when ANTHROPIC_API_KEY is present; else injects a canned 2-entry vision
result so the adapter→dedup→ledger chain still runs real-DB (fail-not-skip on DB).

    cd apps/team-orchestrator
    uv run --no-project --with anthropic --with pillow --with pydantic python canaries/vt55_paper_book.py
    DATABASE_URL=... VT55_REAL_DB=1 uv run python canaries/vt55_paper_book.py
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

RESULTS: dict[str, dict[str, Any]] = {}
_TENANT = "11111111-1111-4111-8111-111111111111"


def assertion(key, name, passed, *, observed=None):
    RESULTS[key] = {"name": name, "status": "PASS" if passed else "FAIL"}
    print(f"[{key}] {'PASS' if passed else 'FAIL'} — {name}")
    print(f"    observed: {observed}")


def _synthetic_ledger_png() -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (760, 360), (250, 248, 240))
    d = ImageDraw.Draw(img)
    d.text((20, 20), "SYNTHETIC TEST LEDGER - NOT REAL DATA", fill=(0, 0, 0))
    d.text((20, 80), "TEST CUSTOMER A   9000000001   1500", fill=(0, 0, 0))
    d.text((20, 140), "TEST CUSTOMER B   9000000002   2500", fill=(0, 0, 0))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def run() -> int:
    from orchestrator.integrations.vision_extraction import extract_entries_from_image

    # A1 — pure: multi-entry parse from a mock vision response.
    payload = json.dumps({"entries": [
        {"fields": [{"name": "customer_name", "value": "A", "confidence": 0.9}]},
        {"fields": [{"name": "customer_name", "value": "B", "confidence": 0.9}]},
    ]})
    fake = SimpleNamespace(messages=SimpleNamespace(
        create=lambda **kw: SimpleNamespace(content=[SimpleNamespace(type="text", text=payload)])))
    got = extract_entries_from_image(
        _synthetic_ledger_png(), tenant_id=_TENANT,
        target_fields=["customer_name"], acquired_via="paper_book",
        media_type="image/png", client=fake, consent_check=lambda _t: True)
    assertion("A1", "mock multi-entry vision → N ExtractionResults", len(got) == 2,
              observed={"entries": len(got)})

    if os.environ.get("VT55_REAL_DB", "0") != "1":
        return _finalise(False)

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("PREFLIGHT FAIL (real DB) — DATABASE_URL absent. Fail-not-skip.", file=sys.stderr)
        return 2

    import apply_migrations
    import psycopg

    if apply_migrations.apply(dsn=dsn)["failed"]:
        print("PREFLIGHT FAIL — migrations", file=sys.stderr)
        return 2
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    if not os.environ.get("TEAM_PHONE_ENCRYPTION_KEY"):
        from cryptography.fernet import Fernet

        os.environ["TEAM_PHONE_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        from orchestrator.db import tenant_connection
        from orchestrator.integrations.methods.paper_book import ingest_paper_book

        with psycopg.connect(dsn, autocommit=True) as cn:
            tenant = str(cn.execute(
                "INSERT INTO tenants (business_name, plan_tier, phase) VALUES "
                "('VT-55 canary','founding','onboarding') RETURNING id").fetchone()[0])

        kwargs: dict[str, Any] = {}
        if not os.environ.get("ANTHROPIC_API_KEY", "").startswith("sk-ant-"):
            # No vision key → inject canned 2-entry result; adapter→DB still real.
            from orchestrator.integrations.vision_extraction import (
                ExtractedField, ExtractionResult,
            )
            ph = "+9190" + uuid4().int.__str__()[:8]
            ph2 = "+9190" + uuid4().int.__str__()[:8]
            canned = [
                ExtractionResult(fields=(
                    ExtractedField(name="customer_name", value="TEST A", confidence=0.9),
                    ExtractedField(name="phone", value=ph, confidence=0.95),
                    ExtractedField(name="amount", value="1500", confidence=0.9)),
                    acquired_via="paper_book", model="canned"),
                ExtractionResult(fields=(
                    ExtractedField(name="customer_name", value="TEST B", confidence=0.9),
                    ExtractedField(name="phone", value=ph2, confidence=0.95),
                    ExtractedField(name="amount", value="2500", confidence=0.9)),
                    acquired_via="paper_book", model="canned"),
            ]
            kwargs["extract_fn"] = lambda *a, **k: canned
            print("(no ANTHROPIC_API_KEY — injecting canned 2-entry vision; DB chain real)")

        summary = ingest_paper_book(tenant, _synthetic_ledger_png(),
                                    media_type="image/png", consent_check=lambda _t: True, **kwargs)
        with tenant_connection(tenant) as conn:
            ncust = conn.execute("SELECT count(*) AS n FROM customers").fetchone()["n"]
            nled = conn.execute("SELECT count(*) AS n FROM customer_ledger_entries").fetchone()["n"]
        assertion("A2", "synthetic multi-entry → committed customers + ledger rows",
                  summary.committed >= 1 and ncust >= summary.committed and nled >= 1,
                  observed={"extracted": summary.entries_extracted, "committed": summary.committed,
                            "customers": ncust, "ledger": nled})
    finally:
        shutdown_dbos()
    return _finalise(True)


def _finalise(real):
    print("\n=== CANARY SUMMARY ===")
    for k, r in RESULTS.items():
        print(f"  [{k}] {r['status']} — {r['name']}")
    print(f"\n=== mode: {'REAL' if real else 'PURE'} ===")
    failed = [k for k, r in RESULTS.items() if r["status"] != "PASS"]
    if failed:
        print(f"\nFAILED: {failed}", file=sys.stderr)
        return 1
    print(f"\nALL {len(RESULTS)} ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run())
