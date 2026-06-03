"""VT-268 — accounts-book (owner Google Sheet) read-only lock.

Cowork ruling 2026-06-03: the "accounts book" the owner forbids the agent from updating IS the
owner's Google Sheet (primary_customer_ledger). This test LOCKS that the Sheets connector can
never write it: the OAuth scope is `spreadsheets.readonly` (no write scope) and the connector
exposes NO Sheet-write method (append / update / batchUpdate / values.append). A future PR that
adds a write scope or a write method fails here.

(The drive-push channel methods write only the internal `tenant_drive_channels` DB table, not the
Sheet — they are not Sheet writes.)
"""

from __future__ import annotations

import inspect

import pytest

# The CI `test` job is a DEP-LESS smoke (uv --no-project, stdlib + pytest only). The connector
# imports pydantic/httpx, so guard the heavy import or collection ModuleNotFound-errors there
# (the full orchestrator job + pre-push run it WITH deps). VT-268 follow-up fix.
pytest.importorskip("orchestrator.integrations.connectors.google_sheet")

from orchestrator.integrations.connectors import google_sheet  # noqa: E402
from orchestrator.integrations.connectors.google_sheet import GoogleSheetConnector  # noqa: E402


def test_oauth_scope_is_readonly_only():
    # Sheets scope is explicitly the readonly variant; no read-write spreadsheets scope present.
    assert google_sheet._SCOPE_SHEETS == "https://www.googleapis.com/auth/spreadsheets.readonly"
    assert "spreadsheets.readonly" in google_sheet._SCOPE
    # The full scope string must NOT contain any read-WRITE Sheets/Drive scope.
    forbidden_scopes = (
        "auth/spreadsheets ",  # bare read-write spreadsheets (note trailing space / end)
        "auth/drive.file",
        "auth/drive ",
    )
    scope_padded = google_sheet._SCOPE + " "
    for bad in forbidden_scopes:
        assert bad not in scope_padded, bad
    # And the only spreadsheets scope token is the readonly one.
    sheets_tokens = [t for t in google_sheet._SCOPE.split() if "spreadsheets" in t]
    assert sheets_tokens == ["https://www.googleapis.com/auth/spreadsheets.readonly"]


def test_connector_exposes_no_sheet_write_method():
    method_names = {
        name for name, _ in inspect.getmembers(GoogleSheetConnector, inspect.isfunction)
    }
    # No method whose name implies writing the spreadsheet.
    write_signals = ("append", "update_values", "batch_update", "write", "push_rows", "set_values")
    offending = {
        m for m in method_names if any(sig in m.lower() for sig in write_signals)
    }
    # register/unregister/renew_drive_push_channel write the internal DB table, not the Sheet —
    # they are allowed; assert they're the ONLY *_update/*write-ish names and nothing targets the Sheet.
    allowed_db_only = {"register_drive_push_channel", "unregister_drive_push_channel", "renew_drive_push_channel"}
    assert offending <= allowed_db_only, f"unexpected Sheet-write-shaped method(s): {offending - allowed_db_only}"


def test_connector_source_has_no_sheets_write_api_call():
    """Belt-and-braces: the connector source makes no Sheets write API call."""
    src = inspect.getsource(google_sheet)
    for bad in ("values:append", "values:batchUpdate", ":batchUpdate", "values/append"):
        assert bad not in src, bad
