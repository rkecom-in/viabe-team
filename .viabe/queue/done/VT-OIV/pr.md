---
task: VT-OIV
pr_url: https://github.com/rkecom-in/viabe-team/pull/53
branch: test/vt-oiv-owner-inputs-verification
opened_at: 2026-05-24T18:05:00+05:30
---

PR #53 opened against `main`.

3-line summary:

- Adds 3 behavioural tests (extraction → row shape + no-body sinks + Composer read-path; classifier-failure resilience; DSR purge per-feature pin) + 1 env-gated real-Anthropic canary mirroring `test_sales_recovery_end_to_end.py`.
- Fixes the retention-comment citation in `migrations/020_owner_inputs.sql` to authoritative page `368387c2-cc5a-8162`.
- SHIP-GATE constant stays `False` on `main`; flag-flip is Fazal's separate post-merge action. Canary requires `RUN_INTEGRATION_TESTS=1 + ANTHROPIC_API_KEY + DATABASE_URL` and CI skips it by design — manual-run command is in the PR description.
