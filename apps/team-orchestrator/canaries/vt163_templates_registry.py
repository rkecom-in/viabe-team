"""Canary: templates_registry structural integrity (VT-163 / Rule #15).

Loads the real on-disk twilio_templates.yaml via templates_registry.canary_load()
and verifies:
  - Every template entry has a non-empty variables list.
  - Every declared language variant has a content_sid matching ^HX[0-9a-f]{32}$
    (null/None SIDs are accepted as pending-approval stubs).
  - No malformed entries exist.

Fail-not-skip: any violation raises TemplateRegistryError and exits non-zero.
This is a structural config-load canary (CL-274 two-mode: fast structural in CI).
No external Twilio API call — that belongs to VT-45's live-send canary.

CL-390: no SID values in log output; template_name + language only.
CL-422: no customer data; config only.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s  %(name)s  %(message)s",
)
logger = logging.getLogger("vt163.canary")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_YAML_PATH = _REPO_ROOT / "apps" / "team-orchestrator" / "config" / "twilio_templates.yaml"


def _ensure_path_on_sys_path() -> None:
    """Ensure apps/team-orchestrator/src is importable (for standalone runs)."""
    src = str(_REPO_ROOT / "apps" / "team-orchestrator" / "src")
    if src not in sys.path:
        sys.path.insert(0, src)


def main() -> None:
    _ensure_path_on_sys_path()

    from orchestrator.templates_registry import (
        TemplateRegistryError,
        approved_template_names,
        canary_load,
        resolve,
    )

    logger.info("vt163 canary: loading %s", _YAML_PATH)

    # --- Step 1: structural integrity check ---
    try:
        canary_load(_YAML_PATH)
    except TemplateRegistryError as exc:
        logger.error("vt163 canary FAILED (structural): %s", exc)
        sys.exit(1)

    logger.info("vt163 canary: structural check PASSED")

    # --- Step 2: spot-check expected SID shapes for all 9 templates ---
    expected_names = [
        "team_welcome",
        "team_weekly_approval",
        "team_opt_out_confirmation",
        "team_dsr_acknowledgment",
        "team_agent_stuck_escalation",
        "team_status_ping",
        "team_unable_to_complete_request",
        "team_error_handler",
        "team_monthly_report",  # VT-163-fix-2 (9th; system-invoked by VT-86)
    ]
    import re
    sid_re = re.compile(r"^HX[0-9a-f]{32}$")
    failures: list[str] = []

    for name in expected_names:
        try:
            entry = resolve(name, "en", _path=_YAML_PATH)
        except TemplateRegistryError as exc:
            failures.append(f"  [{name}][en] resolve failed: {exc}")
            continue

        if entry.content_sid is not None and not sid_re.match(entry.content_sid):
            failures.append(
                f"  [{name}][en] content_sid does not match ^HX[0-9a-f]{{32}}$"
            )
        if not entry.variables:
            failures.append(f"  [{name}] variables list is empty")

        # CL-390: log template_name + language only, never the SID value.
        logger.info(
            "vt163 canary: %s[en] variables=%s agent_selectable=%s",
            name,
            list(entry.variables),
            entry.agent_selectable,
        )

    if failures:
        logger.error("vt163 canary FAILED (spot-check):\n%s", "\n".join(failures))
        sys.exit(1)

    # --- Step 3: approved_template_names ("en") ---
    approved = approved_template_names("en", _path=_YAML_PATH)
    logger.info("vt163 canary: approved_template_names(en) = %s", list(approved))
    if "team_weekly_approval" not in approved:
        logger.error(
            "vt163 canary FAILED: team_weekly_approval not in approved_template_names(en)"
        )
        sys.exit(1)

    logger.info("vt163 canary: ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
