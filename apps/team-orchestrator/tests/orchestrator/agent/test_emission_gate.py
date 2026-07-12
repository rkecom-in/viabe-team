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


# ── contains_fabricated_debt_framing (cluster-2a) — invented customer ₹ debt ─────────────


def test_fabricated_customer_debt_matches():
    # The sr_stop_then_resume breaker (all 3 runs): a ₹ "overdue"/"pending" aggregate on lapsed
    # customers who owe nothing. Needs debt-word + ₹ figure + customer reference (all present).
    assert mod.contains_fabricated_debt_framing(
        "Aapke 5 purane customers ka total ₹5,500 overdue hai (30+ din se)."
    )
    assert mod.contains_fabricated_debt_framing(
        "aapke 5 purane customers ka total ₹5,500 payment pending hai (30+ din se)"
    )
    assert mod.contains_fabricated_debt_framing(
        "Your 8 customers have an outstanding balance of ₹12,000."
    )
    assert mod.contains_fabricated_debt_framing(
        "customers ka total 5500 rupaye bakaya hai"
    )


def test_legit_money_text_does_not_match():
    # Recovery estimate: ₹ + customers but NO debt word.
    assert not mod.contains_fabricated_debt_framing(
        "I've drafted a campaign for 4 customers with an expected recovery of ₹250–₹750."
    )
    # Agent pricing: ₹ but no customer ref, no debt word.
    assert not mod.contains_fabricated_debt_framing("₹5,000/month per agent, one-month free trial.")
    # Finance answer about the OWNER's OWN payables: debt word + ₹ but NO customer reference.
    assert not mod.contains_fabricated_debt_framing("Aapka ₹10,000 ka payment pending hai supplier ko.")
    # Debt word + customers but NO ₹ figure (vaguer — needs the invented aggregate).
    assert not mod.contains_fabricated_debt_framing("Some customers may have a pending payment.")
    assert not mod.contains_fabricated_debt_framing("")
    assert not mod.contains_fabricated_debt_framing(None)  # type: ignore[arg-type]


# ── contains_spend_completion_claim (cluster-2b) — fabricated ad-spend/boost completion ───


def test_spend_completion_hinglish_matches():
    assert mod.contains_spend_completion_claim("Aapka boost kar diya")
    assert mod.contains_spend_completion_claim("500 rupaye ka boost kar diya hai")
    assert mod.contains_spend_completion_claim("paisa de diya")
    assert mod.contains_spend_completion_claim("ad chala diya")


def test_spend_completion_english_matches():
    assert mod.contains_spend_completion_claim("Boosted and paid ₹500")
    assert mod.contains_spend_completion_claim("The boost is live")
    assert mod.contains_spend_completion_claim("Payment successful")
    assert mod.contains_spend_completion_claim("₹500 has been spent")


def test_spend_completion_devanagari_matches():
    assert mod.contains_spend_completion_claim("बूस्ट कर दिया")


def test_spend_completion_verb_adref_amount_combo():
    # spend verb + ad reference + amount (₹ or bare) all present.
    assert mod.contains_spend_completion_claim("Spent ₹500 on the boost")
    assert mod.contains_spend_completion_claim("Paid 500 for your ad")


def test_spend_completion_false_positive_guards():
    # Future proposal / awaiting approval — NOT a completion.
    assert not mod.contains_spend_completion_claim("₹500 ka boost karne ke liye approval chahiye")
    assert not mod.contains_spend_completion_claim("I'll get the ₹500 boost approved")
    assert not mod.contains_spend_completion_claim("₹500 ka boost approval milte hi kar dunga")
    # Honest non-spend completion (drafting) — no spend phrase, no verb+adref combo.
    assert not mod.contains_spend_completion_claim("draft kar diya")
    assert not mod.contains_spend_completion_claim("Aapka draft ready hai")
    # A legit customer-spend REPORT: spend verb + amount but NO ad reference → not a boost claim.
    assert not mod.contains_spend_completion_claim("Your customers spent ₹2000 total this month")
    assert not mod.contains_spend_completion_claim("")
    assert not mod.contains_spend_completion_claim(None)  # type: ignore[arg-type]


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


def _patch_facts(monkeypatch, *, fact: bool, pending: bool, active: bool = False, locale: str = "en"):
    monkeypatch.setattr(mod, "send_fact_exists", lambda t: fact)
    monkeypatch.setattr(mod, "_has_open_approval", lambda t: pending)
    monkeypatch.setattr(mod, "_has_active_task", lambda t: active)
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


def test_claim_without_fact_no_approval_but_active_task_uses_generic_line_en(monkeypatch):
    # cluster-4c: a task IS running -> "still working" is honest.
    _patch_facts(monkeypatch, fact=False, pending=False, active=True, locale="en")
    out = mod.apply_emission_gate("I sent it already.", TENANT)
    assert out == mod._REPLACEMENT_COPY["generic"]["en"]


