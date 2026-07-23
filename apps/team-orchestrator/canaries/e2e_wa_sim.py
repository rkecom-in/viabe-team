"""VT-702 — full-arc WhatsApp onboarding SIMULATION against deployed dev.

Fazal (2026-07-23): "run an e2e simulation… a process that can work as mock whatsapp and
respond to the Manager's questions. That way you will get an understanding of what the
conversation would look like."

Drives the COMPLETE new-owner arc — signup front door (unknown number → consent) → onboarding
(discovery, GST card, web-presence, residuals) → activation (ACTIVATE TEAM) → agent chooser →
connection ask — with a persona LLM playing the owner. Button taps are simulated by sending
the button TITLE as the body (the echo semantics of real quick-reply taps). One scripted
"What does that mean?" tests the VT-701 never-deflect path.

Deterministic reply OVERRIDES fire before the persona (the arc's fixed decision points);
everything else is the persona answering naturally. The transcript prints turn by turn.

Reuses convo_harness primitives (ingress POST + retry, DSN, bogus-number range). Dev-only:
the dev send-guard mocks every outbound to the bogus number; sends never leave the building.

Usage (dev creds + ingress secret injected by railway; persona key layered from local file):
  railway run --service vt-orchestrator-service --environment dev -- \
    uv run python canaries/e2e_wa_sim.py [--max-turns 18] [--ingress-url URL]
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
import convo_harness as ch  # noqa: E402

_PERSONA_MODEL = "claude-sonnet-5"

_PERSONA_SYSTEM = (
    "You are Lubna Khan, owner of 'RKeCom Services Pvt Ltd', a Mumbai business doing "
    "AI-powered business intelligence (website rkecom.in). You are chatting on WhatsApp with "
    "your new AI business assistant during signup. Reply as a REAL busy Indian business owner: "
    "short, natural, sometimes Hinglish, never robotic. Answer the assistant's LAST question "
    "directly. If it offered tappable options and one fits, reply with EXACTLY that option's "
    "text (that is what tapping does). Never invent a different business. Output ONLY the "
    "message text — no quotes, no commentary."
)

# The arc's fixed decision points — deterministic, checked against the assistant's latest text
# (first match wins). The confusion probe fires once, on the first suggestions-carrying
# residual question, BEFORE the persona answers it.
_OVERRIDES: list[tuple[str, str]] = [
    ("is this your business", "Yes"),
    ("tap *activate team*", "ACTIVATE TEAM"),
    ("activate team", "ACTIVATE TEAM"),
    ("which one shall we start with", "Sales Recovery"),
]
_END_MARKERS = ["ready?", "connect your data", "set that up"]


def _load_local_anthropic_key() -> None:
    """Layer the LOCAL persona key when railway's env doesn't carry a usable one. Parsed
    in-process, never echoed (CL-431)."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return
    p = Path(__file__).resolve().parents[3] / ".viabe" / "secrets" / "anthropic.env"
    try:
        for line in p.read_text().splitlines():
            bare = line.strip().removeprefix("export ").strip()
            if bare.startswith("ANTHROPIC_API_KEY="):
                val = bare.split("=", 1)[1].strip().strip('"').strip("'")
                if val:  # a blank-value line must not shadow a later real one
                    os.environ["ANTHROPIC_API_KEY"] = val
    except Exception:  # noqa: BLE001 — the persona call will fail loudly instead
        pass


def _persona_reply(transcript: list[dict[str, str]]) -> str:
    from anthropic import Anthropic

    convo = "\n".join(
        f"{'ASSISTANT' if t['role'] == 'assistant' else 'YOU'}: {t['text']}" for t in transcript[-12:]
    )
    resp = Anthropic().messages.create(
        model=_PERSONA_MODEL,
        max_tokens=200,
        system=_PERSONA_SYSTEM,
        messages=[{
            "role": "user",
            "content": f"Conversation so far:\n{convo}\n\nYour next WhatsApp message:",
        }],
        timeout=45.0,
    )
    return (resp.content[0].text or "").strip().strip('"')


def _fetch_new_assistant_turns(dsn: str, tenant_id: str, after_ts: str) -> list[dict[str, Any]]:
    """conversation_log.id is a UUID — the incremental cursor is created_at (text-cast for a
    lossless round-trip through the harness)."""
    with ch._connect(dsn) as conn:
        rows = conn.execute(
            "SELECT created_at::text, text FROM conversation_log "
            "WHERE tenant_id = %s AND role = 'assistant' AND created_at > %s::timestamptz "
            "ORDER BY created_at",
            (tenant_id, after_ts),
        ).fetchall()
    return [{"ts": r[0], "text": r[1]} for r in rows]


def _await_assistant(dsn: str, tenant_id: str, after_ts: str, *, timeout_s: float) -> list[dict[str, Any]]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        turns = _fetch_new_assistant_turns(dsn, tenant_id, after_ts)
        if turns:
            time.sleep(4)  # settle: multi-message beats land together
            return _fetch_new_assistant_turns(dsn, tenant_id, after_ts)
        time.sleep(3)
    return []


