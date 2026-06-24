"""Canonical PII redactor (VT-104).

Single source of truth for redaction across the orchestrator. Replaces the
inline ``observability/pii.py`` implementation shipped under VT-101/102 —
that module now delegates here byte-identically so VT-101's LangSmith
traces and VT-102's ``pipeline_log`` JSONB rows stay regression-stable.

Two token format families
-------------------------
- **Key-driven redaction (VT-101 legacy format, PRESERVED byte-identical):**
  values at known-PII keys (``phone``, ``customer_name``, ``body``, etc.)
  produce ``phone_tok_HEX`` / ``body_tok_HEX`` / ``<redacted:customer_name:len=N>``
  tokens. Format is an internal API — changing it would invalidate prior
  audit artifacts and downstream query paths.
- **Pattern-driven redaction (VT-104 new patterns):** PAN / Aadhaar / IFSC /
  GST / credit card / long raw-string bodies / customer-name registry hits
  produce ``<type:redacted>`` or ``<type:hash:HEX>`` markers per the brief.

Bank-account redaction is KEY-DRIVEN ONLY
-----------------------------------------
A pure-digit bank-account regex (9-18 digits) would collide with phones
(10), Aadhaar (12), and credit cards (13-19). To keep false-positives
controlled, this module redacts bank-account-shaped values ONLY when they
appear at a known key (``bank_account`` / ``account_number`` / ``acct_no``).
The 7 pattern-driven types (phone, email, PAN, Aadhaar, IFSC, GST, CC)
still catch unstructured strings. Pure-digit bank-account regex detection
is deferred (see VT-104 sprint file Out of scope addendum).

Idempotency
-----------
Output tokens (``phone_tok_``, ``<phone:hash:``, ``<email:hash:``,
``<pan:redacted>``, ``<aadhaar:redacted>``, ``<ifsc:redacted>``,
``<gst:redacted>``, ``<cc:redacted>``, ``<body:hash:``, ``<customer_name>``,
``<redacted:...>``) are designed to NOT match any of the regex patterns.
``redact(redact(x)) == redact(x)`` holds by construction; the canary's
Group D #9 verifies on real data.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Callable
from typing import Any

# ---------------------------------------------------------------------------
# Regex patterns — order matters. Layout below also defines collision order
# (specific letter-formatted patterns first, then digit-shaped patterns from
# longest to shortest so e.g. a 16-digit CC isn't eaten by the 10-digit
# Indian-phone regex).
# ---------------------------------------------------------------------------

# PAN — five letters, four digits, one letter.
_PAN_RE = re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b")

# IFSC — 4 letters, '0', 6 alphanumerics.
_IFSC_RE = re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b")

# GSTIN — 15 chars: 2 digits / 5 letters / 4 digits / 1 letter / 1
# alphanumeric (not 0) / 'Z' / 1 alphanumeric.
_GSTIN_RE = re.compile(r"\b[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b")

# Email.
_EMAIL_RE = re.compile(r"\b[\w.+\-]+@[\w.\-]+\.\w+\b")

# Credit-card-like — 13-19 contiguous digits, optional space/hyphen
# separators, word-boundaried. Luhn validated downstream.
_CC_CANDIDATE_RE = re.compile(r"\b(?:\d[ \-]?){12,18}\d\b")

# Aadhaar — 12 digits in groups of 4 (with optional separator); word-
# boundaried so it doesn't bite into longer numeric tokens (hash hex etc.).
# UUID guard (VT-369 CI flake): ``\b`` treats '-' as a boundary, so a uuid4 whose final 12-hex
# segment is all-numeric (~1-in-285, e.g. ...-a660-214698525976) matched as an "Aadhaar" and id-
# bearing event payloads were corrupted. The hex-hyphen lookbehind/lookahead suppress matches
# inside a UUID shape; a real Aadhaar embedded in a hex-hyphen context is the accepted (rare)
# false-negative.
_AADHAAR_RE = re.compile(r"(?<![0-9a-fA-F]-)\b\d{4}\s?\d{4}\s?\d{4}\b(?!-[0-9a-fA-F])")

# Phone — two narrow forms; intentionally NOT a generic 7-15-digit catch-all
# (the old VT-101 pattern caught hash hex and CC + Aadhaar digit runs, which
# broke idempotency + collided with CC/Aadhaar patterns above).
#   - E.164 explicit +: required '+' prefix, 7-15 total digits.
#   - Indian 10-digit at word boundary, with optional 5-5 split or hyphen
#     separator. Conservative: a plain 10-digit run elsewhere in a hash
#     output (very rare given hex distribution but possible) is the cost
#     of the false-positive vs false-negative trade.
_PHONE_E164_RE = re.compile(r"\+\d[\d \-]{6,18}\d")
_PHONE_IN_RE = re.compile(r"\b(?:\d{5}[\s\-]?\d{5}|\d{10})\b")

# Keys whose values are categorically PII regardless of content (VT-101 inherited).
_PII_KEYS: frozenset[str] = frozenset(
    {
        "name",
        "customer_name",
        "owner_name",
        "first_name",
        "last_name",
        "email",
        "phone",
        "phone_e164",
        "mobile",
        "address",
        "body",
        "message_body",
        "raw_body",
        "stack_trace",
        "stacktrace",
        "error_message",
    }
)

# Keys whose values redact as bank-account markers (no regex variant).
_BANK_KEYS: frozenset[str] = frozenset(
    {"bank_account", "account_number", "acct_no"}
)

# Long-body threshold for raw-string detection (brief §1).
_LONG_BODY_THRESHOLD: int = 200

# Default max recursion depth (brief §1).
DEFAULT_MAX_DEPTH: int = 5


# ---------------------------------------------------------------------------
# Salt + hash helpers — VT-101 token format preserved.
# ---------------------------------------------------------------------------

def _salt() -> str:
    salt = os.environ.get("TEAM_PHONE_HASH_SALT", "")
    if not salt:
        # Fallback so dev + tests don't crash; orchestrator init checks env at boot.
        return "vt-101-fallback-salt-not-for-prod"
    return salt


def _hash_body(body: str) -> str:
    """SHA256[:16] body token — VT-101 byte-identical."""
    digest = hashlib.sha256(f"{_salt()}:body:{body}".encode()).hexdigest()
    return f"body_tok_{digest[:16]}"


def _hash_phone(phone: str) -> str:
    digest = hashlib.sha256(f"{_salt()}:phone:{phone}".encode()).hexdigest()
    return f"phone_tok_{digest[:16]}"


def _hash_email(email: str) -> str:
    digest = hashlib.sha256(f"{_salt()}:email:{email}".encode()).hexdigest()
    return f"<email:hash:{digest[:16]}>"


def _hash_raw_body(body: str) -> str:
    digest = hashlib.sha256(f"{_salt()}:rawbody:{body}".encode()).hexdigest()
    return f"<body:hash:{digest[:16]}>"


# ---------------------------------------------------------------------------
# Luhn check (Phase 1 — single source of truth for CC validation).
# ---------------------------------------------------------------------------

def _is_luhn_valid(digits: str) -> bool:
    """Return True iff ``digits`` (already stripped to 0-9) passes Luhn."""
    if not digits or not digits.isdigit():
        return False
    total = 0
    parity = len(digits) % 2
    for i, c in enumerate(digits):
        n = int(c)
        if i % 2 == parity:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


# ---------------------------------------------------------------------------
# String-level pass — pattern substitutions, ordered to avoid collisions.
# ---------------------------------------------------------------------------

def _redact_str(value: str) -> str:
    """Apply pattern regexes in collision-safe order. Idempotent.

    Order: PAN → IFSC → GSTIN → email → CC (Luhn-validated) → Aadhaar →
    E.164 phone → Indian-10-digit phone → long-body. The order is the
    cure for the collision class that "too clean" warned about: a generic
    7-15-digit phone regex would eat 12-digit Aadhaar, 13-19-digit CC, and
    pure-digit runs inside email/body hash hex (breaking idempotency).

    Tokens emitted by each step are designed to NOT match any subsequent
    pattern, so this function is idempotent on its own output.
    """
    if not value:
        return value

    # 1. PAN (letter-anchored; no digit-pattern collision).
    value = _PAN_RE.sub("<pan:redacted>", value)

    # 2. IFSC.
    value = _IFSC_RE.sub("<ifsc:redacted>", value)

    # 3. GSTIN.
    value = _GSTIN_RE.sub("<gst:redacted>", value)

    # 4. Email — substitute BEFORE phone so the digit-bearing local-part
    # (e.g. ``customer.support99@gmail.com``) doesn't get phone-tokenised.
    value = _EMAIL_RE.sub(lambda m: _hash_email(m.group(0)), value)

    # 5. E.164 phone (explicit '+'). Runs BEFORE Aadhaar so an E.164 string
    # like ``+919876543210`` (whose last 12 digits would otherwise match
    # Aadhaar's 12-digit pattern) is consumed as a phone — preserves
    # VT-101's byte-identical token format for the LangSmith / pipeline_log
    # regression assertions.
    def _phone_sub(match: re.Match[str]) -> str:
        phone = match.group(0)
        if sum(c.isdigit() for c in phone) < 7:
            return phone
        return _hash_phone(re.sub(r"[ \-]", "", phone))

    value = _PHONE_E164_RE.sub(_phone_sub, value)

    # 6. Credit card — Luhn-validated only. BEFORE Aadhaar + Indian-phone so
    # 16-digit CC doesn't get caught by either.
    def _cc_sub(match: re.Match[str]) -> str:
        raw = match.group(0)
        digits = re.sub(r"[ \-]", "", raw)
        if not (13 <= len(digits) <= 19):
            return raw
        if _is_luhn_valid(digits):
            return "<cc:redacted>"
        return raw

    value = _CC_CANDIDATE_RE.sub(_cc_sub, value)

    # 7. Aadhaar — runs AFTER E.164 + CC so it doesn't eat their bodies.
    value = _AADHAAR_RE.sub("<aadhaar:redacted>", value)

    # 8. Indian 10-digit phone at word boundary.
    value = _PHONE_IN_RE.sub(_phone_sub, value)

    # 8. Long raw-string body — only after all pattern subs above so we
    # don't hash the substituted output spuriously. Threshold per brief §1.
    if len(value) > _LONG_BODY_THRESHOLD and not _is_already_redacted(value):
        return _hash_raw_body(value)

    return value


def _is_already_redacted(value: str) -> bool:
    """Heuristic: ``value`` is already a redactor output token.

    Returns True for prefixes the redactor itself emits (``phone_tok_``,
    ``body_tok_``, ``<body:hash:``, ``<email:hash:``, ``<phone:hash:``,
    ``<pan:redacted>``, ``<aadhaar:redacted>``, ``<ifsc:redacted>``,
    ``<gst:redacted>``, ``<cc:redacted>``, ``<bank:redacted:``,
    ``<customer_name>``, ``<owner_name>``, ``<redacted:…>``,
    ``<redaction_truncated>``). Used by both the long-body short-circuit
    AND the named-key idempotency guard.
    """
    return value.startswith(
        (
            "phone_tok_",
            "body_tok_",
            "<body:hash:",
            "<email:hash:",
            "<phone:hash:",
            "<pan:redacted>",
            "<aadhaar:redacted>",
            "<ifsc:redacted>",
            "<gst:redacted>",
            "<cc:redacted>",
            "<bank:redacted",
            "<customer_name>",
            "<owner_name>",
            "<redacted:",
            "<redaction_truncated>",
        )
    )


# ---------------------------------------------------------------------------
# Named-key redaction — VT-101 output preserved byte-identical.
# ---------------------------------------------------------------------------

def _redact_pii_value(
    key_lower: str,
    value: Any,
    name_registry: Callable[[str], bool] | None,
) -> str:
    """Tokenize a value at a known-PII key. VT-101 format preserved.

    Idempotent: if ``value`` is already in a known token form, return it
    unchanged so ``redact(redact(x)) == redact(x)`` holds at named keys.
    """
    if value is None:
        return "<redacted:none>"

    text = str(value)
    # Idempotency: pre-existing tokens pass through unchanged.
    if _is_already_redacted(text):
        return text

    if key_lower in {"phone", "phone_e164", "mobile"}:
        return _hash_phone(text)
    if key_lower in {"body", "message_body", "raw_body",
                     "stack_trace", "stacktrace", "error_message"}:
        return _hash_body(text)
    if key_lower == "email":
        return "<redacted:email>"
    if key_lower in {"customer_name", "owner_name"} and name_registry is not None:
        if name_registry(text):
            return "<customer_name>" if key_lower == "customer_name" else "<owner_name>"
        return f"<redacted:{key_lower}:len={len(text)}>"
    # name / address / etc — VT-101 token format with length hint.
    return f"<redacted:{key_lower}:len={len(text)}>"


def _redact_bank_value(value: Any) -> str:
    """Tokenize a value at a known bank key."""
    if value is None:
        return "<redacted:none>"
    return f"<bank:redacted:len={len(str(value))}>"


# ---------------------------------------------------------------------------
# Public API — recursive walker over dict / list / tuple / str.
# ---------------------------------------------------------------------------

def redact(
    value: Any,
    depth: int = 0,
    max_depth: int = DEFAULT_MAX_DEPTH,
    name_registry: Callable[[str], bool] | None = None,
) -> Any:
    """Return a PII-safe copy of ``value``.

    - ``str``: pattern-driven redaction via :func:`_redact_str`. Customer-
      name registry-driven exact-match scans run when ``name_registry``
      is provided.
    - ``dict``: keys preserved; values at keys in :data:`_PII_KEYS` go
      through :func:`_redact_pii_value` (VT-101 byte-identical output);
      values at keys in :data:`_BANK_KEYS` get the bank marker; other
      values recurse.
    - ``list`` / ``tuple``: recurses element-wise; type preserved.
    - Other types: returned unchanged.

    ``max_depth`` defaults to 5 per brief §1. Depths beyond return
    ``"<redaction_truncated>"``.

    Idempotent by design: re-applying ``redact`` to its output is a no-op.

    ``name_registry`` is a Phase-1 fallback for the customer-name
    tokenisation gap — there is no ``customers`` table yet (audit:
    migrations 000-022). The canary wires a synthetic in-memory set; the
    future VT row that adds the table replaces this with a SQL lookup
    without API change.
    """
    if depth > max_depth:
        return "<redaction_truncated>"

    if isinstance(value, str):
        out = _redact_str(value)
        # Customer-name registry exact-match scan inside the raw string —
        # only when caller provided a registry and the value isn't already
        # a token. Keeps cost low for the no-registry case.
        if name_registry is not None and not _is_already_redacted(out):
            out = _scan_for_registry_names(out, name_registry)
        return out

    if isinstance(value, dict):
        result: dict[Any, Any] = {}
        for k, v in value.items():
            key_str = str(k).lower()
            if key_str in _PII_KEYS:
                result[k] = _redact_pii_value(key_str, v, name_registry)
            elif key_str in _BANK_KEYS:
                result[k] = _redact_bank_value(v)
            else:
                result[k] = redact(v, depth + 1, max_depth, name_registry)
        return result

    if isinstance(value, list):
        return [redact(item, depth + 1, max_depth, name_registry) for item in value]

    if isinstance(value, tuple):
        return tuple(
            redact(item, depth + 1, max_depth, name_registry) for item in value
        )

    return value


_PUNCT_STRIP_RE = re.compile(r"^[^\w]+|[^\w]+$")
_PREFIX_PUNCT_RE = re.compile(r"^[^\w]+")
_SUFFIX_PUNCT_RE = re.compile(r"[^\w]+$")
# Possessive clitic on a NAME token ("Ramesh's", "Ramesh’s", "Suresh'"). The
# bare name still leaks through the possessive, so strip the clitic before the
# registry test and re-attach it on the output side. Straight + curly apostrophe.
_POSSESSIVE_RE = re.compile(r"(['’]s|['’])$")

# VT-412 PR-D (adversarial-review Finding 2) — tokenizer separator. The prior
# scan did `text.split(" ")` (literal space only) and stripped punctuation only
# at token boundaries, so a registered name GLUED to adjacent text leaked:
# tab/newline-separated ("Lakshmi\tDevi", "Lakshmi\ncalled"), interior
# comma/period/slash ("Lakshmi,Devi", "Lakshmi.called"), em/en-dash
# ("Lakshmi—urgent"), and the non-breaking space U+00A0 ("Lakshmi\xa0Devi").
# We now tokenize on:
#   (1) any run of whitespace — Python's \s already matches \xa0 (NBSP) and all
#       Unicode separators, so \s+ splits tab/newline/NBSP-glued names; AND
#   (2) any run of INTERIOR punctuation flanked by word chars on BOTH sides
#       ((?<=\w)[^\w\s'’]+(?=\w)) — splits a glued name (comma/period/slash/
#       em-dash between two letters) but leaves BRACKETING punctuation attached
#       (a leading "(" / trailing ")" is not flanked by \w on the outer side, so
#       the existing prefix/suffix preservation still owns it) and leaves the
#       possessive apostrophe inside the token (the _POSSESSIVE_RE path is
#       unchanged). The separator is a CAPTURING group so the original
#       whitespace/punctuation is reconstructed byte-for-byte for non-matched
#       tokens (re.split → [sep0, tok0, sep1, tok1, …]).
_TOKEN_SPLIT_RE = re.compile(r"(\s+|(?<=\w)[^\w\s'’]+(?=\w))")

# Window sizes the registry scan tries, LONGEST first so a 3-token registered
# name ("Mohammed Abdul Rahman") matches the 3-gram before any of its 2-gram or
# 1-gram subspans. 3 covers the multi-part Indian/Hindi/Muslim names in tenant
# data; 1 covers mononyms (the VT-412 gap — a single-token customer name in
# agent think-text was never tested before, so it survived to the VTR replay).
_REGISTRY_SCAN_WINDOWS = (3, 2, 1)


def _scan_for_registry_names(text: str, name_registry: Callable[[str], bool]) -> str:
    """Replace registered customer names found inside ``text`` with ``<customer_name>``.

    Tokenize on whitespace AND interior name-gluing punctuation (``_TOKEN_SPLIT_RE``),
    then slide windows of size 3 → 2 → 1 (longest-first) over the punctuation-stripped
    tokens and exact-match each candidate span against the registry. Longest-first so
    a multi-token registered name is matched whole, not as a stray sub-token.

    VT-412 — the scan tests SINGLE tokens (window size 1) and strips a trailing
    possessive clitic ("Ramesh's" → tests "Ramesh"). The prior implementation only
    formed consecutive 2-grams, so a mononym customer name ("Rajesh"), any 3+-token
    registered name, and a possessive form all slipped through into
    ``decision_rationale`` agent think-text and survived to a VTR's run replay.

    VT-412 PR-D (adversarial-review Finding 2) — tokenization was ``text.split(" ")``
    (literal space only), so a registered name GLUED to adjacent text by a tab,
    newline, NBSP (U+00A0), interior comma/period/slash, or em/en-dash leaked. The
    tokenizer now splits on any whitespace (NBSP included) AND interior punctuation
    flanked by word chars on both sides, while keeping the ORIGINAL separators for
    byte-exact reconstruction of non-matched text and leaving bracketing punctuation
    + the possessive apostrophe attached to the token.

    The registry predicate is exact-match (case-folded), so a single-token scan only
    ever redacts a token that IS a registered customer name — no false-positive
    widening of the no-name path. Bracketing punctuation + the possessive clitic are
    preserved so the surrounding sentence still reads (``<customer_name>'s``).

    False negatives remain accepted for names whose tokenization differs from
    the registered form (Phase-1 limitation); a name the registry does not hold
    is never inferred.
    """
    # re.split on a CAPTURING separator → [tok0, sep0, tok1, sep1, …, tokN].
    # Tokens at even indices; the original separator that followed each token at
    # the odd index after it. Reconstructing token+sep verbatim is byte-exact.
    parts = _TOKEN_SPLIT_RE.split(text)
    tokens = parts[0::2]
    seps = parts[1::2]  # len(seps) == len(tokens) - 1; seps[i] follows tokens[i]
    if not tokens:
        return text
    out: list[str] = []
    i = 0
    n = len(tokens)
    while i < n:
        matched = False
        for window in _REGISTRY_SCAN_WINDOWS:
            if window > n - i:
                continue
            span = tokens[i : i + window]
            words = [_PUNCT_STRIP_RE.sub("", tok) for tok in span]
            # Strip a possessive clitic from the LAST word only (the bare name is
            # what the registry holds; "Ramesh's" → "Ramesh"). The matched clitic
            # is re-attached on the output side so the sentence still reads.
            poss_match = _POSSESSIVE_RE.search(words[-1]) if words else None
            possessive = ""
            if poss_match:
                possessive = poss_match.group(0)
                words[-1] = words[-1][: poss_match.start()]
            # Skip a window whose tokens are empty (a leading/trailing/double
            # separator yields an empty token) — the registered form has no empty
            # parts. The candidate is the space-joined registry form regardless of
            # whether the source glued the parts with whitespace, NBSP, a comma, or
            # a dash — the exact-match registry only fires on a real registered name,
            # so fusing across an interior glue can't widen the no-name path.
            if any(not w for w in words):
                continue
            candidate = " ".join(words)
            if name_registry(candidate):
                # Preserve bracketing punctuation + the possessive clitic so the
                # surrounding sentence still reads. Leading whitespace is now the
                # PRECEDING separator (emitted already), never inside the token.
                first, last = span[0], span[-1]
                prefix_match = _PREFIX_PUNCT_RE.search(first)
                suffix_match = _SUFFIX_PUNCT_RE.search(last)
                prefix = first[: prefix_match.end()] if prefix_match else ""
                trailing_punct = last[suffix_match.start():] if suffix_match else ""
                out.append(f"{prefix}<customer_name>{possessive}{trailing_punct}")
                # Internal separators are dropped (subsumed into the one redaction
                # token); emit the separator that FOLLOWS the matched span.
                if i + window - 1 < len(seps):
                    out.append(seps[i + window - 1])
                i += window
                matched = True
                break
        if matched:
            continue
        # No match at i — emit the token verbatim plus the separator that follows
        # it (byte-exact reconstruction), then advance one token.
        out.append(tokens[i])
        if i < len(seps):
            out.append(seps[i])
        i += 1
    return "".join(out)


__all__ = [
    "DEFAULT_MAX_DEPTH",
    "redact",
]
