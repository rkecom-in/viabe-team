#!/usr/bin/env python3
"""VT-403 — safe env-inspection helper. Emits ONLY presence/equality BOOLEANS, never a value.

Why this exists
---------------
CC leaked a live secret VALUE into model context twice by printing raw ``railway variables`` /
``--json`` output (``RESEND_API_KEY``, then a PROD ``ANTHROPIC_API_KEY`` fragment) — a CL-431
breach (a secret in CC's turn context goes to Anthropic that turn). A verbal "don't grep raw
variables" rule did not hold. This helper is the tooling-level fix: it reads values INTERNALLY to
compute a boolean, but it is structurally incapable of emitting a value — every code path prints
only ``NAME: set|unset`` or ``LABEL: MATCH|MISMATCH|unset``.

ALL env inspection that touches a secret store MUST go through this helper (CLAUDE.md Rule #18).

Usage
-----
  # presence of names in the CURRENT process env:
  python3 scripts/env_presence.py presence --source env NAME1 NAME2 ...

  # presence of names in a Railway env/service (reads ``railway variables --json`` internally):
  python3 scripts/env_presence.py presence --source railway \
      --environment development --service vt-orchestrator-service NAME1 NAME2 ...

  # equality across two value specs — prints only MATCH|MISMATCH|unset:
  #   spec = env:NAME | railway:NAME | literal:VALUE   (railway:* uses --environment/--service)
  python3 scripts/env_presence.py equal account \
      env:TEAM_TWILIO_ACCOUNT_SID railway:TEAM_TWILIO_ACCOUNT_SID \
      --environment development --service vt-orchestrator-service

Output is the ONLY thing emitted to stdout. Errors (unreachable source) go to stderr WITHOUT any
value and exit non-zero. Presence/equality is information, not failure — exit 0 on success.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from functools import lru_cache


@lru_cache(maxsize=8)
def _railway_vars(environment: str, service: str) -> tuple[tuple[str, bool], ...]:
    """Return ((name, has_nonempty_value), ...) for a Railway env/service.

    Reads ``railway variables --json`` in a subprocess and parses it HERE; the raw JSON is never
    returned or printed. Only the name + a presence boolean per var escapes this function — and the
    value itself is collapsed to a bool before leaving, so no caller can recover it. The actual
    value is kept in a separate private cache for ``equal`` (see ``_railway_value``).
    """
    data = _railway_raw(environment, service)
    return tuple((k, bool((v or "").strip())) for k, v in data.items())


@lru_cache(maxsize=8)
def _railway_raw(environment: str, service: str) -> dict[str, str]:
    proc = subprocess.run(
        ["railway", "variables", "--environment", environment, "--service", service, "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        # Never echo proc.stdout (it may carry values) — only a generic, value-free error.
        print(
            f"env_presence: railway variables failed (env={environment} service={service}, "
            f"exit={proc.returncode}) — check `railway` auth/link",
            file=sys.stderr,
        )
        raise SystemExit(2)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        print("env_presence: could not parse railway --json output", file=sys.stderr)
        raise SystemExit(2) from None


def _railway_value(environment: str, service: str, name: str) -> str | None:
    """Internal-only value read for equality. NEVER printed; returned only to ``_resolve`` which
    collapses it to a comparison boolean."""
    return _railway_raw(environment, service).get(name) or None


def _present_in_env(name: str) -> bool:
    return bool((os.environ.get(name) or "").strip())


def _resolve(spec: str, environment: str | None, service: str | None) -> str | None:
    kind, _, rest = spec.partition(":")
    if kind == "env":
        return os.environ.get(rest) or None
    if kind == "literal":
        return rest or None
    if kind == "railway":
        if not (environment and service):
            print("env_presence: railway:* spec needs --environment and --service", file=sys.stderr)
            raise SystemExit(2)
        return _railway_value(environment, service, rest)
    if kind.startswith("railway@"):
        # Cross-environment spec ``railway@ENV:NAME`` — compare a var ACROSS Railway envs (the
        # dev↔prod parity audit) in one ``equal`` call. Same guarantee as ``railway:``: the value
        # is resolved internally and only the MATCH/MISMATCH bit ever escapes.
        env = kind.split("@", 1)[1]
        if not (env and service):
            print("env_presence: railway@ENV:* spec needs an env in the spec and --service", file=sys.stderr)
            raise SystemExit(2)
        return _railway_value(env, service, rest)
    print(f"env_presence: unknown spec kind {kind!r} (use env:/railway:/railway@ENV:/literal:)", file=sys.stderr)
    raise SystemExit(2)


def _cmd_presence(args: argparse.Namespace) -> int:
    if args.source == "env":
        for name in args.names:
            print(f"{name}: {'set' if _present_in_env(name) else 'unset'}")
        return 0
    # railway
    present = dict(_railway_vars(args.environment, args.service))
    for name in args.names:
        print(f"{name}: {'set' if present.get(name) else 'unset'}")
    return 0


def _cmd_names(args: argparse.Namespace) -> int:
    """Print the sorted variable NAMES declared in a Railway env/service — names only, one per
    line, never a value (weaker than presence: not even the set/unset boolean). Exists so an
    unused-variable audit can enumerate the store without ever running raw ``railway variables``
    (Rule #18)."""
    for name, _present in sorted(_railway_vars(args.environment, args.service)):
        print(name)
    return 0


def _cmd_equal(args: argparse.Namespace) -> int:
    a = _resolve(args.spec_a, args.environment, args.service)
    b = _resolve(args.spec_b, args.environment, args.service)
    if a is None or b is None:
        verdict = "unset"
    else:
        verdict = "MATCH" if a == b else "MISMATCH"
    print(f"{args.label}: {verdict}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Safe env inspection — names→booleans, never values.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("presence", help="print NAME: set|unset for each name")
    p.add_argument("--source", choices=["env", "railway"], default="env")
    p.add_argument("--environment")
    p.add_argument("--service")
    p.add_argument("names", nargs="+")
    p.set_defaults(func=_cmd_presence)

    n = sub.add_parser("names", help="print the sorted declared variable NAMES (railway; never values)")
    n.add_argument("--environment", required=True)
    n.add_argument("--service", required=True)
    n.set_defaults(func=_cmd_names)

    e = sub.add_parser("equal", help="print LABEL: MATCH|MISMATCH|unset for two value specs")
    e.add_argument("label")
    e.add_argument("spec_a", help="env:NAME | railway:NAME | literal:VALUE")
    e.add_argument("spec_b", help="env:NAME | railway:NAME | literal:VALUE")
    e.add_argument("--environment")
    e.add_argument("--service")
    e.set_defaults(func=_cmd_equal)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