def test_claim_without_fact_no_approval_but_active_task_uses_generic_line_hi(monkeypatch):
    _patch_facts(monkeypatch, fact=False, pending=False, active=True, locale="hi")
    out = mod.apply_emission_gate("I sent it already.", TENANT)
    assert out == mod._REPLACEMENT_COPY["generic"]["hi"]


def test_claim_without_fact_no_approval_no_active_task_uses_not_started_line_en(monkeypatch):
    # cluster-4c (consent_natural / routing_db_proof): NOTHING is running -> "still working" would
    # be a false stall; ship the honest "haven't started" line instead.
    _patch_facts(monkeypatch, fact=False, pending=False, active=False, locale="en")
    out = mod.apply_emission_gate("I sent it already.", TENANT)
    assert out == mod._REPLACEMENT_COPY["not_started"]["en"]


def test_claim_without_fact_no_approval_no_active_task_uses_not_started_line_hi(monkeypatch):
    _patch_facts(monkeypatch, fact=False, pending=False, active=False, locale="hi")
    out = mod.apply_emission_gate("I sent it already.", TENANT)
    assert out == mod._REPLACEMENT_COPY["not_started"]["hi"]


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

    # The fact-read AND the active-task read both error (both go through tenant_connection); the
    # gate still swaps to an honest line — not_started (the fail-closed side: never claim work that
    # isn't happening) — rather than passing the fabricated claim through.
    out = mod.apply_emission_gate("I sent it already.", TENANT)
    assert out == mod._REPLACEMENT_COPY["not_started"]["en"]


# ── cluster-2b apply_emission_gate — fabricated spend/boost completion is swapped ───────────


def _patch_facts_spend(monkeypatch, *, pending: bool = False, active: bool = False, locale="en"):
    """Like _patch_facts but the audit stub accepts the event_kind kwarg the spend/debt layers pass."""
    monkeypatch.setattr(mod, "send_fact_exists", lambda t: False)
    monkeypatch.setattr(mod, "_has_open_approval", lambda t: pending)
    monkeypatch.setattr(mod, "_has_active_task", lambda t: active)
    monkeypatch.setattr(
        "orchestrator.owner_surface.freeform_acks.resolve_owner_locale", lambda t: locale
    )
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(
        mod, "_emit_blocked_audit",
        lambda t, text, event_kind="emission_claim_blocked": events.append((text, event_kind)),
    )
    return events


def test_apply_gate_swaps_spend_claim(monkeypatch):
    events = _patch_facts_spend(monkeypatch, pending=False, active=False, locale="en")
    out = mod.apply_emission_gate("Boost kar diya, ₹500 spent!", TENANT)
    # Whole-message honest swap (no approval, no active task -> not_started line), NOT the original.
    assert out == mod._REPLACEMENT_COPY["not_started"]["en"]
    assert out != "Boost kar diya, ₹500 spent!"
    assert events and events[-1][1] == "emission_spend_claim_blocked"


def test_apply_gate_spend_proposal_passthrough(monkeypatch):
    _patch_facts_spend(monkeypatch)
    text = "₹500 ka boost approval milte hi kar dunga."
    assert mod.apply_emission_gate(text, TENANT) == text


# ── #58 (T7) contains_phantom_promise truth table ──────────────────────────────────────────


def test_phantom_promise_en_matches():
    assert mod.contains_phantom_promise("I don't have that — I'll follow up shortly.")
    assert mod.contains_phantom_promise("Let me have the team confirm the exact details.")
    assert mod.contains_phantom_promise("Good question — I'll get back to you on that.")
    assert mod.contains_phantom_promise("I'll circle back once I know more.")
    assert mod.contains_phantom_promise("The team will confirm and let you know.")
    assert mod.contains_phantom_promise("I'll have the team look into it.")


def test_phantom_promise_hinglish_matches():
    assert mod.contains_phantom_promise("Main pata karke bataunga.")
    assert mod.contains_phantom_promise("Team se confirm karke follow up karunga.")
    assert mod.contains_phantom_promise("Baad me bataunga aapko.")


def test_phantom_promise_false_positive_guards():
    # A bare follow-up QUESTION offer is not a promissory deferral.
    assert not mod.contains_phantom_promise("Any follow-up questions? Happy to help.")
    # Immediate action (no deferral) must pass clean.
    assert not mod.contains_phantom_promise("I'll send you the connect link right now.")
    assert not mod.contains_phantom_promise("Want me to set it up now?")
    # "the team" alone (no promissory verb) is fine — the specialist agents are colloquially a team.
    assert not mod.contains_phantom_promise("Your Sales Recovery agent is on the team.")
    assert not mod.contains_phantom_promise("")
    assert not mod.contains_phantom_promise(None)  # type: ignore[arg-type]


