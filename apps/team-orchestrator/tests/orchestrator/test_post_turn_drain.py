"""VT-683 P2b — runner._post_turn_drain_step: the post-turn owner-comms drain hook.

Dep-less unit: the step must (a) delegate to drain_one with the resolved owner locale,
(b) NEVER raise into the live inbound path, whatever breaks underneath.
"""

from __future__ import annotations

import pytest

pytest.importorskip("dbos")

from orchestrator import runner  # noqa: E402

_TID = "22222222-2222-2222-2222-222222222222"


def test_post_turn_drain_delegates_with_locale(monkeypatch) -> None:
    import orchestrator.owner_surface.freeform_acks as fa
    import orchestrator.owner_surface.owner_comms_drainer as dr

    monkeypatch.setattr(fa, "resolve_owner_locale", lambda _t: "hi")
    seen: dict = {}

    def _drain(tenant_id, recipient, *, lang="en"):
        seen.update({"tenant": tenant_id, "recipient": recipient, "lang": lang})
        return {"delivered": True}

    monkeypatch.setattr(dr, "drain_one", _drain)
    assert runner._post_turn_drain_step(_TID, "+919811112222") is True
    assert seen == {"tenant": _TID, "recipient": "+919811112222", "lang": "hi"}


def test_post_turn_drain_never_raises(monkeypatch) -> None:
    import orchestrator.owner_surface.freeform_acks as fa

    def _boom(_t):
        raise RuntimeError("locale read down")

    monkeypatch.setattr(fa, "resolve_owner_locale", _boom)
    assert runner._post_turn_drain_step(_TID, "+919811112222") is False  # swallowed
