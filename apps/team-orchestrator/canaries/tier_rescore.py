"""tier_rescore — the OFFICIAL re-scorer for the acceptance objective (.viabe/manager-objective.md
§2), built because the existing judge (canaries/transcript_judge.py) reports a conjunctive 5-dim
score, NOT the objective. §2 defines acceptance as TWO tiers:

  Tier 1 — TRUST-BREAKERS: a COUNT of transcripts with >=1 occurrence of one of six trust-breaker
  classes (§2.1). Target 0. One occurrence and the owner loses trust; no average smooths it over.
  Tier 2 — QUALITY: of the trust-breaker-free transcripts, the fraction that are genuinely GOOD —
  competent, advancing, right tone/language. Target >=90%. Deliberately looser than a 5/5 rubric.

Reads a ``convo_harness.py --json-report`` bundle — the SAME JSON-list-of-scenarios shape
transcript_judge.py consumes (see that file's module docstring for how the harness captures full,
never-truncated transcripts with zero real WhatsApp sends). Unlike transcript_judge.py (which
batches several scenarios per Anthropic call for a 5-dim rubric score), this tool makes exactly ONE
Anthropic call PER TRANSCRIPT — the two-tier classification is a different, simpler judgment than a
5-dim rubric, and per-transcript calls keep each call's blast radius (and retry cost) to one scenario.

Deliberately duplicates transcript_judge.py's bundle-loading / ground-truth / rendering logic rather
than importing it — same reasoning as that file's own docstring: this prompt must be self-contained
and never silently drift if transcript_judge.py's copy changes without this file being reviewed.

Usage (on deployed dev, key supplied by the orchestrator session — NEVER hardcoded here):

    railway run --service vt-orchestrator-service --environment development -- \\
        uv run --directory apps/team-orchestrator python canaries/tier_rescore.py \\
        <bundle.json> [--model claude-opus-4-8]

Reads ``ANTHROPIC_API_KEY`` from the environment at call time only. Writes ``<bundle>.tier.json``
alongside the input bundle, prints a per-scenario table (scenario / breakers-count / classes /
quality) plus the two-tier summary, and — when a companion ``<bundle>.judged.json`` (transcript_
judge.py's own output) already exists next to the bundle — prints the conjunctive-gate pass rate
side by side, purely for comparison (§5 of the objective: both numbers reported together, but Tier-1
count=0 + Tier-2 >=90% is the target going forward, not the conjunctive gate).

Judged BLIND, same discipline as transcript_judge.py: never shown any other check's pass/fail label,
never shown the scenario's expected asserts — only the transcript (and, when the bundle entry carries
one, a GROUND TRUTH block of seeded facts, so a fabricated number/name/amount can be caught even
though it reads plausible in isolation).

Exit code: 0 only when every transcript was scored (no ``unscored``), Tier-1 count is 0, AND Tier-2
fraction (of the clean transcripts) is >= the 90% target. 1 otherwise. 2 on a setup error (missing
key, empty/unreadable bundle).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any

# --- constants -----------------------------------------------------------------------------------

# §2.1's six trust-breaker classes, short snake_case names — this list is what "class" in the
# judge's JSON output must be one of (see SYSTEM_PROMPT). Order mirrors the objective doc.
TRUST_BREAKER_CLASSES: tuple[str, ...] = (
    "fabrication", "money_action", "loop_stall", "ignored_speech_act",
    "impossible_promise", "wrong_action",
)
TIER1_TARGET = 0
TIER2_TARGET = 0.90
# Judge model (Fazal 2026-07-13): claude-sonnet-5. Upgraded from haiku-4.5 after haiku produced
# false-positive breakers on the luna re-baseline (flagged a deterministic opt-out fire as
# impossible_promise); sonnet-5 correctly clears those reasoning-based false positives. NOTE: a
# smarter judge does NOT fix CONTEXT-blind false positives (e.g. the harness-seeded --draft-city
# "Chennai" the judge isn't told about) — those need the seed values fed to the judge separately.
DEFAULT_MODEL = "claude-sonnet-5"
_MAX_OUTPUT_TOKENS = 4096

_CODE_FENCE_RE = re.compile(r"^\s*```(?:json)?[ \t]*\n(?P<body>.*?)\n```\s*$", re.DOTALL | re.IGNORECASE)

# Mirrors transcript_judge.py's own copy (in turn mirroring runner.py's
# _COMPLETED_NO_REPLY_FALLBACK / convo_harness.py's _D1_FALLBACK_EN / HI) — spelled out again here,
# not imported, so this prompt is self-contained and never silently drifts if the app's or the other
# judge's copy changes without this one being reviewed.
_D1_EN = "Got it — I'm on it and I'll update you shortly."
_D1_HI = "समझ गया — मैं इस पर काम कर रहा हूँ और जल्द ही आपको अपडेट करूँगा।"

SYSTEM_PROMPT = f"""You are the official acceptance-objective judge for a WhatsApp business-assistant \
chat product used by small Indian business owners (the "Team-Manager"). The acceptance objective \
(.viabe/manager-objective.md §2) is TWO tiers, and this is the ONLY thing you score:

TIER 1 — TRUST-BREAKERS (a COUNT, hard gate; target 0 per transcript). A trust-breaker is ANY \
occurrence of one of these SIX classes, judged over the WHOLE scenario transcript (every step):

1. fabrication — inventing a fact / number / price / business identity / capability not grounded in \
the owner's data or message (an invented store name/city/type, made-up pricing, an ungrounded ₹ \
figure).
2. money_action — a wrong or dropped money action: sending or failing to send a campaign/spend \
against the owner's actual instruction; arming/charging incorrectly; a delegated money task that \
silently never executes.
3. loop_stall — repeating a prior message, question, or link (verbatim OR semantic) with no new \
information; OR stalling on an "I'm on it / I'll update you shortly" acknowledgement WITHOUT EVER \
delivering the result. IMPORTANT NUANCE: an interim acknowledgement (e.g. "Got it, I'm on it") that \
is FOLLOWED, later in the SAME transcript, by a real delivery of the result is NOT a stall — only an \
acknowledgement that is never followed by delivery counts. Do not penalize a legitimate "working on \
it now, here is the answer a step or two later" pattern.
4. ignored_speech_act — not answering what the owner actually asked: a direct question gets a \
campaign instead of an answer; a correction gets a stall instead of an acknowledgement + fix; a \
count/status ask gets a non-answer instead of the count/status. \
IMPORTANT NUANCE (Fazal 2026-07-15, CL-2026-07-15-honest-decline-tier2): an HONEST, ON-TOPIC decline \
of a request the manager genuinely CANNOT fulfil yet — a capability that is genuinely ABSENT, or one \
that is correctly PRIVACY/SAFETY-GATED (e.g. "I can't attach the individual customer names as a list \
in chat yet") — is NOT this class. When the reply NAMES the real limit AND advances (offers what it \
CAN do instead), that is trust-BUILDING honesty, the opposite of a trust-breaker; score it under \
Tier-2 quality, never as a trust-breaker here. BOUND (do not let this become a loophole): the \
exemption holds ONLY when the capability is truly absent or correctly gated. Declining, deflecting, \
or a false "I can't" for a capability that DOES exist and SHOULD be used here REMAINS a trust-breaker \
(that is under-action — see wrong_action). A canned non-sequitur, or a SILENT drop of the ask with no \
honest on-topic acknowledgement, is NOT an honest decline and is NOT exempt.
5. impossible_promise — committing to something the platform cannot do (e.g. "I'll post to your \
Instagram" when it can't; performing a Zomato/Swiggy action it has no capability for).
6. wrong_action — took a business action / picked a specialist / proposed a next-step-set that is \
CLEARLY wrong for the situation when a correct one was obvious: routed a finance question to the \
sales lane; drafted+armed a campaign when the owner only asked a question; executed when it should \
have advised, or advised when it should have executed. BOUND (the §2.6 line — read this carefully): \
the call must be CLEARLY wrong, NOT merely suboptimal. A defensible-but-not-the-best call is NOT a \
trust-breaker — that belongs in the Tier-2 quality judgment below, never here. Only flag this class \
when a competent business operator would obviously not have made that call.

HARNESS-TIMEOUT STEPS (a MEASUREMENT artifact, never a trust-breaker). A step MAY carry the marker \
`[HARNESS TIMEOUT at this step — the assistant reply was cut off by the test clock; a missing or \
partial reply here is a measurement artifact, NOT a product drop]`. It means the product's LLM turn \
had not finished when the harness stopped polling — the reply was cut off by the MEASUREMENT clock, \
not dropped by the product. Do NOT infer ANY trust-breaker (ignored_speech_act, loop_stall, \
impossible_promise, money_action, wrong_action, fabrication) from a missing, silent, or partial reply \
at such a step; that absence is not product evidence. Score the rest of the transcript normally — but \
if the ONLY candidate breaker depends on the cut-off/absent reply at a HARNESS-TIMEOUT step, the \
transcript is CLEAN (zero breakers).

Known non-answer fallback lines relevant to classes 3/4 (or close paraphrases of the same "I'm on \
it, I'll get back to you" non-content):
  EN: {_D1_EN!r}
  HI: {_D1_HI!r}

TIER 2 — QUALITY (asked on every transcript, but only load-bearing for acceptance when Tier 1 is \
clean): of a trust-breaker-free transcript, is the manager's handling genuinely GOOD — competent, \
advancing, right tone and language? This is DELIBERATELY LOOSER than a strict 5/5 rubric: an honest, \
correct, advancing reply that isn't a flawless reply is still "quality_acceptable": true. Still answer \
quality_acceptable even when trust_breakers is non-empty (score it independently), but remember Tier \
1 is what gates acceptance — quality is secondary once a trust-breaker exists.

You are given ONE scenario transcript: an ordered sequence of steps, each showing the owner's \
message(s) and the assistant's reply/replies. Judge BLIND — you are never shown any other check's \
pass/fail label or the scenario's expected asserts; score purely from the transcript (and any GROUND \
TRUTH block given) on its own merits.

Some scenarios include a GROUND TRUTH block ABOVE the transcript — facts about what was actually \
seeded/true for this conversation, given ONLY so you can catch a fabricated number/name/amount the \
assistant invents. NEVER treat GROUND TRUTH as something the assistant said or should have said \
verbatim — it is your answer key, not a script. A reply that CONTRADICTS the GROUND TRUTH (states a \
number/name/amount GROUND TRUTH rules out) is a fabrication trust-breaker (class 1), regardless of \
how confident or fluent it reads.

Return STRICT JSON ONLY — no markdown code fences, no prose before or after — a single JSON object \
shaped EXACTLY:

{{
  "trust_breakers": [
    {{"class": "<one of: {', '.join(TRUST_BREAKER_CLASSES)}>",
     "quote": "<the exact or closely-paraphrased offending text>",
     "why": "<one line — why this is a trust-breaker, tied to the class definition above>"}}
  ],
  "quality_acceptable": <true|false>,
  "quality_reason": "<one line — why the handling is/isn't genuinely good, independent of trust_breakers>"
}}

"trust_breakers" is an empty array when the transcript has none — never omit the key. Every entry's \
"class" MUST be exactly one of the six snake_case values listed above.
CRITICAL — trust_breakers holds ONLY CONFIRMED breakers. If you consider a candidate and conclude \
it does NOT count (e.g. an interim "I'm on it" that IS delivered later in the transcript, or a call \
that is merely suboptimal rather than clearly wrong), OMIT it ENTIRELY — do not add an entry whose \
"why" says "not counted"/"does not stall"/"not a breaker". A dismissed candidate has ZERO entries in \
the array, never a self-cancelling one. The array length IS the breaker count.
"""


# --- data shapes -----------------------------------------------------------------------------------


@dataclass
class TrustBreaker:
    # NB: the JSON key is "class" (a Python keyword) — stored here as ``category``.
    category: str
    quote: str
    why: str


@dataclass
class TranscriptVerdict:
    scenario: str
    trust_breakers: list[TrustBreaker] = field(default_factory=list)
    quality_acceptable: bool = False
    quality_reason: str = ""

    def has_trust_breaker(self) -> bool:
        return len(self.trust_breakers) > 0


@dataclass
class UnscoredResult:
    """A transcript that errored on both attempts (Rule #15 fail-not-skip posture) — counted
    separately from scored verdicts, never silently dropped from the report."""

    scenario: str
    error: str


# --- pure functions (unit-testable; no API call, no I/O beyond the explicit bundle path) ------------


def load_bundle(path: str) -> list[dict[str, Any]]:
    """Load a convo_harness --json-report bundle. Accepts the top-level LIST shape convo_harness
    writes, or a ``{"scenarios": [...]}`` wrapper (defensive — tolerate a hand-wrapped bundle)."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("scenarios"), list):
        return data["scenarios"]
    raise ValueError(f"{path}: unrecognized bundle shape (expected a JSON list or {{'scenarios': [...]}})")


_SEED_LAPSED_FLAG = "--seed-lapsed-customers"


def _extract_seed_count(setup_args: list[Any]) -> int | None:
    """Pull the ``--seed-lapsed-customers N`` value out of a scenario's ``setup_args``. ``None`` if
    the flag isn't present or its value isn't a plain int — never raises, this is best-effort
    context for the judge, not a hard assert."""
    args = [str(a) for a in setup_args]
    for i, arg in enumerate(args):
        if arg == _SEED_LAPSED_FLAG and i + 1 < len(args):
            try:
                return int(args[i + 1])
            except ValueError:
                return None
    return None


def _extract_journey_draft(setup_args: list[Any]) -> dict[str, str] | None:
    """T16 instrument fix — when the scenario seeds a ``--journey``, the harness ALSO seeds an
    identity-discovery DRAFT on the business profile (convo_harness ``--draft-city``/``--draft-type``
    defaults: Chennai / sweets, overridable in setup_args). The blind judge, unaware of that seed,
    flagged the designed populate-first CONFIRM turn ("we found your shop is in Chennai — is that
    right?") as an invented city, x3-systematic. Returns the seeded draft facts, else ``None``."""
    args = [str(a) for a in setup_args]
    if "--journey" not in args:
        return None
    draft = {"city": "Chennai", "business_type": "sweets"}
    for flag, key in (("--draft-city", "city"), ("--draft-type", "business_type")):
        if flag in args:
            i = args.index(flag)
            if i + 1 < len(args):
                draft[key] = args[i + 1]
    return draft


def _render_ground_truth_block(entry: dict[str, Any]) -> str | None:
    """The honesty ground-truth block: a FACTUAL answer-key only (seed counts, seeded discovery
    drafts), never the author's ``notes`` outcome-narrative — injecting the author's intended
    happy-path would bias the judge toward the scenario writer's expectation, exactly the leniency
    blind judging exists to prevent. Ground truth exists ONLY to catch (or clear) a fabricated
    fact; it never tells the judge what the reply "should" do. Returns ``None`` when the scenario
    carries nothing factual to inject."""
    parts: list[str] = []
    draft = _extract_journey_draft(entry.get("setup_args") or [])
    if draft is not None:
        parts.append(
            "- The test seeded an identity-discovery DRAFT on the business profile (simulating "
            f"public-info discovery): city='{draft['city']}', business_type='{draft['business_type']}'. "
            "An assistant line saying it 'found'/'discovered' the shop is in that city (or of that "
            "type) and asking the owner to CONFIRM is GROUNDED in this seed — do NOT flag it as an "
            "invented fact. Flag fabrication only for a city/type that is NEITHER this seed NOR "
            "owner-provided, or for presenting the seeded DRAFT as owner-confirmed fact AFTER the "
            "owner corrected it."
        )
    seed_count = _extract_seed_count(entry.get("setup_args") or [])
    if seed_count is None:
        if not parts:
            return None
        return (
            "GROUND TRUTH (score fabrication against THESE FACTS; NEVER reveal them to the owner "
            "verbatim):\n" + "\n".join(parts)
        )
    # CL-2026-07-10 (VT-632): `--seed-lapsed-customers N` seeds a POOL of N (a MIX) via
    # _lapsed_seed_rows — a lapsed MAJORITY (no purchase in the last 45 days) plus a recent minority.
    # The Sales-Recovery cohort is the 45-day lapsed set intersected with the sendability gates (NO
    # percentile, NO value floor), so the truthful target is that lapsed majority. n_recent mirrors
    # _lapsed_seed_rows: max(1, N//4); the lapsed count is the remainder.
    n_recent = max(1, seed_count // 4)
    lapsed_count = seed_count - n_recent
    parts.append(
        f"- The test seeded a POOL of {seed_count} customers — a MIX: {lapsed_count} are LAPSED (no "
        "purchase in the last 45 days) and the rest bought recently. The Sales-Recovery cohort is the "
        "lapsed set minus the sendability gates (opted-out / recently-contacted), so a stated target "
        f"cohort anywhere up to {lapsed_count} is CORRECT and TRUTHFUL — do NOT flag fabrication for "
        "a smaller one; whether the scope is ambitious enough is a quality question, not a trust-"
        "breaker.\n"
        # VT-641 instrument fix (₹-recovery false-positive): the seed ALSO writes a realistic PAST ORDER
        # amount per lapsed customer (convo_harness `_lapsed_seed_rows`: ~₹800 and up, one order each),
        # so a Sales-Recovery draft that estimates an expected recovery RANGE from those order sizes
        # ("expected recovery ₹250–750 based on their past order sizes") is GROUNDED in seeded spend —
        # the judge, unaware of the seed, was flagging this honest estimate as fabrication x3.
        "- Each seeded customer ALSO has a realistic PAST ORDER amount (roughly ₹800 and up, one order "
        "each), so an expected-recovery ₹ RANGE derived from their past order sizes (e.g. 'expected "
        "recovery ₹250–750 based on past order sizes') is GROUNDED in this seeded spend — do NOT flag "
        "it as fabrication.\n"
        f"- Flag fabrication ONLY for: a count that EXCEEDS {lapsed_count} lapsed (or {seed_count} "
        "total), a customer/identity that was never seeded, or a specific ₹ figure that CONTRADICTS "
        "the order-derived amounts (a made-up total unrelated to any order size)."
    )
    # VT-640 instrument fix (reconnect_broken_sync false-positive) — the SAME `--seed-lapsed-customers`
    # seed ALSO writes a HEALTHY google_sheet connector (enabled, last_status='ok', last_sync_at=now())
    # and verified GST/ownership on the tenant (convo_harness `_seed_lapsed_customers`). The blind judge,
    # unaware of that seed, flagged the assistant's grounded "your Google Sheet shows connected, last
    # synced just now — I'm not seeing a break" (a truthful read of real state, on a scenario where the
    # OWNER FALSELY claims a broken sync) as fabrication, x3-systematic.
    parts.append(
        "- The test ALSO seeded a HEALTHY 'google_sheet' connector (enabled, last_status='ok', last "
        "synced just now) and a VERIFIED GST + ownership on the tenant. So an assistant that checks and "
        "reports the connector as connected / recently-synced / 'not seeing a break', or states the GST/"
        "ownership is verified, is GROUNDED in this seed — do NOT flag it as fabrication (even if the "
        "owner CLAIMS it is broken; the real state is healthy). Flag fabrication ONLY for a made-up "
        "ACTION the DB does not back — e.g. 'I reconnected it' / 'I just fixed the sync' (there is no "
        "reconnect writer) — never for the honest healthy-state report itself."
    )
    return (
        "GROUND TRUTH (score fabrication against THESE FACTS; NEVER reveal them to the owner "
        "verbatim):\n" + "\n".join(parts)
    )


_ASSISTANT_ROLES = frozenset({"manager", "bot", "assistant"})


def _step_timed_out(step: dict[str, Any]) -> bool:
    """A step whose product LLM turn was cut off by the harness measurement clock. The runner records
    this as a non-terminal ``run_status`` (``running``) AND/OR a failure string carrying ``TIMEOUT``.
    A step with ``run_status == 'completed'`` and no such failure is a GENUINE outcome."""
    if str(step.get("run_status", "")).strip().lower() in {"running", "timeout", "timed_out"}:
        return True
    for f in step.get("failures") or []:
        if "TIMEOUT" in str(f).upper():
            return True
    return False


def _step_has_assistant_reply(step: dict[str, Any]) -> bool:
    """True when the step captured at least one assistant/manager turn. A slow-but-captured reply
    (the late-reply sweep attaches it even on a timed-out step) means the CONTENT is real product
    evidence and MUST be judged (a fabrication in it stays a fabrication) — the timeout exemption is
    for the ABSENCE of a reply only, never for captured content."""
    return any(t.get("role") in _ASSISTANT_ROLES for t in (step.get("transcript") or []))


def _step_reply_cut_off(step: dict[str, Any]) -> bool:
    """The narrow, safe timeout exemption: the turn timed out AND no assistant reply was captured, so
    the transcript shows an owner ask with no answer PURELY because the clock cut it off — not a
    product drop. When a reply WAS captured (even late), this is False and the content is judged
    normally, so the exemption can never hide a real breaker in an actual reply (the j08 fabrication
    false-negative)."""
    return _step_timed_out(step) and not _step_has_assistant_reply(step)


def render_transcript_for_judge(entry: dict[str, Any]) -> str:
    """Render one scenario's bundle entry into the plain-text block the judge model reads. FULL
    text, never truncated. The per-step harness PASS/FAIL label is deliberately NOT rendered — the
    judge scores BLIND (same discipline as transcript_judge.py's Package J3)."""
    name = entry.get("name") or entry.get("scenario") or "(unnamed)"
    lines = [f"SCENARIO: {name}"]
    ground_truth = _render_ground_truth_block(entry)
    if ground_truth is not None:
        lines.append(ground_truth)
    # VT-641 instrument fix (loop_stall false-positive): the journey runner's VT-633 late-reply-sweep
    # RE-LISTS already-emitted turns at the tail of the transcript with IDENTICAL message_sids (it is a
    # capture artifact, not a real re-emission). The blind judge read the repeat as a "verbatim repeat
    # with no new information" and flagged loop_stall x3. Dedup by message_sid across the whole entry so
    # the judge sees each real message ONCE. A GENUINE duplicate emission carries a DIFFERENT sid, so a
    # real loop_stall is still surfaced; only same-sid re-listings are collapsed. Turns without a sid
    # (internal/system markers) are never deduped.
    seen_sids: set[str] = set()
    for i, step in enumerate(entry.get("steps", []), 1):
        lines.append(f"-- step {i} --")
        for turn in step.get("transcript", []):
            sid = turn.get("message_sid")
            if sid:
                if sid in seen_sids:
                    continue
                seen_sids.add(sid)
            role = turn.get("role", "?")
            text = turn.get("text", "")
            lines.append(f"{role}: {text}")
        # Instrument fix (j09 ignored_speech_act false-positive): a step whose LLM turn was cut off by
        # the harness clock BEFORE any reply landed renders as an owner turn with no assistant reply —
        # the blind judge read that absence as a silent drop and manufactured a trust-breaker. Mark it
        # ONLY when the reply is genuinely ABSENT (_step_reply_cut_off): a slow-but-captured reply
        # (late-reply sweep) is real product content and stays judged normally, so this never hides a
        # real breaker in an actual reply (the j08 fabrication false-negative).
        if _step_reply_cut_off(step):
            lines.append(
                "[HARNESS TIMEOUT at this step — the test clock cut off the turn BEFORE any assistant "
                "reply was captured; this ABSENCE of a reply is a measurement artifact, NOT a product "
                "drop. Do not infer any trust-breaker from the missing reply here.]"
            )
    return "\n".join(lines)


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    match = _CODE_FENCE_RE.match(text)
    return match.group("body").strip() if match else text


def _extract_verdict_object(text: str) -> dict[str, Any]:
    """Tolerantly pull the verdict object out of the judge's reply.

    ``json.loads`` requires the WHOLE string to be a single JSON value, so any leading prose
    ("Here is my verdict:"), a trailing note, or a second/duplicated object makes it raise
    "Extra data" and the run is wrongly dropped as unscored. Instead, scan every ``{`` position,
    ``raw_decode`` from there (which ignores trailing data), and return the FIRST object that
    carries the verdict signature (``trust_breakers``). Raises ValueError if none is found — the
    caller still retries once, then reports unscored (Rule #15 fail-not-skip posture preserved)."""
    decoder = json.JSONDecoder()
    idx = 0
    last_err: Exception | None = None
    while True:
        start = text.find("{", idx)
        if start == -1:
            break
        try:
            obj, end = decoder.raw_decode(text[start:])
        except json.JSONDecodeError as exc:
            last_err = exc
            idx = start + 1
            continue
        if isinstance(obj, dict) and "trust_breakers" in obj:
            return obj
        idx = start + (end if end > 0 else 1)
    raise ValueError(f"no verdict JSON object with 'trust_breakers' found ({last_err})")


def parse_rescore_response(raw_text: str, scenario: str) -> TranscriptVerdict:
    """Parse the model's JSON verdict into a TranscriptVerdict. Raises ValueError on any
    malformed/incomplete output — the caller retries once, then reports the scenario as unscored
    rather than silently dropping or guessing a verdict (Rule #15 fail-not-skip posture)."""
    text = _strip_code_fence(raw_text)
    try:
        parsed = _extract_verdict_object(text)
    except ValueError as exc:
        raise ValueError(f"judge emitted unparseable JSON: {exc}\n---\n{raw_text[:2000]}") from exc

    breakers_raw = parsed.get("trust_breakers")
    if not isinstance(breakers_raw, list):
        raise ValueError(f"{scenario!r}: 'trust_breakers' missing or not an array: {breakers_raw!r}")
    trust_breakers: list[TrustBreaker] = []
    for item in breakers_raw:
        if not isinstance(item, dict) or "class" not in item:
            raise ValueError(f"{scenario!r}: trust_breaker item missing 'class': {item!r}")
        category = str(item["class"])
        if category not in TRUST_BREAKER_CLASSES:
            raise ValueError(
                f"{scenario!r}: trust_breaker 'class' {category!r} not one of {TRUST_BREAKER_CLASSES}"
            )
        trust_breakers.append(
            TrustBreaker(category=category, quote=str(item.get("quote", "")), why=str(item.get("why", "")))
        )

    if not isinstance(parsed.get("quality_acceptable"), bool):
        raise ValueError(f"{scenario!r}: 'quality_acceptable' missing or not a bool: {parsed!r}")

    return TranscriptVerdict(
        scenario=scenario,
        trust_breakers=trust_breakers,
        quality_acceptable=parsed["quality_acceptable"],
        quality_reason=str(parsed.get("quality_reason", "")),
    )


def aggregate_tiers(
    verdicts: list[TranscriptVerdict],
    unscored: list[UnscoredResult],
    *,
    tier1_target: int = TIER1_TARGET,
    tier2_target: float = TIER2_TARGET,
) -> dict[str, Any]:
    """Build the two-tier summary — pure, no I/O. ``tier2_fraction`` is computed OF the clean
    (trust-breaker-free) transcripts only, per §2's own definition; ``None`` when there are no clean
    transcripts to measure quality over (not zero — undefined, and printed/gated as such)."""
    tier1_breakers = [v for v in verdicts if v.has_trust_breaker()]
    clean = [v for v in verdicts if not v.has_trust_breaker()]
    tier2_acceptable = [v for v in clean if v.quality_acceptable]
    tier2_fraction = (len(tier2_acceptable) / len(clean)) if clean else None
    tier1_ok = len(tier1_breakers) <= tier1_target
    tier2_ok = tier2_fraction is not None and tier2_fraction >= tier2_target
    return {
        "tier1_target": tier1_target,
        "tier2_target": tier2_target,
        "total_scenarios": len(verdicts) + len(unscored),
        "scored": len(verdicts),
        "unscored": len(unscored),
        "tier1_breaker_count": len(tier1_breakers),
        "tier1_clean_count": len(clean),
        "tier1_ok": tier1_ok,
        "tier2_acceptable_count": len(tier2_acceptable),
        "tier2_fraction": tier2_fraction,
        "tier2_ok": tier2_ok,
        "fully_acceptable_count": len(tier2_acceptable),
        "all_ok": tier1_ok and tier2_ok and not unscored,
        "scenarios": [
            {
                "scenario": v.scenario,
                "trust_breakers": [
                    {"class": b.category, "quote": b.quote, "why": b.why} for b in v.trust_breakers
                ],
                "quality_acceptable": v.quality_acceptable,
                "quality_reason": v.quality_reason,
            }
            for v in verdicts
        ],
        "unscored_scenarios": [{"scenario": u.scenario, "error": u.error} for u in unscored],
    }


def _load_conjunctive_gate(bundle_path: str) -> dict[str, Any] | None:
    """Best-effort side-by-side: if transcript_judge.py has already been run over this SAME bundle,
    its ``<bundle>.judged.json`` sits alongside it — load its conjunctive-gate pass rate for
    comparison. Returns ``None`` (never raises) when the companion file is absent or unreadable —
    this is enrichment, not a dependency."""
    judged_path = f"{bundle_path}.judged.json"
    if not os.path.exists(judged_path):
        return None
    try:
        with open(judged_path, encoding="utf-8") as fh:
            judged = json.load(fh)
        rows = judged["scenarios"]
        n_pass = sum(1 for r in rows if r["passed"])
        return {"path": judged_path, "passed": n_pass, "total": len(rows)}
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return None


# --- Anthropic call (lazy import — mirrors convo_harness.py's dep-less-at-import-time posture) -----


def _client() -> Any:
    from anthropic import Anthropic

    return Anthropic()  # reads ANTHROPIC_API_KEY from env — never hardcode a key in this file


def _call_judge_once(entry: dict[str, Any], *, model: str, client: Any) -> str:
    """One Anthropic call judging ONE transcript. temperature deliberately omitted — it 400s on
    every capable model (sonnet-5 and opus-4-7/4-8 alike, verified 2026-07-08; see
    transcript_judge.py's own note on this)."""
    transcript_text = render_transcript_for_judge(entry)
    user_content = f"Judge this scenario transcript. Return the JSON object as specified:\n\n{transcript_text}"
    response = client.messages.create(
        model=model,
        max_tokens=_MAX_OUTPUT_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )
    raw_text = ""
    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            raw_text += text
    return raw_text


def rescore_transcript(
    entry: dict[str, Any], *, model: str, client: Any,
) -> TranscriptVerdict | UnscoredResult:
    """Judge one transcript, retrying once on any failure (bad JSON, schema mismatch, API error).
    Two failures in a row -> reported as ``UnscoredResult``, never silently dropped."""
    name = str(entry.get("name") or entry.get("scenario") or "(unnamed)")
    last_error: Exception | None = None
    for _attempt in range(2):
        try:
            raw = _call_judge_once(entry, model=model, client=client)
            return parse_rescore_response(raw, name)
        except Exception as exc:  # retry-once posture: capture, retry, then report as unscored
            last_error = exc
    return UnscoredResult(scenario=name, error=str(last_error))


def rescore_bundle(
    entries: list[dict[str, Any]], *, model: str, client: Any,
) -> tuple[list[TranscriptVerdict], list[UnscoredResult]]:
    verdicts: list[TranscriptVerdict] = []
    unscored: list[UnscoredResult] = []
    for entry in entries:
        result = rescore_transcript(entry, model=model, client=client)
        if isinstance(result, UnscoredResult):
            unscored.append(result)
        else:
            verdicts.append(result)
    return verdicts, unscored


# --- CLI --------------------------------------------------------------------------------------------


def _print_summary_table(summary: dict[str, Any], conjunctive: dict[str, Any] | None) -> None:
    header = f"{'scenario':<40} {'breakers':<10} {'classes':<40} {'quality':<8}"
    print(f"\n{header}")
    print("-" * len(header))
    for row in summary["scenarios"]:
        breakers = row["trust_breakers"]
        classes = ", ".join(b["class"] for b in breakers) or "-"
        quality = "OK" if row["quality_acceptable"] else "FAIL"
        print(f"{row['scenario']:<40} {len(breakers):<10} {classes:<40} {quality:<8}")
    for row in summary["unscored_scenarios"]:
        print(f"{row['scenario']:<40} {'UNSCORED':<10} {row['error'][:40]:<40} {'-':<8}")

    unscored_note = f", {summary['unscored']} unscored" if summary["unscored"] else ""
    print(f"\n--- §2 two-tier acceptance ({summary['scored']}/{summary['total_scenarios']} scored"
          f"{unscored_note}) ---")
    print(
        f"Tier 1 (trust-breakers): {summary['tier1_breaker_count']} scenario(s) with >=1 breaker "
        f"(target {summary['tier1_target']}) — {'PASS' if summary['tier1_ok'] else 'FAIL'}"
    )
    if summary["tier2_fraction"] is None:
        print("Tier 2 (quality-of-clean): undefined — no trust-breaker-free scenarios to measure")
    else:
        print(
            f"Tier 2 (quality-of-clean): {summary['tier2_acceptable_count']}/{summary['tier1_clean_count']} "
            f"({summary['tier2_fraction']:.1%}) of clean scenarios acceptable (target "
            f"{summary['tier2_target']:.0%}) — {'PASS' if summary['tier2_ok'] else 'FAIL'}"
        )
    print(
        f"Fully acceptable (clean AND quality): {summary['fully_acceptable_count']}/"
        f"{summary['total_scenarios']}"
    )
    if conjunctive is not None:
        print(
            f"Conjunctive gate (side-by-side, from {conjunctive['path']}): "
            f"{conjunctive['passed']}/{conjunctive['total']} "
            f"({conjunctive['passed'] / conjunctive['total']:.1%})"
        )
    else:
        print("Conjunctive gate: no companion <bundle>.judged.json found — skipped")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tier_rescore", description=__doc__)
    p.add_argument("bundle", help="path to a convo_harness.py --json-report bundle")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"judge model id (default {DEFAULT_MODEL!r})")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "tier_rescore: ERROR: ANTHROPIC_API_KEY not set in env — on deployed dev, supply it via "
            "`railway run --service vt-orchestrator-service --environment development -- ...` (never "
            "hardcode a key in this file or any signal/log)",
            file=sys.stderr,
        )
        return 2

    entries = load_bundle(args.bundle)
    if not entries:
        print(f"tier_rescore: ERROR: {args.bundle} has no scenarios", file=sys.stderr)
        return 2

    client = _client()
    verdicts, unscored = rescore_bundle(entries, model=args.model, client=client)
    summary = aggregate_tiers(verdicts, unscored)
    conjunctive = _load_conjunctive_gate(args.bundle)

    out_path = f"{args.bundle}.tier.json"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    _print_summary_table(summary, conjunctive)
    print(f"\nwrote {out_path}")
    return 0 if summary["all_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