# ── #58 (T7) apply_emission_gate — phantom-promise sentence strip ───────────────────────────


def _patch_no_completion_claim_path(monkeypatch, *, locale: str = "en"):
    """Neutralize layer-1 (no completion claim / fact irrelevant) and capture strip audits."""
    monkeypatch.setattr(mod, "send_fact_exists", lambda t: True)  # layer-1 never fires
    monkeypatch.setattr(mod, "_has_open_approval", lambda t: False)
    monkeypatch.setattr(
        "orchestrator.owner_surface.freeform_acks.resolve_owner_locale", lambda t: locale
    )
    audits: list[tuple[str, str]] = []
    monkeypatch.setattr(
        mod,
        "_emit_blocked_audit",
        lambda t, text, event_kind="emission_claim_blocked": audits.append((event_kind, text)),
    )
    return audits


def test_phantom_promise_trailing_clause_is_stripped_keeping_honest_remainder(monkeypatch):
    audits = _patch_no_completion_claim_path(monkeypatch)
    text = (
        "I don't have the exact GST filing date for your state. "
        "The full details are on the portal at viabe.ai/team. "
        "I'll have the team confirm and follow up."
    )
    out = mod.apply_emission_gate(text, TENANT)
    assert "follow up" not in out.lower()
    assert "the team" not in out.lower()
    assert "GST filing date" in out
    assert "viabe.ai/team" in out
    assert audits and audits[0][0] == "emission_phantom_promise_stripped"


def test_phantom_promise_whole_message_falls_back_to_generic(monkeypatch):
    audits = _patch_no_completion_claim_path(monkeypatch, locale="en")
    out = mod.apply_emission_gate("I'll have the team confirm and get back to you.", TENANT)
    assert out == mod._REPLACEMENT_COPY["generic"]["en"]
    assert audits and audits[0][0] == "emission_phantom_promise_stripped"


def test_phantom_promise_whole_message_generic_hi(monkeypatch):
    _patch_no_completion_claim_path(monkeypatch, locale="hi")
    out = mod.apply_emission_gate("Main pata karke bataunga.", TENANT)
    assert out == mod._REPLACEMENT_COPY["generic"]["hi"]


def test_no_phantom_promise_passes_through_unchanged(monkeypatch):
    _patch_no_completion_claim_path(monkeypatch)
    text = "Your Google Sheet isn't connected yet. Want me to set it up now?"
    assert mod.apply_emission_gate(text, TENANT) == text


def test_completion_claim_layer_precedes_phantom_strip(monkeypatch):
    # A fabricated "sent" claim + a phantom clause: layer-1 fires first (no fact) and swaps the
    # WHOLE message, so we never reach the strip.
    monkeypatch.setattr(mod, "send_fact_exists", lambda t: False)
    monkeypatch.setattr(mod, "_has_open_approval", lambda t: False)
    monkeypatch.setattr(mod, "_has_active_task", lambda t: False)
    monkeypatch.setattr(
        "orchestrator.owner_surface.freeform_acks.resolve_owner_locale", lambda t: "en"
    )
    monkeypatch.setattr(mod, "_emit_blocked_audit", lambda *a, **k: None)
    out = mod.apply_emission_gate("I sent it. I'll follow up shortly.", TENANT)
    # Layer-1 swapped the WHOLE message (not the phantom-stripped text) — no active task -> not_started.
    assert out == mod._REPLACEMENT_COPY["not_started"]["en"]


def test_true_send_claim_still_strips_trailing_phantom_promise(monkeypatch):
    # A REAL send (fact exists) with a trailing phantom promise: layer-1 passes, layer-2 strips
    # only the phantom sentence, keeping the true claim.
    audits = _patch_no_completion_claim_path(monkeypatch)  # send_fact_exists -> True
    out = mod.apply_emission_gate("Sent to 45 customers. I'll follow up with you soon.", TENANT)
    assert "Sent to 45 customers" in out
    assert "follow up" not in out.lower()
    assert audits and audits[0][0] == "emission_phantom_promise_stripped"


def test_phantom_strip_never_raises_falls_back_to_original(monkeypatch):
    monkeypatch.setattr(mod, "send_fact_exists", lambda t: True)  # skip layer-1

    def _boom(*a, **k):
        raise RuntimeError("strip blew up")

    monkeypatch.setattr(mod, "_split_sentences", _boom)
    text = "I'll follow up shortly."
    # the strip path raises -> the gate's outer guard ships the ORIGINAL text, never breaks.
    assert mod.apply_emission_gate(text, TENANT) == text
