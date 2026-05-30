"""VT-253 — proof test for the URL-auth-token CI guard (scripts/check_no_url_tokens.sh).

Runs the guard in a throwaway git repo: a planted real-shaped token value trips
it (exit 1); the elided/narrative forms the VT-252 session-export .md actually
contains pass (exit 0). No external API — this is the Rule #15 analog.

The planted token values are BUILT AT RUNTIME (not written as source literals)
so this test file itself carries no token-shaped / high-entropy string — keeping
both gitleaks and the guard's own tracked-file scan clean on it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_GUARD = _REPO / "scripts" / "check_no_url_tokens.sh"

_EQ = "=" + ""  # split so no literal "token=<chars>" appears in this source file
_LONG = "a1b2c3d4" * 7  # 56 chars, matches the guard's {20,}, low-entropy (gitleaks-safe)


def _git_repo(tmp_path: Path, filename: str, content: str) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / filename).write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", filename], cwd=tmp_path, check=True)
    return tmp_path


def _run_guard(repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(_GUARD)], cwd=repo, capture_output=True, text=True
    )


@pytest.mark.skipif(not _GUARD.exists(), reason="guard script not present")
def test_guard_trips_on_real_magiclink_token(tmp_path):
    leak = f"see https://x.supabase.co/auth/v1/verify?token{_EQ}{_LONG}&type=magiclink"
    repo = _git_repo(tmp_path, "export.md", leak)
    res = _run_guard(repo)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "export.md" in (res.stdout + res.stderr)


@pytest.mark.skipif(not _GUARD.exists(), reason="guard script not present")
def test_guard_passes_on_elided_and_error_code(tmp_path):
    # Exactly the forms the committed session-export .md contains — must NOT trip:
    # an elided fragment token (3 dots) and the literal error code.
    narrative = (
        "the redirect landed at /team/ops/login?error" + _EQ + "missing_token"
        "#access_token" + _EQ + "...\n"
        "session was in URL fragment, browsers never send it.\n"
    )
    repo = _git_repo(tmp_path, "narrative.md", narrative)
    res = _run_guard(repo)
    assert res.returncode == 0, res.stdout + res.stderr


@pytest.mark.skipif(not _GUARD.exists(), reason="guard script not present")
def test_guard_trips_on_access_token_value(tmp_path):
    leak = f"callback?access_token{_EQ}{_LONG}"
    repo = _git_repo(tmp_path, "cb.md", leak)
    res = _run_guard(repo)
    assert res.returncode == 1, res.stdout + res.stderr
