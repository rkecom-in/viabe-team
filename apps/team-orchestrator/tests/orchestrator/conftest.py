"""Shared fixtures for the orchestrator test package (VT-3.3c).

Every test that invokes a direct handler now exercises the Twilio send path.
No test may make a live Twilio call, so the Twilio client is stubbed for the
whole package via the autouse fixture below.
"""

from __future__ import annotations

import os

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "integration: real-LLM / external-service test; runs only when "
        "the RUN_INTEGRATION_TESTS=1 environment variable is set.",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip @pytest.mark.integration tests unless RUN_INTEGRATION_TESTS=1."""
    if os.environ.get("RUN_INTEGRATION_TESTS") == "1":
        return
    skip = pytest.mark.skip(
        reason="integration test — set RUN_INTEGRATION_TESTS=1 to run"
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def twilio_create(monkeypatch):
    """Stub Twilio's ``messages.create`` and return the Mock so a test can set
    ``.return_value`` / ``.side_effect``. Default: one successful send.
    """
    from unittest.mock import MagicMock

    from orchestrator.utils import twilio_send

    create = MagicMock(return_value=MagicMock(sid="SM" + "0" * 32))
    fake_client = MagicMock()
    fake_client.messages.create = create
    monkeypatch.setattr(twilio_send, "_client", lambda: fake_client)
    monkeypatch.setenv("TEAM_TWILIO_FROM_NUMBER", "+910000000000")
    monkeypatch.setenv("TEAM_TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("TEAM_TWILIO_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("TEAM_PHONE_HASH_SALT", "vt-3-3c-test-salt")
    return create


@pytest.fixture(autouse=True)
def _autostub_twilio(request):
    """Autouse: no orchestrator test makes a live Twilio call.

    A test that needs to control the send declares ``twilio_create`` directly —
    it receives the same Mock (pytest caches fixtures per test). Skipped in the
    lightweight ``test`` CI job, where twilio / dbos are not installed.
    """
    try:
        import orchestrator.utils.twilio_send  # noqa: F401
    except Exception:
        return
    request.getfixturevalue("twilio_create")
