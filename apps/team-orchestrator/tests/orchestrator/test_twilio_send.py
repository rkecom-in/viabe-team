"""VT-3.3c tests — utils/twilio_send.send_template_message.

Require a live Postgres via ``DATABASE_URL`` plus the dbos / twilio stack;
run in the CI ``orchestrator`` job. No live Twilio call is made — the client
is stubbed via the ``twilio_create`` fixture (conftest.py).
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

pytest.importorskip("dbos")
pytest.importorskip("twilio")

import psycopg  # noqa: E402 — imported after the dependency skip guards

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — twilio_send tests skipped",
)


@pytest.fixture(scope="module")
def send_ctx():
    """Apply migrations + launch DBOS so @DBOS.step / get_pool() work."""
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield SimpleNamespace(dsn=dsn)
    finally:
        shutdown_dbos()


def _new_tenant(dsn: str, whatsapp_number: str) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at, "
            "whatsapp_number) VALUES ('VT-3.3c Send Test', 'founding', 'trial', "
            "now(), %s) RETURNING id",
            (whatsapp_number,),
        ).fetchone()
    assert row is not None
    return str(row[0])


# team_status_ping declares 3 positional vars; VT-400 fail-closed needs them all present.
_PING_PARAMS = {
    "owner_name": "Asha",
    "last_activity_description": "reviewed your sales",
    "next_up_description": "draft a campaign",
}


def test_send_returns_success_when_content_sid_present(send_ctx, twilio_create):
    from orchestrator.utils.twilio_send import send_template_message

    result = send_template_message(
        uuid4(), "team_opt_out_confirmation", {"owner_name": "Asha"}, recipient_phone="+919812300001"
    )
    assert result.success is True
    assert result.message_sid == "SM" + "0" * 32
    assert result.template_name == "team_opt_out_confirmation"
    twilio_create.assert_called_once()


def test_send_returns_stub_when_content_sid_null(send_ctx, twilio_create, monkeypatch):
    from types import SimpleNamespace

    from orchestrator.utils import twilio_send

    # VT-163: send_template_message resolves via the registry (_registry_resolve),
    # not the legacy _templates() dict. Monkeypatch the resolver to return a
    # registered template whose content_sid is null (pending Meta approval) so
    # the stub path is exercised.
    monkeypatch.setattr(
        twilio_send,
        "_registry_resolve",
        lambda name, lang="en": SimpleNamespace(content_sid=None, audience="owner"),
    )
    result = twilio_send.send_template_message(
        uuid4(), "team_pending", {}, recipient_phone="+919812300002"
    )
    assert result.success is False
    assert result.error_code == "template_not_yet_approved"
    twilio_create.assert_not_called()


def test_send_raises_when_template_not_in_yaml(send_ctx, twilio_create):
    from orchestrator.utils.twilio_send import (
        TemplateNotConfigured,
        send_template_message,
    )

    with pytest.raises(TemplateNotConfigured):
        send_template_message(
            uuid4(), "no_such_template", {}, recipient_phone="+919812300003"
        )


def test_send_handles_permanent_twilio_error_4xx(send_ctx, twilio_create):
    from twilio.base.exceptions import TwilioRestException

    from orchestrator.utils.twilio_send import send_template_message

    twilio_create.side_effect = TwilioRestException(
        status=400, uri="/Messages", msg="invalid 'To' number", code=21211
    )
    result = send_template_message(
        uuid4(), "team_status_ping", _PING_PARAMS, recipient_phone="+919812300004"
    )
    assert result.success is False
    assert result.error_code == "21211"
    assert "invalid" in (result.error_message or "")


def test_send_propagates_transient_twilio_error_5xx(send_ctx, twilio_create):
    from twilio.base.exceptions import TwilioRestException

    from orchestrator.utils.twilio_send import send_template_message

    twilio_create.side_effect = TwilioRestException(
        status=503, uri="/Messages", msg="service unavailable", code=20500
    )
    with pytest.raises(TwilioRestException):
        send_template_message(
            uuid4(), "team_status_ping", _PING_PARAMS, recipient_phone="+919812300005"
        )


def test_send_uses_tenant_whatsapp_number_by_default(send_ctx, twilio_create):
    from orchestrator.utils.twilio_send import send_template_message

    number = "+919876543210"
    tenant_id = _new_tenant(send_ctx.dsn, number)
    send_template_message(UUID(tenant_id), "team_status_ping", _PING_PARAMS)
    # VT-399: from_/to ride the WhatsApp channel (whatsapp:-prefixed).
    assert twilio_create.call_args.kwargs["to"] == f"whatsapp:{number}"


def test_send_uses_recipient_phone_override(send_ctx, twilio_create):
    from orchestrator.utils.twilio_send import send_template_message

    override = "+919800000099"
    send_template_message(
        uuid4(), "team_status_ping", _PING_PARAMS, recipient_phone=override
    )
    assert twilio_create.call_args.kwargs["to"] == f"whatsapp:{override}"


def test_recipient_phone_is_tokenised_in_result(send_ctx, twilio_create):
    from orchestrator.utils.twilio_send import send_template_message

    phone = "+919811112222"
    result = send_template_message(
        uuid4(), "team_status_ping", _PING_PARAMS, recipient_phone=phone
    )
    assert result.recipient_phone_token.startswith("phone_tok_")
    assert phone not in result.recipient_phone_token


# --------------------------------------------------------------------------- #
# VT-393: the optional ``language`` param. Backward-compat (every existing
# caller keeps "en") + a ``hi`` call resolves the Hindi variant SID.
# --------------------------------------------------------------------------- #


def test_language_param_defaults_to_en_when_omitted(send_ctx, twilio_create, monkeypatch):
    """Backward-compat: a caller that omits ``language`` resolves the "en" variant —
    the pre-VT-393 implicit behaviour. The resolver is spied to assert the language
    threaded into _registry_resolve is exactly "en"."""
    from orchestrator.utils import twilio_send

    seen: list[str] = []
    real_resolve = twilio_send._registry_resolve

    def _spy(name, lang):
        seen.append(lang)
        return real_resolve(name, lang)

    monkeypatch.setattr(twilio_send, "_registry_resolve", _spy)
    twilio_send.send_template_message(
        uuid4(), "team_status_ping", _PING_PARAMS, recipient_phone="+919812300011"
    )
    assert seen == ["en"], "an omitted language must resolve the 'en' variant"


def test_language_param_resolves_hi_variant_sid(send_ctx, twilio_create):
    """A ``language="hi"`` call resolves the Hindi variant SID (team_welcome has both
    EN+HI). The resolved hi content_sid (NOT the en one) is what Twilio is asked to send."""
    from orchestrator.templates_registry import resolve
    from orchestrator.utils.twilio_send import send_template_message

    en_sid = resolve("team_welcome", "en").content_sid
    hi_sid = resolve("team_welcome", "hi").content_sid
    assert en_sid and hi_sid and en_sid != hi_sid  # the yaml has two distinct SIDs

    result = send_template_message(
        uuid4(),
        "team_welcome",
        {"owner_name": "Asha", "trial_end_date": "2026-07-14"},
        recipient_phone="+919812300012",
        language="hi",
    )
    assert result.success is True
    # The hi (not en) SID is the content_sid Twilio was asked to send.
    assert twilio_create.call_args.kwargs["content_sid"] == hi_sid
    assert twilio_create.call_args.kwargs["content_sid"] != en_sid


# --------------------------------------------------------------------------- #
# VT-399: WhatsApp channel prefix on BOTH from_ and to. A raw E.164 misroutes to
# SMS and fails (the live welcome failed Twilio error 21659). FROM env stays plain.
# --------------------------------------------------------------------------- #


def test_template_send_prefixes_whatsapp_on_both_ends(send_ctx, twilio_create):
    from orchestrator.utils.twilio_send import send_template_message

    send_template_message(uuid4(), "team_status_ping", _PING_PARAMS, recipient_phone="+919812300013")
    kwargs = twilio_create.call_args.kwargs
    assert kwargs["from_"] == "whatsapp:+910000000000"  # FROM env is plain; prefixed at call site
    assert kwargs["to"] == "whatsapp:+919812300013"


def test_freeform_send_prefixes_whatsapp_on_both_ends(send_ctx, twilio_create):
    from orchestrator.utils.twilio_send import send_freeform_message

    send_freeform_message("onboarding question 1?", "+919812300014")
    kwargs = twilio_create.call_args.kwargs
    assert kwargs["from_"] == "whatsapp:+910000000000"
    assert kwargs["to"] == "whatsapp:+919812300014"


def test_wa_prefix_is_idempotent():
    from orchestrator.utils.twilio_send import _wa

    assert _wa("+918108084223") == "whatsapp:+918108084223"
    assert _wa("whatsapp:+918108084223") == "whatsapp:+918108084223"  # never double-prefixes


# --------------------------------------------------------------------------- #
# VT-487: transport-level E.164 fail-closed guard. A malformed/corrupted recipient
# (e.g. a scientific-notation float artifact, the six Twilio 21211 failures) is BLOCKED
# before reaching Twilio. Both from_ and to are validated; the body/copy never leaks.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "bad",
    [
        "+91998886e+11",   # scientific-notation float artifact — the breach
        "+919988.6e11",    # decimal-point artifact
        "999886123456",    # no leading '+'
        "+0123456789",     # E.164 country code cannot start with 0
        "+12",             # too short
        "",                # empty
        "whatsapp:+91garbage",  # non-digit body
    ],
)
def test_wa_blocks_non_e164_failclosed(bad):
    from orchestrator.utils.twilio_send import BlockedRecipientError, _wa

    with pytest.raises(BlockedRecipientError):
        _wa(bad)


def test_wa_blocks_non_e164_message_is_pii_safe():
    """The raised error must NOT carry the raw plaintext number (CL-390) — only a token + last-4."""
    from orchestrator.utils.twilio_send import BlockedRecipientError, _wa

    raw = "+91998886e+11"
    with pytest.raises(BlockedRecipientError) as ei:
        _wa(raw)
    msg = str(ei.value)
    assert raw not in msg  # the full corrupted number never appears in the message
    assert "phone_tok_" in msg  # the hashed token does
    assert "..e+11"[-4:] in msg or "..+11" in msg  # last-4 fragment only


def test_template_send_to_corrupted_number_is_blocked(send_ctx, twilio_create):
    """A scientific-notation recipient (the float-corruption breach) fails CLOSED at the transport —
    BlockedRecipientError, and messages.create is NEVER called (nothing reaches Twilio)."""
    from orchestrator.utils.twilio_send import BlockedRecipientError, send_template_message

    with pytest.raises(BlockedRecipientError):
        send_template_message(
            uuid4(), "team_status_ping", _PING_PARAMS, recipient_phone="+91998886e+11"
        )
    twilio_create.assert_not_called()


def test_freeform_send_to_corrupted_number_is_blocked(send_ctx, twilio_create):
    from orchestrator.utils.twilio_send import BlockedRecipientError, send_freeform_message

    with pytest.raises(BlockedRecipientError):
        send_freeform_message("re-engage?", "+91998886e+11")
    twilio_create.assert_not_called()


def test_valid_e164_still_sends(send_ctx, twilio_create):
    """The guard never blocks a well-formed number — the happy path is unchanged."""
    from orchestrator.utils.twilio_send import send_template_message

    send_template_message(uuid4(), "team_status_ping", _PING_PARAMS, recipient_phone="+919321553267")
    twilio_create.assert_called_once()


# --------------------------------------------------------------------------- #
# VT-486: team_reengage out-of-window owner re-engagement template resolves + sends.
# --------------------------------------------------------------------------- #


def test_team_reengage_resolves_in_registry():
    from orchestrator.templates_registry import resolve

    en = resolve("team_reengage", "en")
    hi = resolve("team_reengage", "hi")
    assert en.content_sid == "HXbdb250089fafc02a0d75ce6817e9ce11"
    assert hi.content_sid == "HX27a50d65fedbb7b6a3c2fb6a6a24f13c"
    assert en.audience == "owner"
    assert en.agent_selectable is False  # system-invoked, never agent-chosen
    assert en.variables == ("owner_name",)


def test_team_reengage_sends_owner_name_positional(send_ctx, twilio_create):
    import json

    from orchestrator.utils.twilio_send import send_template_message

    send_template_message(
        uuid4(), "team_reengage", {"owner_name": "Sundaram"}, recipient_phone="+919321553267"
    )
    kwargs = twilio_create.call_args.kwargs
    assert kwargs["content_sid"] == "HXbdb250089fafc02a0d75ce6817e9ce11"
    assert kwargs["to"] == "whatsapp:+919321553267"
    assert kwargs["from_"] == "whatsapp:+910000000000"
    assert json.loads(kwargs["content_variables"]) == {"1": "Sundaram"}


# --------------------------------------------------------------------------- #
# VT-400: named params -> POSITIONAL content_variables. Named keys are ignored by
# Twilio (it renders the template SAMPLE, "Hi Raj Cafe"); we map onto {{1}}/{{2}}.
# --------------------------------------------------------------------------- #


def test_content_variables_are_positional(send_ctx, twilio_create):
    import json

    from orchestrator.utils.twilio_send import send_template_message

    send_template_message(
        uuid4(),
        "team_welcome",
        {"owner_name": "Sundaram", "trial_end_date": "2026-07-14"},
        recipient_phone="+919812300015",
    )
    sent = json.loads(twilio_create.call_args.kwargs["content_variables"])
    # team_welcome variables order = (owner_name, trial_end_date) -> positions 1, 2.
    assert sent == {"1": "Sundaram", "2": "2026-07-14"}
    assert "owner_name" not in sent  # never the named keys Twilio would ignore


def test_content_variables_positional_three_vars(send_ctx, twilio_create):
    import json

    from orchestrator.utils.twilio_send import send_template_message

    send_template_message(
        uuid4(), "team_status_ping", _PING_PARAMS, recipient_phone="+919812300016"
    )
    sent = json.loads(twilio_create.call_args.kwargs["content_variables"])
    assert sent == {
        "1": "Asha",
        "2": "reviewed your sales",
        "3": "draft a campaign",
    }


def test_missing_template_var_omits_that_position(send_ctx, twilio_create):
    """A declared var absent from params -> its position is OMITTED (never invented), and the send
    still proceeds (VT-400 deferred strict fail-closed; the absent {{n}} renders its Twilio sample
    until each partial-param sender is completed). The PRESENT vars still map to real values."""
    import json

    from orchestrator.utils.twilio_send import send_template_message

    send_template_message(
        uuid4(),
        "team_welcome",
        {"owner_name": "Sundaram"},  # trial_end_date missing
        recipient_phone="+919812300017",
    )
    sent = json.loads(twilio_create.call_args.kwargs["content_variables"])
    assert sent == {"1": "Sundaram"}  # position 2 omitted, not a sample/placeholder
    twilio_create.assert_called_once()
