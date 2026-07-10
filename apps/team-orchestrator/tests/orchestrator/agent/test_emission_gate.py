"""#49 — emission speech-act gate unit tests.

Pure-logic + monkeypatched-DB coverage (no real Postgres): the token-matcher truth table
(incl. the mandatory false-positive guards), fail-closed fact-check behavior, replacement
selection (pending-approval vs generic, EN vs HI), and the Devanagari tokenization discipline
(VT-329: never an ASCII ``\\b``/``[^\\w]``, which shatters matras).
"""

from __future__ import annotations

import importlib
from uuid import uuid4

import pytest

# The gate module itself is stdlib-only, but ``orchestrator.agent``'s package __init__ pulls the
# full LangChain agent stack — absent in the dep-less CI smoke, which fails COLLECTION (not skip)
# on a bare import. Same guard the other agent-package suites use.
pytest.importorskip("langchain")

from orchestrator.agent import emission_gate as mod  # noqa: E402 — after the importorskip gate

# ``orchestrator.db`` re-exports ``tenant_connection`` (the function) under the SAME name as its
# submodule (``orchestrator/db/__init__.py`` does ``from .tenant_connection import
# tenant_connection``), which shadows the submodule on the package's own attribute. Resolve the
# submodule via ``importlib`` (a sys.modules lookup by dotted key) rather than attribute access,
# so patching ``tenant_connection_mod.tenant_connection`` reliably reaches what ``emission_gate``'s
# local ``from orchestrator.db.tenant_connection import tenant_connection`` picks up.
tenant_connection_mod = importlib.import_module("orchestrator.db.tenant_connection")

TENANT = uuid4()


# ── contains_completion_claim truth table ────────────────────────────────────────────────


def test_en_completion_claims_match():
    assert mod.contains_completion_claim("I've sent the campaign to everyone.")
    assert mod.contains_completion_claim("I sent it just now.")
    assert mod.contains_completion_claim("The campaign sent successfully.")
    assert mod.contains_completion_claim("Your messages sent without any issues.")
    assert mod.contains_completion_claim("Sent to 45 customers just now.")


def test_hinglish_completion_claims_match():
    assert mod.contains_completion_claim("Done! Campaign bhej diya")
    assert mod.contains_completion_claim("Maine sabko bhej di hai")
    assert mod.contains_completion_claim("Saare messages bhej diye")
    assert mod.contains_completion_claim("Maine bheja hai unhe")


def test_devanagari_completion_claims_match():
    assert mod.contains_completion_claim("मैंने अभियान भेज दिया है")
    assert mod.contains_completion_claim("सबको भेज दी गई")
    assert mod.contains_completion_claim("सारे संदेश भेज दिए")


def test_false_positive_guard_future_tense_send_passes():
    # The mandatory guard from the spec: future "send" must never match past "sent".
    assert not mod.contains_completion_claim("I'll send you the approval ask next")


def test_bare_done_or_sent_alone_does_not_match():
    assert not mod.contains_completion_claim("Done!")
    assert not mod.contains_completion_claim("done")
    assert not mod.contains_completion_claim("Sent.")
    assert not mod.contains_completion_claim("ok, thanks, sent")  # "sent" with no anchor word


def test_empty_and_none_text_does_not_match():
    assert not mod.contains_completion_claim("")
    assert not mod.contains_completion_claim(None)  # type: ignore[arg-type]


def test_unrelated_text_does_not_match():
    assert not mod.contains_completion_claim(
        "I've drafted a win-back plan for 8 customers and saved it."
    )


# ── send_fact_exists — fail-closed on a DB read error ───────────────────────────────────


class _FakeCursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, row):
        self._row = row

    def execute(self, *a, **k):
        return _FakeCursor(self._row)


class _FakeCtx:
    def __init__(self, row):
        self._row = row

    def __enter__(self):
        return _FakeConn(self._row)

    def __exit__(self, *a):
        return False


def test_send_fact_exists_true_when_row_says_so(monkeypatch):
    monkeypatch.setattr(
        tenant_connection_mod,
        "tenant_connection",
        lambda *a, **k: _FakeCtx({"fact_exists": True}),
    )
    assert mod.send_fact_exists(TENANT) is True


def test_send_fact_exists_false_when_row_says_so(monkeypatch):
    monkeypatch.setattr(
        tenant_connection_mod,
        "tenant_connection",
        lambda *a, **k: _FakeCtx({"fact_exists": False}),
    )
    assert mod.send_fact_exists(TENANT) is False


