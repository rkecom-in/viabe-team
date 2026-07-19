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


# ── VT-657 — send-STATE completion ("gone out"/"went out"), the "sent"-less fabrication class ─────


def test_vt657_send_state_completion_matches():
    # "your campaign has gone out" carries NO "sent" token, so the subject+verb bigrams missed it
    # (j02: the brain claimed a send that never happened). These must now be caught.
    assert mod.contains_completion_claim("Your campaign has gone out to everyone.")
    assert mod.contains_completion_claim("The offer went out this morning.")
    assert mod.contains_completion_claim("Your Diwali campaign has gone out.")


def test_vt657_send_state_negated_or_future_or_out_of_passes_clean():
    # An honest denial, a future/conditional framing, and a non-send "out of …" must NOT match.
    assert not mod.contains_completion_claim("It hasn't gone out yet.")
    assert not mod.contains_completion_claim("It has not gone out yet.")
    assert not mod.contains_completion_claim("It will go out once you approve.")
    assert not mod.contains_completion_claim(
        "The draft is ready and it will go out after you approve."
    )
    assert not mod.contains_completion_claim("The item went out of stock last week.")


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


# ── Layer 1b — never-lie COUNT binding (CL-2026-07-16 money-authority Part B) ───────────────────


def _patch_count(monkeypatch, *, real, locale="en"):
    """Layer-1b setup: a REAL send exists (fact=True so Layer-1 passes), and the DB send count is
    ``real`` (None simulates a read error). Emit-audit mock tolerates the event_kind kwarg."""
    monkeypatch.setattr(mod, "send_fact_exists", lambda t: True)
    monkeypatch.setattr(mod, "send_count_since", lambda t: real)
    monkeypatch.setattr(
        "orchestrator.owner_surface.freeform_acks.resolve_owner_locale", lambda t: locale
    )
    audits: list[tuple] = []
    monkeypatch.setattr(
        mod, "_emit_blocked_audit", lambda t, text, event_kind="": audits.append((text, event_kind))
    )
    return audits


def test_layer1b_truthful_count_passes_through(monkeypatch):
    # j01 false-positive cleared: "sent it to 8" when 8 really went out is TRUTHFUL -> unchanged.
    audits = _patch_count(monkeypatch, real=8)
    txt = "Your campaign has gone out — I sent it to 8 customers."
    assert mod.apply_emission_gate(txt, TENANT) == txt
    assert audits == []


def test_layer1b_overstated_count_rewritten_to_truth_en(monkeypatch):
    # the fabrication: "sent to 40" when only 3 went out -> rewritten to the DB truth, audited.
    audits = _patch_count(monkeypatch, real=3, locale="en")
    out = mod.apply_emission_gate("Done — I sent it to 40 customers!", TENANT)
    assert out == mod._REPLACEMENT_COPY["sent_count_corrected"]["en"].format(n=3)
    assert len(audits) == 1 and audits[0][1] == "emission_sent_count_mismatch_blocked"


def test_layer1b_understated_count_rewritten_to_truth_hi(monkeypatch):
    _patch_count(monkeypatch, real=8, locale="hi")
    out = mod.apply_emission_gate("Maine campaign 2 customers ko bhej diya", TENANT)
    assert out == mod._REPLACEMENT_COPY["sent_count_corrected"]["hi"].format(n=8)


def test_layer1b_db_read_error_skips_binding_no_false_rewrite(monkeypatch):
    # send_count_since None (DB blip) -> NEVER rewrite a possibly-truthful claim to a wrong number.
    _patch_count(monkeypatch, real=None)
    txt = "I sent it to 40 customers."
    assert mod.apply_emission_gate(txt, TENANT) == txt


def test_layer1b_zero_real_count_skips_binding(monkeypatch):
    # real == 0 (no campaign fan-out): Layer-1 governed existence; Layer-1b does not fire (real>0 guard).
    _patch_count(monkeypatch, real=0)
    txt = "I sent it to 40 customers."
    assert mod.apply_emission_gate(txt, TENANT) == txt


