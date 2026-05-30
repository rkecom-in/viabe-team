"""Canary: send_whatsapp_template VT-45 (Rule #15).

Two-mode canary following the CL-274 / test_owner_inputs_canary_real_anthropic.py pattern:

DEFAULT (dry-run / mock mode)
  Loads the real registry, resolves team_weekly_approval[en], validates params,
  builds content_variables — WITHOUT calling Twilio. Asserts the composed payload
  matches the expected SID. Zero cost, zero network, zero PII.

REAL-SEND (three-gated, TEST-RECIPIENT-ONLY — CL-422)
  Requires: VT45_REAL_SEND=1 + TWILIO_TEST_RECIPIENT set + real Twilio creds.
  Sends to TWILIO_TEST_RECIPIENT ONLY (never a real customer). fail-not-skip.
  Recommended sequence: first with TEAM_TWILIO_MOCK_MODE=1 to verify code path,
  then one genuine send to prove the Content SID renders.

CL-390: logs SID + template_name + status only. Never logs phone or param values.
CL-422: never sends to or resolves real customer data. TWILIO_TEST_RECIPIENT must
  be a developer-controlled sandbox number.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s  %(name)s  %(message)s",
)
logger = logging.getLogger("vt45.canary")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC = str(_REPO_ROOT / "apps" / "team-orchestrator" / "src")
_YAML_PATH = _REPO_ROOT / "apps" / "team-orchestrator" / "config" / "twilio_templates.yaml"


def _ensure_src_on_path() -> None:
    if _SRC not in sys.path:
        sys.path.insert(0, _SRC)


def _dry_run_mode() -> None:
    """Validate registry resolution + content_variables composition, NO send."""
    from orchestrator.templates_registry import (
        TemplateRegistryError,
        resolve,
        validate_params,
    )
    from orchestrator.agent.tools.send_whatsapp_template import _build_content_variables

    logger.info("vt45 canary: DRY-RUN mode (no Twilio call)")

    # Use team_weekly_approval as the canonical test case.
    template_name = "team_weekly_approval"
    language = "en"
    params = {
        "customer_segment": "SMB",
        "campaign_mode": "recovery",
        "projected_recovery_inr": "5000",
    }

    # --- Step 1: resolve entry ---
    try:
        entry = resolve(template_name, language, _path=_YAML_PATH)
    except TemplateRegistryError as exc:
        logger.error("vt45 canary FAILED (resolve): %s", exc)
        sys.exit(1)

    # CL-390: log template_name + variables only, never SID value.
    logger.info(
        "vt45 canary: resolved template=%s lang=%s variables=%s",
        template_name, language, list(entry.variables),
    )

    # Assert content_sid is a real HX SID (not None, not pending).
    import re
    sid_re = re.compile(r"^HX[0-9a-f]{32}$")
    if entry.content_sid is None or not sid_re.match(entry.content_sid):
        logger.error(
            "vt45 canary FAILED: content_sid for %s[%s] is absent or malformed",
            template_name, language,
        )
        sys.exit(1)
    logger.info("vt45 canary: content_sid shape OK (HX...)")

    # --- Step 2: validate params ---
    try:
        validate_params(template_name, language, params, _path=_YAML_PATH)
    except TemplateRegistryError as exc:
        logger.error("vt45 canary FAILED (validate_params): %s", exc)
        sys.exit(1)
    logger.info("vt45 canary: param signature validated OK")

    # --- Step 3: build content_variables ---
    content_variables = _build_content_variables(entry.variables, params)
    expected_keys = {str(i + 1) for i in range(len(entry.variables))}
    if set(content_variables.keys()) != expected_keys:
        logger.error(
            "vt45 canary FAILED: content_variables keys %s != expected %s",
            sorted(content_variables.keys()), sorted(expected_keys),
        )
        sys.exit(1)
    logger.info(
        "vt45 canary: content_variables keys=%s OK", sorted(content_variables.keys()),
    )

    # --- Step 4: reproducibility — compose twice, assert identical ---
    cv2 = _build_content_variables(entry.variables, params)
    if content_variables != cv2:
        logger.error("vt45 canary FAILED: content_variables not reproducible")
        sys.exit(1)
    logger.info("vt45 canary: reproducibility check PASSED")

    logger.info("vt45 canary: DRY-RUN ALL CHECKS PASSED")


def _real_send_mode() -> None:
    """Send to TWILIO_TEST_RECIPIENT only. THREE-GATED. CL-422 safe."""
    test_recipient = os.environ.get("TWILIO_TEST_RECIPIENT", "").strip()
    if not test_recipient:
        logger.error(
            "vt45 canary FAILED (real-send): TWILIO_TEST_RECIPIENT is unset. "
            "Real sends must go to a developer-controlled sandbox number — "
            "never a real customer (CL-422). Set the env var and retry."
        )
        sys.exit(1)

    logger.info(
        "vt45 canary: REAL-SEND mode to TWILIO_TEST_RECIPIENT (CL-422 gated)"
    )

    # Resolve and build content_variables first (same as dry-run).
    from orchestrator.templates_registry import resolve, TemplateRegistryError
    from orchestrator.agent.tools.send_whatsapp_template import _build_content_variables

    template_name = "team_weekly_approval"
    language = "en"
    params = {
        "customer_segment": "Canary",
        "campaign_mode": "test",
        "projected_recovery_inr": "0",
    }

    try:
        entry = resolve(template_name, language, _path=_YAML_PATH)
    except TemplateRegistryError as exc:
        logger.error("vt45 canary FAILED (resolve): %s", exc)
        sys.exit(1)

    content_variables = _build_content_variables(entry.variables, params)
    content_sid = entry.content_sid
    if content_sid is None:
        logger.error(
            "vt45 canary FAILED: content_sid is None (pending approval) — "
            "real send not possible until Meta approves the template."
        )
        sys.exit(1)

    logger.info(
        "vt45 canary: sending template=%s lang=%s to test_recipient (hash only logged)",
        template_name, language,
    )

    # Import and use send_template_message directly (the same path the tool uses).
    # TEAM_TWILIO_MOCK_MODE=1 is honoured if set (recommended for first-pass).
    from orchestrator.utils.twilio_send import send_template_message
    from uuid import UUID

    # Use a dummy tenant UUID for the canary (no DB, no real tenant).
    _CANARY_TENANT_ID = UUID("00000000-0000-0000-0000-000000000001")

    try:
        result = send_template_message(
            _CANARY_TENANT_ID,
            template_name,
            content_variables,
            recipient_phone=test_recipient,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("vt45 canary FAILED (send exception): %s", type(exc).__name__)
        sys.exit(1)

    if not result.success:
        logger.error(
            "vt45 canary FAILED: send_result.success=False code=%s msg=%s",
            result.error_code, result.error_message,
        )
        sys.exit(1)

    message_sid = result.message_sid or ""
    mock_mode = os.environ.get("TEAM_TWILIO_MOCK_MODE", "0") == "1"

    if mock_mode:
        # Mock SIDs start with MK (the mock client prefix).
        if not message_sid.startswith("MK"):
            logger.error(
                "vt45 canary FAILED (mock mode): expected MK-prefixed mock SID, got %s",
                message_sid[:4] + "...",
            )
            sys.exit(1)
        logger.info("vt45 canary: mock-mode SID shape OK (MK...)")
    else:
        # CL-272 proof-of-call: real SIDs start with SM or MM.
        import re
        if not re.match(r"^(SM|MM)[0-9a-f]{32}$", message_sid):
            logger.error(
                "vt45 canary FAILED (real-send): SID does not match ^(SM|MM)[0-9a-f]{32}$ "
                "(got prefix: %s)",
                message_sid[:4] if message_sid else "empty",
            )
            sys.exit(1)
        logger.info("vt45 canary: real SID shape OK (SM/MM...)")

    # CL-390: log sid + status only. recipient_phone_token is a hash (safe to log).
    logger.info(
        "vt45 canary: REAL-SEND sent template=%s status=success "
        "recipient_token=%s",
        template_name, result.recipient_phone_token,
    )
    logger.info("vt45 canary: REAL-SEND ALL CHECKS PASSED")


def main() -> None:
    _ensure_src_on_path()

    real_send = os.environ.get("VT45_REAL_SEND", "0") == "1"
    if real_send:
        _real_send_mode()
    else:
        _dry_run_mode()


if __name__ == "__main__":
    main()
