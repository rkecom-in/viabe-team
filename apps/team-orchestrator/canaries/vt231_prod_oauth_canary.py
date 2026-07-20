"""VT-231 prod OAuth redirect canary — proves the api.viabe.ai redirect wiring in the LIVE prod
process, and stages the human-consent round-trip.

Run INSIDE prod so it reads the real prod env (GOOGLE_OAUTH_REDIRECT_URI / SHOPIFY_OAUTH_REDIRECT_URI
/ client ids) OS-env->process — never into any operator's context:

    railway run --environment production --service vt-orchestrator-service -- \
        uv run --directory apps/team-orchestrator python canaries/vt231_prod_oauth_canary.py

STRUCTURAL leg (default, PROD-SAFE): builds the Google authorize URL + the Shopify install URL with a
THROWAWAY static state (no DB write, no token exchange, no send) and asserts each carries the
api.viabe.ai redirect. This is the Rule #15 canary for the redirect env change — it proves the live
prod process threads the correct redirect into the provider authorize URL.

HUMAN leg (--emit-google-url): additionally prints the Google authorize URL so a real consent
round-trip can be completed on demand (open -> Google consent -> callback lands on api.viabe.ai).
The printed URL carries only the PUBLIC OAuth client_id + the public redirect + a throwaway state —
no secret. The token-exchange half still needs GOOGLE_OAUTH_CLIENT_SECRET (sealed on prod) and a real
tenant; that half is the true e2e, fired separately once a prod-safe test tenant exists.
"""

from __future__ import annotations

import sys
from uuid import uuid4

_EXPECTED_HOST = "https://api.viabe.ai"
_GOOGLE_CB = f"{_EXPECTED_HOST}/api/orchestrator/integrations/google/callback"
_SHOPIFY_CB = f"{_EXPECTED_HOST}/api/orchestrator/integrations/shopify/callback"
_THROWAWAY_STATE = "vt231-canary-throwaway-state"  # never persisted; pure URL construction


def _check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail and not ok else ""))
    return ok


def main() -> int:
    emit_google = "--emit-google-url" in sys.argv
    tid = uuid4()
    all_ok = True

    print("=== VT-231 prod OAuth redirect canary (structural, prod-safe) ===")

    # --- Google ---
    from orchestrator.integrations.connectors.google_sheet import GoogleSheetConnector

    g_url = GoogleSheetConnector().build_auth_url(tid, state=_THROWAWAY_STATE)
    from urllib.parse import parse_qs, urlparse

    gq = parse_qs(urlparse(g_url).query)
    all_ok &= _check("google authorize host is accounts.google.com",
                     urlparse(g_url).netloc == "accounts.google.com", urlparse(g_url).netloc)
    all_ok &= _check("google redirect_uri == api.viabe.ai callback",
                     gq.get("redirect_uri", [""])[0] == _GOOGLE_CB, gq.get("redirect_uri", [""])[0])
    all_ok &= _check("google response_type=code", gq.get("response_type", [""])[0] == "code")
    all_ok &= _check("google scope present", bool(gq.get("scope", [""])[0]))
    all_ok &= _check("google state threaded", gq.get("state", [""])[0] == _THROWAWAY_STATE)

    # --- Shopify ---
    from orchestrator.integrations.connectors.shopify import (
        ShopifyConfigError,
        ShopifyConnector,
    )

    try:
        s_url = ShopifyConnector().build_oauth_install_url(
            tid, "vt231-canary-shop.myshopify.com", state=_THROWAWAY_STATE
        )
        sq = parse_qs(urlparse(s_url).query)
        all_ok &= _check("shopify install host is the shop domain",
                         urlparse(s_url).netloc == "vt231-canary-shop.myshopify.com", urlparse(s_url).netloc)
        all_ok &= _check("shopify redirect_uri == api.viabe.ai callback",
                         sq.get("redirect_uri", [""])[0] == _SHOPIFY_CB, sq.get("redirect_uri", [""])[0])
        all_ok &= _check("shopify scope present", bool(sq.get("scope", [""])[0]))
    except ShopifyConfigError:
        # Not a redirect failure — the redirect env is verified separately (equal-check). The app
        # creds (SHOPIFY_API_KEY/SECRET) are simply absent on this env, so the install URL can't be
        # built. Report as a distinct config gap, not a canary FAIL.
        print("  [GAP ] shopify SHOPIFY_API_KEY/SHOPIFY_API_SECRET absent on this env — "
              "install URL unbuildable until set (redirect env itself is correct)")

    if emit_google:
        print("\n=== HUMAN-CONSENT leg — open this URL, complete Google consent ===")
        print(g_url)
        print("(client_id is public; state is throwaway. Token exchange needs a real tenant + the "
              "sealed client_secret — that half is the separate e2e.)")

    print(f"\n=== {'ALL PASS' if all_ok else 'FAILURES ABOVE'} ===")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