def test_layer1b_no_stated_count_passes_through(monkeypatch):
    # a completion claim with a real fact but NO stated count is unchanged (existing behavior kept).
    audits = _patch_count(monkeypatch, real=5)
    assert mod.apply_emission_gate("Done! Campaign bhej diya", TENANT) == "Done! Campaign bhej diya"
    assert audits == []


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


# ── R2 — negation / owner-directed / ability-marker exemptions (LOOSENS the honesty gate) ────────
# THIS is the only build item that loosens the gate. Every exemption is SENTENCE-SCOPED; the full
# existing fabrication suite above stays green and the three exemption-abuse fixtures below are
# merge-blocking.


def test_r2_owner_directed_send_passes():
    # "sent you the connect link" / "aapko … bhej diya" is a message TO THE OWNER (a link/plan), not
    # a customer-send claim — passes clean.
    assert not mod.contains_completion_claim("I've sent you the connect link.")
    assert not mod.contains_completion_claim("Maine aapko link bhej diya hai.")


def test_vt640_owner_artifact_send_no_explicit_you_passes():
    # VT-640 — the exact multi_field_single_message false-positive: a GOOD onboarding reply refers to
    # the connect link the manager already sent the owner ("using the link I sent, then reply 'done'").
    # The sentence has NO explicit "you", so the owner-directed exemption (b) missed it and the
    # ("i","sent") bigram wrongly swapped a perfect answer for the "haven't started" stall. Exemption
    # (d): an owner-FACING artifact (link/approval) send with no customer reference passes.
    assert not mod.contains_completion_claim(
        "The Shopify connection isn't complete yet — approve it in the browser using the link I "
        "sent, then reply 'done'."
    )
    assert not mod.contains_completion_claim("Open the approval I sent and tap confirm.")
    assert not mod.contains_completion_claim("I've sent the OAuth link — reply 'done' when finished.")


def test_vt640_artifact_exemption_does_not_leak_to_customer_sends():
    # MERGE-BLOCKING: the artifact exemption must NOT clear a real customer send that merely also
    # mentions a link, and must NOT clear the "sent to N" trigram or a bare "I sent it".
    assert mod.contains_completion_claim("I sent the offer link to 40 customers.")  # customer ref
    assert mod.contains_completion_claim("Sent to 45 customers via the link.")       # trigram
    assert mod.contains_completion_claim("I sent it just now.")                       # no artifact


def test_r2_ability_framed_send_passes():
    # A subject-less bigram gated by an ability/future marker BEFORE it is a capability statement,
    # not a completed act.
    assert not mod.contains_completion_claim(
        "I can win back customers — messages sent automatically once you approve."
    )


def test_r2_spend_denial_adjacent_negation_passes():
    # An adjacent-negated spend verb is a DENIAL, never a fabricated completion (money severity).
    assert not mod.contains_spend_completion_claim(
        "main bina aapki approval ke ads pe ₹1 bhi kharch nahi karta"
    )


def test_r2_true_positives_still_block():
    # The loosening must not open the real fabrications.
    assert mod.contains_completion_claim("Maine campaign bhej diya — 40 customers ko")
    assert mod.contains_completion_claim("sent to 40 customers")
    assert mod.contains_spend_completion_claim("₹500 boost pe kharch kar diya")


def test_r2_exemption_abuse_fixtures_still_block():
    # MERGE-BLOCKING adversarial set — every exemption is sentence-scoped and must NOT leak:
    # (1) an owner-directed clause + a customer-send claim in the SAME sentence still blocks.
    assert mod.contains_completion_claim("I sent you the campaign — 40 customers reached")
    # (2) an ability marker in sentence 1 must NOT exempt the marker-less claim in sentence 2.
    assert mod.contains_completion_claim(
        "I can send campaigns. Campaign sent to 40 customers last week"
    )
    # (3) a NON-adjacent negation does NOT exempt a spend claim (positional binding).
    assert mod.contains_spend_completion_claim("nahi, ₹500 kharch kar diya")