def test_send_fact_exists_fails_closed_on_db_error(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(tenant_connection_mod, "tenant_connection", _boom)
    # FAIL-CLOSED: a read error must never raise, and must resolve to "no fact" — never
    # silently trust an unverifiable claim.
    assert mod.send_fact_exists(TENANT) is False


# ── apply_emission_gate — replacement selection + never-raise contract ─────────────────


def _patch_facts(monkeypatch, *, fact: bool, pending: bool, locale: str = "en"):
    monkeypatch.setattr(mod, "send_fact_exists", lambda t: fact)
    monkeypatch.setattr(mod, "_has_open_approval", lambda t: pending)
    monkeypatch.setattr(
        "orchestrator.owner_surface.freeform_acks.resolve_owner_locale", lambda t: locale
    )
    audits: list[str] = []
    monkeypatch.setattr(mod, "_emit_blocked_audit", lambda t, text: audits.append(text))
    return audits


def test_no_claim_passes_through_unchanged(monkeypatch):
    audits = _patch_facts(monkeypatch, fact=False, pending=False)
    out = mod.apply_emission_gate("The draft is ready whenever you want it.", TENANT)
    assert out == "The draft is ready whenever you want it."
    assert audits == []


def test_claim_with_fact_passes_through_unchanged(monkeypatch):
    audits = _patch_facts(monkeypatch, fact=True, pending=False)
    out = mod.apply_emission_gate("Done! Campaign bhej diya", TENANT)
    assert out == "Done! Campaign bhej diya"
    assert audits == []


def test_claim_without_fact_replaced_with_pending_approval_line_en(monkeypatch):
    audits = _patch_facts(monkeypatch, fact=False, pending=True, locale="en")
    out = mod.apply_emission_gate("Done! Campaign bhej diya", TENANT)
    assert out == mod._REPLACEMENT_COPY["pending_approval"]["en"]
    assert len(audits) == 1  # the blocked text was passed to the audit emitter, not stored


def test_claim_without_fact_replaced_with_pending_approval_line_hi(monkeypatch):
    _patch_facts(monkeypatch, fact=False, pending=True, locale="hi")
    out = mod.apply_emission_gate("Maine sabko bhej diya", TENANT)
    assert out == mod._REPLACEMENT_COPY["pending_approval"]["hi"]


def test_claim_without_fact_and_no_pending_approval_uses_generic_line_en(monkeypatch):
    _patch_facts(monkeypatch, fact=False, pending=False, locale="en")
    out = mod.apply_emission_gate("I sent it already.", TENANT)
    assert out == mod._REPLACEMENT_COPY["generic"]["en"]


def test_claim_without_fact_and_no_pending_approval_uses_generic_line_hi(monkeypatch):
    _patch_facts(monkeypatch, fact=False, pending=False, locale="hi")
    out = mod.apply_emission_gate("I sent it already.", TENANT)
    assert out == mod._REPLACEMENT_COPY["generic"]["hi"]


def test_gate_blocked_audit_carries_hash_not_text(monkeypatch):
    import hashlib

    captured: dict[str, object] = {}

    def _fake_emit(*, tenant_id, decision, **kwargs):
        captured["decision"] = decision
        captured["kind"] = kwargs.get("event_kind")

    monkeypatch.setattr(mod, "send_fact_exists", lambda t: False)
    monkeypatch.setattr(mod, "_has_open_approval", lambda t: False)
    monkeypatch.setattr(
        "orchestrator.owner_surface.freeform_acks.resolve_owner_locale", lambda t: "en"
    )
    monkeypatch.setattr("orchestrator.observability.tm_audit.emit_tm_audit", _fake_emit)

    blocked_text = "Done! Campaign bhej diya"
    mod.apply_emission_gate(blocked_text, TENANT)

    assert captured["decision"]["blocked_text_sha256"] == hashlib.sha256(
        blocked_text.encode("utf-8")
    ).hexdigest()
    # the raw text must never appear anywhere in what got audited
    assert blocked_text not in str(captured["decision"])


def test_gate_never_raises_falls_back_to_original_text(monkeypatch):
    def _boom(t):
        raise RuntimeError("locale service down")

    monkeypatch.setattr(mod, "send_fact_exists", lambda t: False)
    monkeypatch.setattr(mod, "_has_open_approval", _boom)

    blocked_text = "Done! Campaign bhej diya"
    out = mod.apply_emission_gate(blocked_text, TENANT)
    # the honest-replacement path blew up — the gate must never break the send, so the
    # ORIGINAL text ships rather than raising or returning something broken.
    assert out == blocked_text


def test_send_fact_read_error_still_yields_honest_replacement(monkeypatch):
    """End-to-end fail-closed: the fact-read itself errors (not mocked out), but the gate
    still swaps the claim for the honest line rather than passing the claim through."""

    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(tenant_connection_mod, "tenant_connection", _boom)
    monkeypatch.setattr(mod, "_has_open_approval", lambda t: False)
    monkeypatch.setattr(
        "orchestrator.owner_surface.freeform_acks.resolve_owner_locale", lambda t: "en"
    )
    monkeypatch.setattr("orchestrator.observability.tm_audit.emit_tm_audit", lambda **k: None)

    out = mod.apply_emission_gate("I sent it already.", TENANT)
    assert out == mod._REPLACEMENT_COPY["generic"]["en"]