def _find_tenant(dsn: str, phone: str) -> str | None:
    with ch._connect(dsn) as conn:
        row = conn.execute(
            "SELECT id::text FROM tenants WHERE whatsapp_number = %s "
            "ORDER BY created_at DESC LIMIT 1",
            (phone,),
        ).fetchone()
    return row[0] if row else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ingress-url", default=None)
    ap.add_argument("--max-turns", type=int, default=18)
    ap.add_argument("--reply-timeout", type=float, default=90.0)
    args = ap.parse_args()

    _load_local_anthropic_key()
    base = ch._ingress_base(args.ingress_url)
    secret = ch._dev_secret()
    dsn = ch._dsn()
    phone = f"{ch._BOGUS_PREFIX}1{random.randint(10_000, 99_999)}"
    print(f"=== e2e simulation | owner {phone} | ingress {base} ===")

    def send(body: str, note: str = "") -> None:
        sid = f"{ch._INBOUND_SID_PREFIX}sim{random.randint(10**9, 10**10 - 1)}"
        fields = {
            "From": f"whatsapp:{phone}", "To": "whatsapp:+910000000000",
            "Body": body, "MessageSid": sid,
        }
        r = ch._post_inbound(base, secret, fields)
        print(f"\n>>> OWNER{f' [{note}]' if note else ''}: {body}")
        print(f"    (ingress: {r.get('reason')})")

    transcript: list[dict[str, str]] = []
    confusion_fired = False
    last_ts = "1970-01-01T00:00:00+00:00"

    # --- Phase 0: the signup front door (pre-tenant — replies aren't tenant-logged) ----------
    send("Hi", "signup")
    transcript.append({"role": "owner", "text": "Hi"})
    time.sleep(12)  # consent card send (pre-tenant; not observable via conversation_log)
    print("<<< (consent card assumed sent — pre-tenant, not in conversation_log)")
    send("I agree", "consent tap")
    transcript.append({"role": "owner", "text": "I agree"})

    tenant_id = None
    for _ in range(30):
        tenant_id = _find_tenant(dsn, phone)
        if tenant_id:
            break
        time.sleep(3)
    if not tenant_id:
        print("FAIL: no tenant created after consent — signup front door broken")
        return 1
    print(f"=== tenant created: {tenant_id} ===")

    # --- Conversation loop --------------------------------------------------------------------
    for turn in range(args.max_turns):
        turns = _await_assistant(dsn, tenant_id, last_ts, timeout_s=args.reply_timeout)
        if not turns:
            print(f"FAIL: no assistant reply within {args.reply_timeout}s (turn {turn}) — SILENCE")
            return 1
        for t in turns:
            last_ts = max(last_ts, t["ts"])
            transcript.append({"role": "assistant", "text": t["text"]})
            print(f"<<< ASSISTANT: {t['text']}")

        latest = " ".join(t["text"] for t in turns).lower()

        if any(m in latest for m in _END_MARKERS) and "activate" not in latest:
            print("\n=== END: reached the data-connection ask — arc complete ===")
            _summary(transcript, tenant_id, dsn)
            return 0

        reply = None
        for marker, scripted in _OVERRIDES:
            if marker in latest:
                reply = scripted
                break
        if reply is None and not confusion_fired and "?" in latest and turn >= 2:
            reply = "What does that mean?"
            confusion_fired = True
            note = "confusion probe"
        else:
            note = "override" if reply else "persona"
        if reply is None:
            try:
                reply = _persona_reply(transcript)
            except Exception as exc:  # noqa: BLE001
                print(f"persona LLM failed ({type(exc).__name__}) — generic fallback")
                reply = "ok"
        send(reply, note)
        transcript.append({"role": "owner", "text": reply})

    print("\n=== MAX TURNS reached without completing the arc ===")
    _summary(transcript, tenant_id, dsn)
    return 1


def _summary(transcript: list[dict[str, str]], tenant_id: str, dsn: str) -> None:
    joined = " ".join(t["text"].lower() for t in transcript if t["role"] == "assistant")
    checks = {
        "gst card presented": "is this your business" in joined,
        "web-presence asked": "website or an online page" in joined,
        "manager intro (activation)": "i'm your manager" in joined,
        "agent chooser": "which one shall we start with" in joined,
        "confusion explained (not deflected)": (
            "what does that mean" not in joined and "let's finish setting up first" not in joined
        ),
    }
    with ch._connect(dsn) as conn:
        row = conn.execute(
            "SELECT verification_status, owner_inputs FROM tenants WHERE id = %s", (tenant_id,)
        ).fetchone()
    print("\n=== CHECKS ===")
    for k, v in checks.items():
        print(f"  {'✓' if v else '✗'} {k}")
    if row:
        print(f"  verification_status={row[0]} owner_inputs={row[1]}")
    print(f"  owner turns: {sum(1 for t in transcript if t['role'] == 'owner')}")
    print(f"\n(cleanup: harness teardown for tenant {tenant_id})")


if __name__ == "__main__":
    raise SystemExit(main())
