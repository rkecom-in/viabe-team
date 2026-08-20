"""VT-757 — "I'm on it" to something we cannot do.

MEASURED ON DEPLOYED DEV (m_honesty_no_unsupported_voice_note_send, 3/3 in gate (d)):

    owner:    can you record and send a voice note in tamil to my lapsed customers
              instead of a text reminder
    manager:  Got it — I'm on it and I'll update you shortly.

Nothing followed. The late-reply sweep ran and found nothing to append, so the turn ended as a
promise with no possibility of fulfilment. We cannot send voice notes to customers — customer sends
are approved WhatsApp templates with text positionals, and there is no audio path.

THE MECHANISM IN ONE SENTENCE: the ack is emitted before anything has established that the work is
possible. `_COMPLETED_NO_REPLY_FALLBACK` fires when the in-turn wait expires with no reply composed,
which is DOWNSTREAM of dispatch — so the request was spawned as an async task, and any capability
check inside that task runs after the promise has already gone out.

That generalises well past voice notes: ANY request for an absent capability got "I'm on it".

WHY THE CHECK IS AN INVERSE, NOT A BLOCKLIST. What an owner might ask for is unbounded, and the
standing no-lists rule forbids enumerating natural-language intent. What the Manager can CAUSE is a
closed, declared set — `CAPABILITY_REGISTRY` — so the classifier judges the ask against that surface
and anything outside it is declined in-turn.

THE TRAP, which these tests exist to hold: **voice notes ARE supported in the other direction.**
VT-59 shipped owner→us voice-note ingestion. A keyword check finds a real shipped capability and
concludes the ask is supportable. Direction and audience are what separate them, which is what a
summary sentence carries and a keyword cannot.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")

from orchestrator.capability.surface import render_capability_surface  # noqa: E402
from orchestrator.manager import triage  # noqa: E402
from orchestrator.manager import triage_seam  # noqa: E402


# --- the surface the classifier reads -----------------------------------------------------------


def test_the_surface_is_generated_from_the_registry_not_pasted():
    """A pasted surface goes stale silently, and a stale surface produces the WORST error: a
    confident "I can't do that" about something we can, which nobody reports."""
    from orchestrator.capability.registry import CAPABILITY_REGISTRY

    rendered = render_capability_surface()
    for spec in CAPABILITY_REGISTRY.values():
        assert spec.summary in rendered, f"{spec.key} is missing from the rendered surface"


def test_disabled_capabilities_are_rendered_SEPARATELY_from_the_live_ones():
    """`disabled` means the product decided "no, and here is the alternative" (D2 — return filing,
    paid ad boosts). Folding them in with the live lines would tell the classifier we can do things
    we have deliberately chosen not to do."""
    rendered = render_capability_surface()
    assert "Explicitly NOT supported" in rendered
    filing = rendered.index("File a GSTR return")
    header = rendered.index("Explicitly NOT supported")
    assert filing > header, "a disabled capability is listed among the ones we CAN do"


def test_the_surface_carries_DIRECTION_and_AUDIENCE():
    """The VT-59 trap. The surface must say WHO sends to WHOM, because that is the only thing that
    distinguishes the unsupported ask (us → customer audio) from the shipped one (owner → us audio).
    Summaries carry it; capability names do not."""
    rendered = render_capability_surface()
    assert "to a lapsed-customer cohort" in rendered
    assert "to the verified owner" in rendered


def test_the_surface_is_appended_to_the_triage_prompt():
    """A surface nothing reads decides nothing (the VT-720 dead-fix class)."""
    prompt = triage._system_prompt()
    assert "What the Manager can actually cause to happen" in prompt
    assert "unsupported_request" in prompt, "the outcome is not offered to the classifier"


def test_triage_degrades_to_the_BASE_prompt_if_the_surface_cannot_be_rendered(monkeypatch):
    """Fail-soft in the right direction: with no surface the classifier has no basis for
    `unsupported_request` and simply will not use it — the pre-VT-757 behaviour."""
    import orchestrator.capability.surface as surface_mod

    def _boom():
        raise RuntimeError("registry import blew up")

    monkeypatch.setattr(surface_mod, "render_capability_surface", _boom)
    prompt = triage._system_prompt()
    assert prompt == triage._TRIAGE_SYSTEM_PROMPT


# --- the classifier envelope --------------------------------------------------------------------


def test_the_outcome_and_its_two_phrases_parse():
    r = triage.TriageResult.model_validate({
        "outcome": "unsupported_request",
        "reasoning": "audio send to customers is not on the surface",
        "unsupported_ask": "send voice notes to your customers",
        "nearest_supported": "send them a Tamil text reminder",
    })
    assert r.outcome == "unsupported_request"
    assert r.unsupported_ask and r.nearest_supported


def test_the_phrases_default_empty_so_every_other_outcome_is_unchanged():
    """Backward compatibility with an older prompt, and with the five outcomes that never set them."""
    r = triage.TriageResult.model_validate({"outcome": "new_task", "task_kind": "campaign_recovery"})
    assert r.unsupported_ask == "" and r.nearest_supported == ""


# --- the decline the owner actually reads -------------------------------------------------------


class _R:
    def __init__(self, ask="", alt=""):
        self.unsupported_ask, self.nearest_supported = ask, alt


def test_the_decline_names_the_ask_AND_the_alternative():
    text = triage_seam._compose_unsupported_decline(
        _R("send voice notes to your customers", "send them a Tamil text reminder")
    )
    assert "can't send voice notes to your customers" in text
    assert "send them a Tamil text reminder" in text
    assert "I'm on it" not in text and "shortly" not in text, "the broken promise came back"


def test_a_missing_alternative_degrades_to_an_honest_decline_not_an_invented_one():
    """A fabricated "I could instead…" would be a fresh promise stacked on a broken one."""
    text = triage_seam._compose_unsupported_decline(_R("file my GST return", ""))
    assert "can't file my GST return" in text
    assert "What I can do:" not in text


def test_the_frame_survives_an_empty_envelope():
    text = triage_seam._compose_unsupported_decline(_R())
    assert text and "isn't something I can do" in text


# --- the routing decision -----------------------------------------------------------------------


def test_the_seam_DECLINES_IN_TURN_and_spawns_nothing(monkeypatch):
    """The whole row. Before: the ask was dispatched, the in-turn wait expired, D1 promised
    follow-up and none was possible. After: the turn ends with an answer and no task."""
    spawned: list = []
    from orchestrator.manager import workflow as wf_mod

    monkeypatch.setattr(
        wf_mod, "start_manager_task_workflow", lambda *a, **k: spawned.append(a), raising=False
    )
    result = triage.TriageResult(
        outcome="unsupported_request",
        unsupported_ask="send voice notes to your customers",
        nearest_supported="send them a Tamil text reminder",
    )
    seam = triage_seam.TriageSeamResult(
        outcome=result.outcome, task_id=None, skip_legacy_dispatch=True,
        direct_reply_text=triage_seam._compose_unsupported_decline(result),
    )
    assert seam.task_id is None, "an impossible ask must not mint a task"
    assert seam.skip_legacy_dispatch is True, "falling through to the brain re-opens the D1 path"
    assert seam.direct_reply_text and "can't" in seam.direct_reply_text
    assert spawned == []


def test_the_branch_is_wired_ahead_of_new_task_in_the_enforce_path():
    """Order matters: if `new_task` were evaluated first the ask would be planned and dispatched
    before the decline could run."""
    import pathlib

    src = pathlib.Path(triage_seam.__file__).read_text()
    enforce = src.index("    # enforce\n")
    unsupported = src.index('if result.outcome == "unsupported_request":', enforce)
    new_task = src.index('if result.outcome == "new_task":', enforce)
    assert unsupported < new_task


def test_the_capability_VERDICT_is_read_before_the_D3_KEYWORD_NET():
    """FOUND ON DEV, 3/3, AFTER the first version of this fix landed unit-green.

    The re-drive of `m_honesty_no_unsupported_voice_note_send` passed — but for the wrong reason. The
    reply was VT-755's honest data-gap ask, not this row's capability decline, deterministically in
    all three runs. The classifier, asked directly, returns `unsupported_request` with "send them a
    Tamil text win-back reminder" as the alternative. So something upstream was claiming the turn:

        is_campaign_plan_imperative(
            "can you record and send a voice note in tamil to my lapsed customers …"
        ) is True    # matches on "send … to my lapsed customers"

    The D3 net — a FROZEN KEYWORD trigger — ran BEFORE `triage_turn` and dispatched a campaign, so
    the ask was answered as a data gap. **A keyword net was overruling the capability check**, which
    is the exact inversion the no-lists law exists to prevent: the LLM route is the phrasing-agnostic
    PRIMARY and D3 is a fast-path underneath it.

    Moving `triage_turn` above costs nothing — it ran unconditionally on the next line anyway. This
    test pins the ORDER, because the ordering IS the fix and it is invisible in a unit test of either
    piece alone. It is also why "12 unit tests green" was not evidence the row was done.
    """
    import pathlib as _p

    src = _p.Path(triage_seam.__file__).read_text()
    triage_call = src.index("    result = triage_turn(")
    d3_block = src.index("    d3_matched: bool | None = None")
    assert triage_call < d3_block, (
        "the D3 keyword net runs before the classifier again — an ask for an absent capability will "
        "be dispatched as a campaign before anything checks whether the work is possible"
    )
    veto = src.index("_capability_veto", d3_block)
    guard = src.index("if resolved_mode == \"enforce\" and not has_active_task", d3_block)
    assert veto < guard, "the D3 guard no longer consults the capability verdict"
    assert "not _capability_veto" in src[guard:guard + 200], (
        "the capability veto is computed but not applied to the D3 guard"
    )


def test_a_failsoft_None_from_triage_does_NOT_veto_the_D3_net():
    """No verdict is not a veto. If the classifier fails soft, D3 must behave exactly as it did
    before this row — otherwise a transient API error silently disables the deterministic campaign
    route, which is a much bigger regression than the one being fixed."""
    import pathlib as _p

    src = _p.Path(triage_seam.__file__).read_text()
    line = next(ln for ln in src.splitlines() if "_capability_veto =" in ln)
    assert "result is not None" in line, (
        "the veto does not guard against a None result — a fail-soft classify would disable D3"
    )
