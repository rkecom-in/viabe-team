"""VT-403 — env_presence helper emits booleans ONLY, never a value.

Subprocess-invokes ``scripts/env_presence.py`` with THROWAWAY env vars (never a real secret, per the
ironic-failure guard) and asserts no value substring ever reaches stdout/stderr. Also runs the
``no-raw-railway-variables`` gate over the tree.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[4]
_HELPER = "scripts/env_presence.py"
_GUARD = "scripts/check_no_raw_railway_variables.py"

# A throwaway value that is NOT a real secret; the whole point is to prove it never echoes back.
_SENTINEL = "throwaway-sentinel-value-DO-NOT-ECHO"


def _run(args: list[str], env_extra: dict[str, str]) -> subprocess.CompletedProcess[str]:
    import os

    env = {**os.environ, **env_extra}
    return subprocess.run(
        [sys.executable, _HELPER, *args],
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_presence_reports_set_and_unset_without_value() -> None:
    r = _run(
        ["presence", "--source", "env", "TEST_PRESENT", "TEST_ABSENT"],
        {"TEST_PRESENT": _SENTINEL},
    )
    assert r.returncode == 0, r.stderr
    assert "TEST_PRESENT: set" in r.stdout
    assert "TEST_ABSENT: unset" in r.stdout
    # THE invariant: the value never appears anywhere the model would read.
    assert _SENTINEL not in r.stdout
    assert _SENTINEL not in r.stderr


def test_equal_match_mismatch_unset_without_value() -> None:
    match = _run(
        ["equal", "acct", "env:TEST_A", "env:TEST_B"],
        {"TEST_A": _SENTINEL, "TEST_B": _SENTINEL},
    )
    assert "acct: MATCH" in match.stdout
    assert _SENTINEL not in match.stdout and _SENTINEL not in match.stderr

    mismatch = _run(
        ["equal", "acct", "env:TEST_A", "env:TEST_B"],
        {"TEST_A": _SENTINEL, "TEST_B": "other-throwaway"},
    )
    assert "acct: MISMATCH" in mismatch.stdout
    assert _SENTINEL not in mismatch.stdout and _SENTINEL not in mismatch.stderr

    unset = _run(["equal", "acct", "env:TEST_A", "env:TEST_MISSING"], {"TEST_A": _SENTINEL})
    assert "acct: unset" in unset.stdout
    assert _SENTINEL not in unset.stdout and _SENTINEL not in unset.stderr


def test_no_raw_railway_variables_gate_passes() -> None:
    r = subprocess.run(
        [sys.executable, _GUARD], cwd=_REPO, capture_output=True, text=True, check=False
    )
    assert r.returncode == 0, r.stdout + r.stderr
