"""VT-683 P3 — ``owner_surface.wakeup``: locale→variant routing (hinglish→hing), param building
(floor-1), the ≤1/day predicate + DB gate, and fail-closed unconfigured handling. Send/DB seams are
monkeypatched — deterministic and dep-guarded."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

# send_wakeup lazily imports owner_send -> twilio_send (dbos/twilio) and reads the yaml registry;
# wakeup_due lazily imports tenant_connection (psycopg). Guard so the dep-less smoke skips cleanly.
pytest.importorskip("dbos")
pytest.importorskip("twilio")
pytest.importorskip("psycopg")

from orchestrator.owner_surface import wakeup as wk  # noqa: E402

_TID = "22222222-2222-2222-2222-222222222222"
_NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)


# --- wakeup_language: locale -> registry variant (hinglish -> hing when the registry resolves it) ---


@pytest.mark.parametrize(
    "locale,expected",
    [("en", "en"), ("hi", "hi"), ("hinglish", "hing"), ("gibberish", "en")],
)
def test_wakeup_language_maps_locale_to_variant(monkeypatch, locale, expected) -> None:
    import orchestrator.owner_surface.owner_locale as ol

    monkeypatch.setattr(ol, "resolve_owner_locale", lambda _t: locale)
    # team_wakeup2 registers an approved 'hing' SID, so hinglish resolves to the hing variant.
    assert wk.wakeup_language(_TID) == expected


def test_wakeup_language_falls_back_to_en_when_variant_absent(monkeypatch) -> None:
    """If the locale maps to a variant the registry can't resolve, fall back to 'en' (never fail)."""
    import orchestrator.owner_surface.owner_locale as ol
    import orchestrator.templates_registry as reg

    monkeypatch.setattr(ol, "resolve_owner_locale", lambda _t: "hinglish")
    real = reg.resolve

    def _no_hing(name, lang, **k):
        if lang == "hing":
            raise reg.UnknownLanguageVariantError(name, lang)
        return real(name, lang, **k)

    monkeypatch.setattr(reg, "resolve", _no_hing)
    assert wk.wakeup_language(_TID) == "en"


# --- send_wakeup: builds {owner_name, pending_count(str, floored to 1)}, dispatches team_wakeup2 ---


def test_send_wakeup_builds_params_and_dispatches(monkeypatch) -> None:
    monkeypatch.setattr(wk, "wakeup_language", lambda _t: "en")
    captured: dict[str, Any] = {}
    import orchestrator.owner_surface.owner_send as os_mod

    def _fake(tenant_id, template_name, language, params, *, recipient_phone):
        captured.update(
            template_name=template_name, language=language, params=params,
            recipient_phone=recipient_phone,
        )
        return SimpleNamespace(success=True, message_sid="SM1")

    monkeypatch.setattr(os_mod, "send_owner_template", _fake)
    out = wk.send_wakeup(_TID, owner_phone="+919811112222", owner_name="Asha", pending_count=4)
    assert out.success is True
    assert captured["template_name"] == "team_wakeup2"
    assert captured["language"] == "en"
    assert captured["params"] == {"owner_name": "Asha", "pending_count": "4"}
    assert captured["recipient_phone"] == "+919811112222"


def test_send_wakeup_floors_pending_count_to_one(monkeypatch) -> None:
    monkeypatch.setattr(wk, "wakeup_language", lambda _t: "en")
    captured: dict[str, Any] = {}
    import orchestrator.owner_surface.owner_send as os_mod

    monkeypatch.setattr(
        os_mod, "send_owner_template",
        lambda *a, **k: captured.update(params=a[3]) or SimpleNamespace(success=True),
    )
    wk.send_wakeup(_TID, owner_phone="+919811112222", owner_name="", pending_count=0)
    assert captured["params"] == {"owner_name": "", "pending_count": "1"}  # floored to 1


def test_send_wakeup_returns_none_when_variant_unconfigured(monkeypatch) -> None:
    """A language the template lacks (validate_params raises) → None, and no send is attempted."""
    monkeypatch.setattr(wk, "wakeup_language", lambda _t: "fr")  # team_wakeup2 has no 'fr' variant
    import orchestrator.owner_surface.owner_send as os_mod

    monkeypatch.setattr(
        os_mod, "send_owner_template",
        lambda *a, **k: pytest.fail("must not send when the template is unconfigured"),
    )
    assert wk.send_wakeup(_TID, owner_phone="+919811112222", pending_count=2) is None


# --- _is_due: the pure ≤1/day predicate ---


def test_is_due_predicate() -> None:
    mi = timedelta(hours=20)
    assert wk._is_due(None, _NOW, mi) is True                          # never woken
    assert wk._is_due(_NOW - timedelta(hours=19), _NOW, mi) is False   # woken 19h ago (<20)
    assert wk._is_due(_NOW - timedelta(hours=20), _NOW, mi) is True    # exactly 20h ago (boundary)
    assert wk._is_due(_NOW - timedelta(hours=25), _NOW, mi) is True    # long ago


# --- wakeup_due: DB-backed gate, fail-CLOSED on a read error ---


def _patch_tenant_conn(monkeypatch, *, last=None, boom=False):
    import importlib

    # orchestrator.db.__init__ re-exports the ``tenant_connection`` FUNCTION under the package
    # attribute of the same name, shadowing the submodule — so ``import orchestrator.db.tenant_connection
    # as tc`` would bind the function, not the module. Fetch the real submodule (what wakeup's lazy
    # ``from orchestrator.db.tenant_connection import tenant_connection`` resolves against) to patch it.
    tc = importlib.import_module("orchestrator.db.tenant_connection")

    if boom:
        def _boom(_t):
            raise RuntimeError("db down")

        monkeypatch.setattr(tc, "tenant_connection", _boom)
        return

    class _Conn:
        def execute(self, sql, params):
            return SimpleNamespace(fetchone=lambda: {"last_wakeup_at": last})

    class _CM:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *e):
            return False

    monkeypatch.setattr(tc, "tenant_connection", lambda _t: _CM())


def test_wakeup_due_reads_last_wakeup_at(monkeypatch) -> None:
    _patch_tenant_conn(monkeypatch, last=None)
    assert wk.wakeup_due(_TID, now=_NOW) is True  # never woken
    _patch_tenant_conn(monkeypatch, last=_NOW - timedelta(hours=1))
    assert wk.wakeup_due(_TID, now=_NOW) is False  # woken an hour ago → not due (<20h)
    _patch_tenant_conn(monkeypatch, last=_NOW - timedelta(hours=21))
    assert wk.wakeup_due(_TID, now=_NOW) is True  # woken >20h ago → due again


def test_wakeup_due_fail_closed_on_read_error(monkeypatch) -> None:
    _patch_tenant_conn(monkeypatch, boom=True)
    assert wk.wakeup_due(_TID, now=_NOW) is False  # unreadable → not due (never re-wake)
