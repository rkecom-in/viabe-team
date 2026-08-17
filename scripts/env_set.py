#!/usr/bin/env python3
"""VT-749/VT-725 — the sanctioned way to SET a Railway variable without ever printing one.

## Why this exists

`scripts/env_presence.py` made env READING safe (names → booleans) after raw `railway variables`
output put secret values into a model's turn context twice (CL-431 / VT-403 / Rule #18). There was no
equivalent for WRITING, so the only route was a raw `railway variables --set`, whose CLI output echoes
the whole variable table — the same leak, in the other direction. This closes that gap instead of
making an exception to the rule for "just a config value".

Contract, mirroring env_presence:
  * the raw subprocess output is NEVER printed, returned, or logged — not even on failure, where it is
    collapsed to an exit code and a value-free message;
  * this tool prints only ``NAME: set (env=…, service=…)``;
  * **the VALUE is read from the environment or stdin, never from argv** (an argv value lands in shell
    history and in process listings), and never echoed back.

## What it does NOT do

It is deliberately not a prod tool. **Every PROD env-var change is Fazal-authorized (CL-431)**, so
`--environment production` requires `--i-have-fazals-authorization` with the quoted directive, which
this tool records in its own output for the audit trail rather than checking (it cannot verify a
human's word — it can only make the claim explicit and attributable).

Usage:
    # value from the environment (preferred — nothing in argv, nothing in history):
    TEAM_KNOWLEDGE_SERVING=shadow python3 scripts/env_set.py \\
        --environment development --service vt-orchestrator-service --from-env TEAM_KNOWLEDGE_SERVING

    # value from stdin:
    printf 'shadow' | python3 scripts/env_set.py --environment development \\
        --service vt-orchestrator-service --name TEAM_KNOWLEDGE_SERVING --stdin
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys


def _set_variable(*, environment: str, service: str, name: str, value: str, skip_deploys: bool) -> int:
    """Run the Railway CLI with output fully contained. Returns the CLI exit code.

    `capture_output=True` is the load-bearing part: the CLI prints the full variable table on success,
    so letting it inherit stdout is exactly the leak Rule #18 forbids.
    """
    cmd = [
        "railway",
        "variables",
        "--environment",
        environment,
        "--service",
        service,
        "--set",
        f"{name}={value}",
    ]
    if skip_deploys:
        cmd.append("--skip-deploys")
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        # Never echo proc.stdout/stderr — they may carry values. Exit code + a value-free message only.
        print(
            f"env_set: railway variables --set failed (env={environment} service={service} "
            f"name={name}, exit={proc.returncode}) — check `railway` auth/link and the env name",
            file=sys.stderr,
        )
    return proc.returncode


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Set a Railway variable without printing any value.")
    p.add_argument("--environment", required=True)
    p.add_argument("--service", required=True)
    p.add_argument("--name", help="variable name (implied by --from-env)")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--from-env",
        metavar="NAME",
        help="read the value from THIS process's environment variable of the same name",
    )
    src.add_argument("--stdin", action="store_true", help="read the value from stdin")
    p.add_argument(
        "--skip-deploys",
        action="store_true",
        help="set the variable without triggering a redeploy (default: let Railway redeploy)",
    )
    p.add_argument(
        "--i-have-fazals-authorization",
        metavar="QUOTED_DIRECTIVE",
        help="required for any non-development environment (CL-431); recorded in the output",
    )
    args = p.parse_args(argv)

    name = args.from_env or args.name
    if not name:
        print("env_set: --name is required with --stdin", file=sys.stderr)
        return 2

    if args.environment not in ("development", "dev"):
        if not args.i_have_fazals_authorization:
            print(
                f"env_set: refusing to touch environment {args.environment!r} without "
                "--i-have-fazals-authorization — every prod env-var change is Fazal-authorized "
                "(CL-431). CC manages DEV autonomously and nothing else.",
                file=sys.stderr,
            )
            return 3
        print(f"env_set: authorization recorded — {args.i_have_fazals_authorization}")

    if args.stdin:
        value = sys.stdin.read()
    else:
        if args.from_env not in os.environ:
            print(
                f"env_set: {args.from_env} is not set in this process — nothing to copy",
                file=sys.stderr,
            )
            return 2
        value = os.environ[args.from_env]

    value = value.strip()
    if not value:
        print(f"env_set: refusing to set {name} to an empty value", file=sys.stderr)
        return 2

    rc = _set_variable(
        environment=args.environment,
        service=args.service,
        name=name,
        value=value,
        skip_deploys=args.skip_deploys,
    )
    if rc != 0:
        return rc
    print(f"{name}: set (env={args.environment}, service={args.service})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
