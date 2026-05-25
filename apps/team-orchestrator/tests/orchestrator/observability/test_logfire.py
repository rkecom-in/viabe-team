"""Tests for the Logfire observability module (VT-171, hot-fix CL-56).

Pure tests — Logfire SDK is patched / monkeypatched so no real network IO.
The canary covers the on-the-wire ingest proofs against the EU workspace.
"""

from __future__ import annotations

import os
import warnings
from typing import Any
from uuid import uuid4

import pytest

pytest.importorskip("logfire")

from orchestrator.observability import logfire as logfire_mod  # noqa: E402
from orchestrator.observability.pii import (  # noqa: E402
    redact_for_langsmith,
    redact_for_log,
    redact_for_otel_span,
)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setenv("TEAM_PHONE_HASH_SALT", "test-salt-vt171")
    # Always start from a clean configured state.
    logfire_mod._reset_for_tests()
    yield
    logfire_mod._reset_for_tests()


# ---------------------------------------------------------------------------
# 1. configure_logfire idempotent + no-token disable
# ---------------------------------------------------------------------------

def test_configure_logfire_no_token_returns_false_and_warns(monkeypatch, capsys) -> None:
    monkeypatch.delenv("LOGFIRE_TOKEN", raising=False)
    out = logfire_mod.configure_logfire()
    assert out is False
    err = capsys.readouterr().err
    assert "Logfire disabled" in err


def test_configure_logfire_idempotent_with_token(monkeypatch) -> None:
    """First call configures; second is a no-op short-circuit."""
    monkeypatch.setenv("LOGFIRE_TOKEN", "pylf_test_token_DUMMY")
    monkeypatch.setenv("LOGFIRE_BASE_URL", "https://example.invalid")
    call_count = {"n": 0}

    import logfire as _lf

    def _fake_configure(**kwargs: Any) -> None:
        call_count["n"] += 1

    monkeypatch.setattr(_lf, "configure", _fake_configure)
    monkeypatch.setattr(_lf, "instrument_anthropic", lambda *a, **k: None)
    monkeypatch.setattr(_lf, "instrument_pydantic", lambda *a, **k: None)

    a = logfire_mod.configure_logfire()
    b = logfire_mod.configure_logfire()
    assert a is True
    assert b is True
    assert call_count["n"] == 1, "second configure should short-circuit"


def test_configure_logfire_passes_advanced_base_url(monkeypatch) -> None:
    """Q3 contract: AdvancedOptions(base_url=...) reaches logfire.configure
    so the SDK routes spans through the EU region. Env-var-driven OTLP
    setup was the original Q3 plan but caused exporter conflicts; the
    SDK's internal exporter via AdvancedOptions is the load-bearing path.
    DBOS picks up the global TracerProvider that configure() registers."""
    monkeypatch.setenv("LOGFIRE_TOKEN", "pylf_test_token_DUMMY")
    monkeypatch.setenv("LOGFIRE_BASE_URL", "https://logfire-eu.pydantic.dev")
    captured: dict[str, Any] = {}

    import logfire as _lf

    def _fake_configure(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(_lf, "configure", _fake_configure)
    monkeypatch.setattr(_lf, "instrument_anthropic", lambda *a, **k: None)
    monkeypatch.setattr(_lf, "instrument_pydantic", lambda *a, **k: None)

    logfire_mod.configure_logfire()
    assert captured.get("token") == "pylf_test_token_DUMMY"
    advanced = captured.get("advanced")
    assert advanced is not None
    assert getattr(advanced, "base_url", None) == "https://logfire-eu.pydantic.dev"


# ---------------------------------------------------------------------------
# 2. traced_node decorator redacts inputs BEFORE span capture
# ---------------------------------------------------------------------------

def test_traced_node_disabled_no_token_is_passthrough(monkeypatch) -> None:
    monkeypatch.delenv("LOGFIRE_TOKEN", raising=False)
    called = {"n": 0}

    @logfire_mod.traced_node("test_span")
    def _fn(x: int) -> int:
        called["n"] += 1
        return x + 1

    assert _fn(2) == 3
    assert called["n"] == 1


def test_traced_node_captures_redacted_input(monkeypatch) -> None:
    """Inputs flow through redact_for_otel_span BEFORE the Logfire span captures them."""
    monkeypatch.setenv("LOGFIRE_TOKEN", "pylf_test_token_DUMMY")
    captured_attrs: dict[str, Any] = {}

    class _FakeSpan:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def set_attribute(self, key: str, value: Any) -> None:
            captured_attrs[f"output:{key}"] = value

    import logfire as _lf

    def _fake_span(name, **attrs):
        captured_attrs.update(attrs)
        return _FakeSpan()

    monkeypatch.setattr(_lf, "span", _fake_span)

    @logfire_mod.traced_node("my_node")
    def _fn(payload: dict[str, str]) -> dict[str, str]:
        return {"echo": "ok"}

    _fn({"phone": "+919876543210", "customer_name": "Rajesh Kumar"})

    # The captured args contain the REDACTED dict, not the raw input.
    args_attr = captured_attrs.get("args", [])
    assert args_attr, "args attribute should have been captured"
    redacted_dict = args_attr[0]
    assert redacted_dict["phone"].startswith("phone_tok_")
    assert redacted_dict["customer_name"].startswith("<redacted:customer_name:")


# ---------------------------------------------------------------------------
# 3. Deprecated `redact_for_langsmith` alias still works + emits warning
# ---------------------------------------------------------------------------

def test_redact_for_langsmith_alias_emits_deprecation_warning() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out = redact_for_langsmith({"phone": "+919876543210"})
        assert any(
            issubclass(w.category, DeprecationWarning) for w in caught
        ), "expected DeprecationWarning"
    assert out["phone"].startswith("phone_tok_")


def test_redact_for_otel_span_canonical_no_warning() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out = redact_for_otel_span({"phone": "+919876543210"})
        assert not any(
            issubclass(w.category, DeprecationWarning) for w in caught
        ), "canonical name should NOT emit DeprecationWarning"
    assert out["phone"].startswith("phone_tok_")


def test_redact_for_log_alias_unchanged() -> None:
    out = redact_for_log({"body": "Hi I want to cancel"})
    assert out["body"].startswith("body_tok_")


# ---------------------------------------------------------------------------
# 4. byte-identical contract — VT-104 token format preserved
# ---------------------------------------------------------------------------

def test_otel_span_matches_langsmith_alias_byte_identical() -> None:
    """Cond-2 regression: rename did not drift the token format."""
    payload = {
        "k": "Customer +919876543210 cancellation",
        "customer_name": "Rajesh Kumar",
        "body": "Hi I want to cancel",
    }
    canonical = redact_for_otel_span(payload)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        deprecated = redact_for_langsmith(payload)
    assert canonical == deprecated


# ---------------------------------------------------------------------------
# 5. is_enabled tracks LOGFIRE_TOKEN
# ---------------------------------------------------------------------------

def test_is_enabled_tracks_token(monkeypatch) -> None:
    monkeypatch.delenv("LOGFIRE_TOKEN", raising=False)
    assert logfire_mod.is_enabled() is False
    monkeypatch.setenv("LOGFIRE_TOKEN", "x")
    assert logfire_mod.is_enabled() is True


# ---------------------------------------------------------------------------
# 6. format_run_id_footer unchanged behaviour
# ---------------------------------------------------------------------------

def test_format_run_id_footer() -> None:
    run_id = uuid4()
    assert logfire_mod.format_run_id_footer(run_id) == f"run_id={run_id}"