def test_r2_debt_block_returns_receivables_line_both_locales(monkeypatch):
    # R2 (d) — the fabricated-debt block now returns the schema-truth receivables line (a substantive
    # answer), NOT the task-framed not_started stall.
    text = "Aapke 5 purane customers ka total ₹5,500 overdue hai."
    events = _patch_facts_spend(monkeypatch, locale="en")
    out = mod.apply_emission_gate(text, TENANT)
    assert out == mod._REPLACEMENT_COPY["receivables"]["en"]
    assert out != text
    assert events and events[-1][1] == "emission_fabricated_debt_blocked"

    _patch_facts_spend(monkeypatch, locale="hi")
    assert mod.apply_emission_gate(text, TENANT) == mod._REPLACEMENT_COPY["receivables"]["hi"]


# ── R3 — INTERIM_REPLACEMENT_MARKERS export (interim stalls, not substantive answers) ────────────


def test_r3_interim_replacement_markers_cover_stalls_not_answers():
    markers = mod.INTERIM_REPLACEMENT_MARKERS
    # the interim STALLS are in the set (lowercased, so task_outcome's substring match hits)
    assert mod._REPLACEMENT_COPY["generic"]["en"].lower() in markers
    assert mod._REPLACEMENT_COPY["generic"]["hi"].lower() in markers
    assert mod._REPLACEMENT_COPY["not_started"]["en"].lower() in markers
    assert mod._REPLACEMENT_COPY["not_started"]["hi"].lower() in markers
    # substantive answers (pending_approval + receivables) are deliberately EXCLUDED
    assert mod._REPLACEMENT_COPY["pending_approval"]["en"].lower() not in markers
    assert mod._REPLACEMENT_COPY["receivables"]["en"].lower() not in markers
    # cluster-3c/3d swaps are SUBSTANTIVE answers too — never interim stalls.
    assert mod._REPLACEMENT_COPY["campaign_not_drafted"]["en"].lower() not in markers
    assert mod._REPLACEMENT_COPY["onboarding_incomplete"]["en"].lower() not in markers


# ── cluster-3c (VT-655) contains_campaign_draft_claim truth table ───────────────────────────────


def test_campaign_draft_claim_matches():
    assert mod.contains_campaign_draft_claim("Your plan is ready for approval.")
    assert mod.contains_campaign_draft_claim("I've drafted the offer for your lapsed customers.")
    assert mod.contains_campaign_draft_claim("Great news — the campaign is approved.")
    assert mod.contains_campaign_draft_claim("Reviewed and approved — sending shortly.")
    assert mod.contains_campaign_draft_claim("Campaign taiyaar hai, bas approve kar dijiye.")
    assert mod.contains_campaign_draft_claim("मैंने कैंपेन बना दिया है।")


def test_campaign_draft_verb_noun_combo_matches_with_intervening_word():
    # The self-gate false-NEGATIVE: an intervening word ("Diwali") breaks the adjacency phrase, but
    # the PAST-verb + campaign-noun combo still catches it. High precision, past-tense only.
    assert mod.contains_campaign_draft_claim("I've drafted the Diwali offer for you.")
    assert mod.contains_campaign_draft_claim("I've prepared a festive winback campaign for you.")
    assert mod.contains_campaign_draft_claim("I put together the campaign.")
    assert mod.contains_campaign_draft_claim("Maine aapke liye ek offer banaya hai.")
    assert mod.contains_campaign_draft_claim("मैंने आपके लिए ऑफर बनाया है।")


