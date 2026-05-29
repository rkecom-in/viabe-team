#!/usr/bin/env python3
"""VT-237 — env-password operator login canary.

Source-substrate check (mock-friendly per addendum). Real runtime
coverage in apps/team-web/tests/api/ops-login-password.test.ts.

5 assertions:
- A1: POST handler branches on password presence + calls
      timingSafeEqual for constant-time compare
- A2: invalid_credentials redirect path wired (email OR password fail)
- A3: password_login_not_configured redirect when env unset
- A4: magic-link path (signInWithOtp) preserved alongside
- A5: shared lib/auth/issue-operator-session.ts helper sets
      path=/team + maxAge=604800

Wall-clock ≤ 5s.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
ROUTE = REPO / "apps/team-web/app/api/ops/login/route.ts"
HELPER = REPO / "apps/team-web/lib/auth/issue-operator-session.ts"
LOGIN_PAGE = REPO / "apps/team-web/app/(auth)/team/ops/login/page.tsx"

RESULTS: dict[int, dict[str, Any]] = {}


def assertion(num: int, name: str, passed: bool, *, observed: Any = None,
               expected: Any = None) -> None:
    status = "PASS" if passed else "FAIL"
    RESULTS[num] = {"name": name, "status": status, "observed": observed,
                    "expected": expected}
    print(f"[{num}] {status} — {name}")
    print(f"    observed: {observed}")
    if not passed and expected is not None:
        print(f"    expected: {expected}", file=sys.stderr)


def run_canary() -> int:
    for p in (ROUTE, HELPER, LOGIN_PAGE):
        if not p.exists():
            print(f"PREFLIGHT FAIL — missing: {p}", file=sys.stderr)
            return 2
    print("PREFLIGHT OK")

    route_src = ROUTE.read_text(encoding="utf-8")
    helper_src = HELPER.read_text(encoding="utf-8")
    page_src = LOGIN_PAGE.read_text(encoding="utf-8")

    # --- A1: password branch + timingSafeEqual ---
    has_branch = (
        "body.password" in route_src
        and "handleEnvPasswordSignIn" in route_src
        and "timingSafeEqual" in route_src
        and "constantTimeEqual" in route_src
    )
    assertion(
        1,
        "POST branches on password + uses timingSafeEqual",
        has_branch,
        observed={
            "password_branch": "body.password" in route_src,
            "handler_named": "handleEnvPasswordSignIn" in route_src,
            "timing_safe": "timingSafeEqual" in route_src,
        },
    )

    # --- A2: invalid_credentials redirect ---
    has_invalid_redirect = "error=invalid_credentials" in route_src
    assertion(
        2,
        "invalid_credentials redirect wired",
        has_invalid_redirect,
        observed={"present": has_invalid_redirect},
    )

    # --- A3: not_configured guard ---
    has_not_configured = "error=password_login_not_configured" in route_src
    assertion(
        3,
        "password_login_not_configured guard wired",
        has_not_configured,
        observed={"present": has_not_configured},
    )

    # --- A4: magic-link path preserved ---
    has_magic_link = (
        "signInWithOtp" in route_src
        and "handleMagicLink" in route_src
    )
    assertion(
        4,
        "Magic-link path (signInWithOtp) preserved alongside",
        has_magic_link,
        observed={
            "signInWithOtp": "signInWithOtp" in route_src,
            "handleMagicLink": "handleMagicLink" in route_src,
        },
    )

    # --- A5: helper sets path=/team + 7d Max-Age ---
    has_helper_substrate = (
        "viabe_ops_jwt" in helper_src
        and "60 * 60 * 24 * 7" in helper_src
        and "'/team'" in helper_src
        and "httpOnly: true" in helper_src
        and "secure: true" in helper_src
        and "sameSite: 'lax'" in helper_src
    )
    has_password_field = (
        'name="password"' in page_src
        and 'type="password"' in page_src
    )
    pass_5 = has_helper_substrate and has_password_field
    assertion(
        5,
        "Helper sets path=/team + 7d Max-Age + password field present",
        pass_5,
        observed={
            "helper_substrate": has_helper_substrate,
            "password_field": has_password_field,
        },
    )

    failures = [r for r in RESULTS.values() if r["status"] != "PASS"]
    if failures:
        print(f"\nFAIL: {len(failures)}/{len(RESULTS)} assertion(s)",
              file=sys.stderr)
        return 1
    print(f"\nPASS: {len(RESULTS)}/{len(RESULTS)} assertions")
    return 0


if __name__ == "__main__":
    sys.exit(run_canary())
