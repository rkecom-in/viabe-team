"""VT-59 — cash-book (Method 5) tests.

PURE: input validation. DB (real Postgres, no mock cursors): image-only /
audio-only / both-merge paths → attributed customer + ledger; unattributed
narration → parked; a photo↔voice conflict (low confidence) → clarifying flow.
Sarvam + Anthropic are FAKED (no network/key); the merge/extraction JSON is
injected. Synthetic data only (CL-422).
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("pydantic")

from orchestrator.integrations.methods.cash_book import ingest_cash_book  # noqa: E402
from orchestrator.integrations.vision_extraction import (  # noqa: E402
    ExtractedField,
    ExtractionResult,
)
from orchestrator.integrations.voice_transcription import TranscriptionResult  # noqa: E402


# --- fakes --------------------------------------------------------------------

def _entry(**fields):
    return {"fields": [
        {"name": n, "value": v, "confidence": c} for n, (v, c) in fields.items()
    ]}


class _FakeAnthropic:
    """Anthropic stand-in returning a canned JSON body (owner-typed OR merge)."""

    def __init__(self, *entries):
        self._body = json.dumps({"entries": list(entries)})
        self.messages = SimpleNamespace(create=lambda **_k: SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self._body)]))


def _transcribe(text="Rajesh paid 500"):
    return lambda audio_bytes, **_k: TranscriptionResult(
        transcript_text=text, language="en", confidence=0.9)


def _vision(*entries):
    """image_extract_fn stub returning fixed ExtractionResults."""
    results = [
        ExtractionResult(
            fields=tuple(ExtractedField(name=n, value=v, confidence=c)
                         for n, (v, c) in e.items()),
            acquired_via="cash_book", model="stub",
        )
        for e in entries
    ]
    return lambda *_a, **_k: results


_OK = {"consent_check": lambda _t: True}


# --- PURE ---------------------------------------------------------------------

def test_no_input_raises():
    with pytest.raises(ValueError, match="image_bytes and/or audio_bytes"):
        ingest_cash_book(uuid4(), **_OK)


# --- DB -----------------------------------------------------------------------

pytest.importorskip("dbos")
import psycopg  # noqa: E402

_DB = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — cash_book DB tests skipped",
)


@pytest.fixture(scope="module")
def db_ctx():
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    assert not apply_migrations.apply(dsn=dsn)["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    if not os.environ.get("TEAM_PHONE_ENCRYPTION_KEY"):
        from cryptography.fernet import Fernet

        os.environ["TEAM_PHONE_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield SimpleNamespace(dsn=dsn)
    finally:
        shutdown_dbos()


def _tenant(dsn: str) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return str(conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('VT-59 cash-book test', 'founding', 'onboarding') RETURNING id"
        ).fetchone()[0])


def _phone() -> str:
    return "+9190" + uuid4().int.__str__()[:8]


def _counts(tenant: str) -> tuple[int, int, int]:
    from orchestrator.db import tenant_connection

    with tenant_connection(tenant) as conn:
        cust = conn.execute("SELECT count(*) AS n FROM customers").fetchone()["n"]
        led = conn.execute(
            "SELECT count(*) AS n FROM customer_ledger_entries").fetchone()["n"]
        imp = conn.execute(
            "SELECT count(*) AS n FROM imported_transactions").fetchone()["n"]
    return cust, led, imp


@_DB
def test_image_only_attributes_and_commits(db_ctx):
    tenant = _tenant(db_ctx.dsn)
    phone = _phone()
    extract = _vision({"customer_name": ("Rajesh", 0.95), "phone": (phone, 0.95),
                       "amount": ("500", 0.95), "entry_date": ("2026-06-01", 0.95)})
    s = ingest_cash_book(tenant, image_bytes=b"img", image_extract_fn=extract, **_OK)
    assert s.committed == 1
    cust, led, _ = _counts(tenant)
    assert cust == 1 and led == 1


@_DB
def test_audio_only_narration_attributes(db_ctx):
    tenant = _tenant(db_ctx.dsn)
    phone = _phone()
    client = _FakeAnthropic(_entry(
        customer_name=("Rajesh", 0.95), phone=(phone, 0.95),
        amount=("500", 0.95), entry_date=("2026-06-01", 0.95)))
    s = ingest_cash_book(tenant, audio_bytes=b"aud", transcribe_fn=_transcribe(),
                         anthropic_client=client, **_OK)
    assert s.committed == 1
    cust, led, _ = _counts(tenant)
    assert cust == 1 and led == 1


@_DB
def test_both_merge_attributes(db_ctx):
    tenant = _tenant(db_ctx.dsn)
    phone = _phone()
    extract = _vision({"customer_name": ("Rajesh", 0.9), "amount": ("500", 0.9)})
    merge = _FakeAnthropic(_entry(
        customer_name=("Rajesh", 0.95), phone=(phone, 0.95),
        amount=("500", 0.95), entry_date=("2026-06-01", 0.95)))
    s = ingest_cash_book(tenant, image_bytes=b"img", audio_bytes=b"aud",
                         image_extract_fn=extract, transcribe_fn=_transcribe(),
                         anthropic_client=merge, **_OK)
    assert s.committed == 1
    cust, led, _ = _counts(tenant)
    assert cust == 1 and led == 1


@_DB
def test_unattributed_narration_parked(db_ctx):
    tenant = _tenant(db_ctx.dsn)
    # Narration with an amount but no name/phone → parked (VT-58 seam).
    client = _FakeAnthropic(_entry(
        customer_name=(None, 0.0), phone=(None, 0.0),
        amount=("500", 0.95), entry_date=("2026-06-01", 0.95)))
    s = ingest_cash_book(tenant, audio_bytes=b"aud", transcribe_fn=_transcribe(),
                         anthropic_client=client, **_OK)
    assert s.committed == 0 and s.parked == 1
    cust, led, imp = _counts(tenant)
    assert cust == 0 and led == 0 and imp == 1


@_DB
def test_merge_conflict_routes_to_clarification(db_ctx):
    tenant = _tenant(db_ctx.dsn)
    phone = _phone()
    extract = _vision({"customer_name": ("Rajesh", 0.9), "amount": ("500", 0.9)})
    # Realistic conflict: identity is certain, the AMOUNT disagrees (photo 500 vs
    # voice 700) → only the amount field is low (0.5) → one clarifying question.
    merge = _FakeAnthropic(_entry(
        customer_name=("Rajesh", 0.95), phone=(phone, 0.95),
        amount=("700", 0.5), entry_date=("2026-06-01", 0.95)))
    s = ingest_cash_book(tenant, image_bytes=b"img", audio_bytes=b"aud",
                         image_extract_fn=extract, transcribe_fn=_transcribe(),
                         anthropic_client=merge, **_OK)
    assert s.committed == 0 and s.pending_clarification == 1
    cust, led, _ = _counts(tenant)
    assert cust == 0 and led == 0  # nothing committed on conflict