def test_campaign_draft_future_proposal_does_not_match():
    # PROPOSAL / future — NOT a claim that a draft already exists (phrase AND combo paths).
    assert not mod.contains_campaign_draft_claim("Shall I draft the campaign for you?")
    assert not mod.contains_campaign_draft_claim("I'll draft the offer once you confirm.")
    assert not mod.contains_campaign_draft_claim("Want me to put together a campaign?")
    assert not mod.contains_campaign_draft_claim("Want me to put together a festival offer?")
    assert not mod.contains_campaign_draft_claim("Should I make a Diwali offer for your customers?")
    assert not mod.contains_campaign_draft_claim("Once you approve, your plan is ready to send.")
    # Combo must NOT trip on a generic verb+generic-noun that isn't a campaign draft.
    assert not mod.contains_campaign_draft_claim("I made a note of your plan.")
    assert not mod.contains_campaign_draft_claim("I reviewed the draft you sent.")
    # Unrelated / no draft claim.
    assert not mod.contains_campaign_draft_claim("Your customers are ready to hear from you.")
    assert not mod.contains_campaign_draft_claim("")
    assert not mod.contains_campaign_draft_claim(None)  # type: ignore[arg-type]


# ── cluster-3c campaign_draft_fact_exists — fail-closed on a DB read error ───────────────────────


def test_campaign_draft_fact_exists_true_when_row_says_so(monkeypatch):
    from orchestrator.db.wrappers import CampaignsWrapper

    monkeypatch.setattr(CampaignsWrapper, "has_any_since", lambda self, *a, **k: True)
    assert mod.campaign_draft_fact_exists(TENANT) is True


def test_campaign_draft_fact_exists_false_when_row_says_so(monkeypatch):
    from orchestrator.db.wrappers import CampaignsWrapper

    monkeypatch.setattr(CampaignsWrapper, "has_any_since", lambda self, *a, **k: False)
    assert mod.campaign_draft_fact_exists(TENANT) is False


