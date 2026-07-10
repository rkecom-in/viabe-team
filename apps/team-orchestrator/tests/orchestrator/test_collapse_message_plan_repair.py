"""VT-633 — _complete_message_plan repair unit checks (pure, mocked conn)."""
from uuid import uuid4
from unittest.mock import MagicMock

import pytest

# orchestrator.collapse imports psycopg at module level — absent in the dep-less CI smoke,
# which fails COLLECTION (not skip) on a bare import.
pytest.importorskip("psycopg")

from orchestrator.collapse import _complete_message_plan  # noqa: E402 — after the importorskip gate


def _plan(params, template="team_winback_offer"):
    return {"message_plan": {"template_id": template, "language": "en",
            "personalization": "Special offer waiting for you.",
            "template_params": params}}


def _conn(business="Sundaram Stores"):
    c = MagicMock()
    c.execute.return_value.fetchone.return_value = {"business_name": business}
    return c


def test_placeholder_and_missing_filled():
    p = _plan({"customer_name": "<customer_name>"})
    _complete_message_plan(_conn(), uuid4(), p)
    got = p["message_plan"]["template_params"]
    assert got == {"customer_name": "ji", "business_name": "Sundaram Stores",
                   "offer_description": "Special offer waiting for you."}


def test_real_values_kept_extras_dropped():
    p = _plan({"customer_name": "Asha", "business_name": "X", "offer_description": "Y",
               "bogus_extra": "Z"})
    _complete_message_plan(_conn(), uuid4(), p)
    got = p["message_plan"]["template_params"]
    assert got == {"customer_name": "Asha", "business_name": "X", "offer_description": "Y"}


def test_unknown_template_untouched():
    p = _plan({"a": "b"}, template="no_such_template")
    before = dict(p["message_plan"]["template_params"])
    _complete_message_plan(_conn(), uuid4(), p)
    assert p["message_plan"]["template_params"] == before


def test_registry_signature_satisfied_post_repair():
    from orchestrator.templates_registry import validate_params
    p = _plan({})
    _complete_message_plan(_conn(), uuid4(), p)
    validate_params("team_winback_offer", "en", p["message_plan"]["template_params"])  # no raise