def test_campaign_draft_fact_exists_fails_closed_on_db_error(monkeypatch):
    from orchestrator.db.wrappers import CampaignsWrapper

    def _boom(self, *a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(CampaignsWrapper, "has_any_since", _boom)
    assert mod.campaign_draft_fact_exists(TENANT) is False


# ── cluster-3c apply_emission_gate — fabricated campaign draft swapped; true draft passes through ──


def test_apply_gate_swaps_campaign_draft_when_no_fact(monkeypatch):
    events = _patch_facts_spend(monkeypatch, locale="en")  # neutralize layers 1/3/3b + audit stub
    monkeypatch.setattr(mod, "campaign_draft_fact_exists", lambda t: False)
    out = mod.apply_emission_gate("Your festival plan is ready for approval.", TENANT)
    assert out == mod._REPLACEMENT_COPY["campaign_not_drafted"]["en"]
    assert out != "Your festival plan is ready for approval."
    assert events and events[-1][1] == "emission_campaign_draft_blocked"


def test_apply_gate_campaign_draft_passthrough_when_fact_exists(monkeypatch):
    # A TRUE draft claim (a real ``campaigns`` row exists) passes through UNCHANGED.
    _patch_facts_spend(monkeypatch, locale="en")
    monkeypatch.setattr(mod, "campaign_draft_fact_exists", lambda t: True)
    text = "Your festival plan is ready for approval."
    assert mod.apply_emission_gate(text, TENANT) == text


def test_apply_gate_campaign_proposal_passthrough(monkeypatch):
    # A future proposal never trips the matcher, so no fact-check / swap even with no draft.
    _patch_facts_spend(monkeypatch, locale="en")
    monkeypatch.setattr(mod, "campaign_draft_fact_exists", lambda t: False)
    text = "Want me to draft a festival campaign for your lapsed customers?"
    assert mod.apply_emission_gate(text, TENANT) == text


def test_apply_gate_swaps_drafted_offer_for_you_when_no_fact(monkeypatch):
    # PIN (self-gate): "I've drafted the offer for you" is a past-tense draft-EXISTS claim — with NO
    # backing ``campaigns`` row it MUST be swapped for the honest line, never shipped as-is.
    events = _patch_facts_spend(monkeypatch, locale="en")
    monkeypatch.setattr(mod, "campaign_draft_fact_exists", lambda t: False)
    out = mod.apply_emission_gate("I've drafted the offer for you.", TENANT)
    assert out == mod._REPLACEMENT_COPY["campaign_not_drafted"]["en"]
    assert out != "I've drafted the offer for you."
    assert events and events[-1][1] == "emission_campaign_draft_blocked"


# ── cluster-3d (VT-656) _reply_asks_a_question — the STRUCTURAL over-fire guard ─────────────────────


def test_reply_asks_a_question_structural():
    # A reply that ADVANCES the turn by asking anything (contains an interrogative) — the good case.
    assert mod._reply_asks_a_question("We found your business is based in Chennai — is that right?")
    assert mod._reply_asks_a_question("Got it. What does your business do?")
    assert mod._reply_asks_a_question("Aapka business kya karta hai?")  # Hinglish, ASCII '?'
    assert mod._reply_asks_a_question("आपका व्यवसाय क्या करता है?")  # Devanagari, ASCII '?'
    assert mod._reply_asks_a_question("You're all set! Anything else I can note?")  # trailing question


def test_reply_asks_a_question_false_for_statements():
    # Question-LESS replies — read as statements/completions, NOT advancing.
    assert not mod._reply_asks_a_question(
        "Thanks — that's everything we need to get started. Here's what I've noted."
    )
    assert not mod._reply_asks_a_question("You're all set!")
    assert not mod._reply_asks_a_question("Onboarding is complete.")
    assert not mod._reply_asks_a_question("Setup ho gaya, ab main aage badhta hoon.")
    assert not mod._reply_asks_a_question("आपका सेटअप हो गया है।")
    assert not mod._reply_asks_a_question("")
    assert not mod._reply_asks_a_question(None)  # type: ignore[arg-type]


# ── cluster-3d apply_emission_gate — premature complete swapped; true complete passes through ─────


class _FakeQ:
    def __init__(self, prompt_en: str, prompt_hi: str):
        self.prompt_en = prompt_en
        self.prompt_hi = prompt_hi


class _FakeDecision:
    def __init__(self, next_question):
        self.next_question = next_question


def _patch_conductor(monkeypatch, decision):
    """Monkeypatch the lazily-imported ``next_question_for_tenant`` at its source module (the gate
    does ``from orchestrator.onboarding.conductor import next_question_for_tenant`` at call time)."""
    monkeypatch.setattr(
        "orchestrator.onboarding.conductor.next_question_for_tenant", lambda t: decision
    )


def test_apply_gate_swaps_premature_onboarding_complete(monkeypatch):
    # Active journey + profile INCOMPLETE (a pending question remains) -> swap for that question.
    events = _patch_facts_spend(monkeypatch, locale="en")
    monkeypatch.setattr(mod, "_onboarding_journey_active", lambda t: True)
    _patch_conductor(
        monkeypatch, _FakeDecision(_FakeQ("What does your business do?", "Aapka business kya karta hai?"))
    )
    out = mod.apply_emission_gate(
        "That's everything we need — setting up your assistant now.", TENANT
    )
    assert out == "What does your business do?"
    assert events and events[-1][1] == "emission_onboarding_incomplete_blocked"


def test_apply_gate_swaps_premature_onboarding_complete_hi(monkeypatch):
    _patch_facts_spend(monkeypatch, locale="hi")
    monkeypatch.setattr(mod, "_onboarding_journey_active", lambda t: True)
    _patch_conductor(
        monkeypatch, _FakeDecision(_FakeQ("What does your business do?", "Aapka business kya karta hai?"))
    )
    out = mod.apply_emission_gate("Sab kuch mil gaya, assistant taiyaar hai.", TENANT)
    assert out == "Aapka business kya karta hai?"


def test_apply_gate_onboarding_passthrough_when_actually_complete(monkeypatch):
    # A TRUE "onboarding complete" (deterministic completion True -> next_question None) passes through.
    _patch_facts_spend(monkeypatch, locale="en")
    monkeypatch.setattr(mod, "_onboarding_journey_active", lambda t: True)
    _patch_conductor(monkeypatch, _FakeDecision(None))
    text = "Onboarding is complete. You're all set!"
    assert mod.apply_emission_gate(text, TENANT) == text


def test_apply_gate_onboarding_inactive_journey_passthrough(monkeypatch):
    # MERGE-BLOCKING false-positive guard: a question-less "all set", but there is NO active onboarding
    # journey (a non-onboarding "all set"), so the fact-check must NOT run and nothing is swapped.
    _patch_facts_spend(monkeypatch, locale="en")
    monkeypatch.setattr(mod, "_onboarding_journey_active", lambda t: False)

    def _boom(t):
        raise AssertionError("next_question_for_tenant must not run when the journey is inactive")

    monkeypatch.setattr("orchestrator.onboarding.conductor.next_question_for_tenant", _boom)
    text = "You're all set — I'll message your customers next."
    assert mod.apply_emission_gate(text, TENANT) == text


def test_apply_gate_onboarding_factcheck_fails_closed(monkeypatch):
    # Inside an active journey, a completion fact-check read error must FAIL-CLOSED to the honest
    # generic continuation (never ship the unverifiable "all set").
    _patch_facts_spend(monkeypatch, locale="en")
    monkeypatch.setattr(mod, "_onboarding_journey_active", lambda t: True)

    def _boom(t):
        raise RuntimeError("conductor down")

    monkeypatch.setattr("orchestrator.onboarding.conductor.next_question_for_tenant", _boom)
    out = mod.apply_emission_gate("Onboarding complete!", TENANT)
    assert out == mod._REPLACEMENT_COPY["onboarding_incomplete"]["en"]


def test_apply_gate_swaps_offlist_completion_phrasing(monkeypatch):
    # THE VT-656 CORE: a false completion phrased in a way NO phrase list enumerated (the real dev
    # transcript: "that's everything we need TO GET STARTED"). The structural guard (active + incomplete
    # + reply asks NO question) catches it REGARDLESS of phrasing — no whack-a-mole.
    events = _patch_facts_spend(monkeypatch, locale="en")
    monkeypatch.setattr(mod, "_onboarding_journey_active", lambda t: True)
    _patch_conductor(
        monkeypatch,
        _FakeDecision(
            _FakeQ("We found your business is based in Chennai — is that right?", "Chennai — sahi hai?")
        ),
    )
    out = mod.apply_emission_gate(
        "Thanks — that's everything we need to get started. Here's what I've noted: B2B wholesale.",
        TENANT,
    )
    assert out == "We found your business is based in Chennai — is that right?"
    assert events and events[-1][1] == "emission_onboarding_incomplete_blocked"


def test_apply_gate_onboarding_reply_already_asks_question_not_degraded(monkeypatch):
    # OVER-FIRE GUARD (the critical regression risk): the GOOD conductor turn — active + INCOMPLETE, but
    # the reply already ASKS the pending question. It must pass through UNTOUCHED (never degraded /
    # double-asked), even though the profile is deterministically incomplete.
    _patch_facts_spend(monkeypatch, locale="en")
    monkeypatch.setattr(mod, "_onboarding_journey_active", lambda t: True)

    def _boom(t):
        raise AssertionError("swap must not run when the reply already asks a question")

    monkeypatch.setattr("orchestrator.onboarding.conductor.next_question_for_tenant", _boom)
    text = "Got it — B2B wholesale. We found your business is based in Chennai — is that right?"
    assert mod.apply_emission_gate(text, TENANT) == text
