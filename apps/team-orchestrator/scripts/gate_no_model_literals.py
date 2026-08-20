#!/usr/bin/env python3
"""VT-732 gate — no model chosen outside the env, anywhere.

Fails the build when a model id is hardcoded outside the model seam, or when an LLM slot is
re-added to ``config/models.yaml``. Both were real: on 2026-08-05 every ``TEAM_MODEL_*`` var on dev
read ``gpt-5.6-luna`` while the bill said Sonnet, because ~30 call sites built their own Anthropic
client (and ``config/models.yaml`` kept its own VIABE_ENV-slotted pins that the tier seam never
saw). Configuration that the code can bypass is not governance, so this is the tripwire that keeps
the fix from decaying one convenient literal at a time.

Run: ``python scripts/gate_no_model_literals.py`` (exit 1 on a violation).

ALLOWED (the seam + the deliberate exceptions):
  * ``src/orchestrator/llm/provider.py``   — the registry + tier defaults; THE place ids live.
  * ``src/orchestrator/llm/pricing.py``    — the multi-provider price table (keyed BY model id).
  * ``src/orchestrator/agent/cost.py``     — the Anthropic paise rate table (keyed BY model id).
  * ``src/orchestrator/agent/dispatch.py`` — ``_BRAIN_MODEL_*`` express TIER INTENT and resolve
    through the env; they are the innocent path, on Clau's do-not-fix list.
  * ``src/orchestrator/advice_eval.py``    — the Mac-side offline judge, deliberately Claude and
    deliberately cross-family from the product (Fazal 2026-07-13).

NOT a model literal: ``twilio_send`` / ``dev_send_guard`` / ``send_whatsapp_*`` call
``messages.create`` on the TWILIO client. Pattern-matching that method name breaks WhatsApp
delivery, not model governance — so this gate matches MODEL IDS, never method names.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
_MODELS_YAML = _ROOT / "config" / "models.yaml"

# A quoted model id of any supported family.
_MODEL_LITERAL = re.compile(
    r"""["'](claude-[a-z0-9.\-]+|gpt-5[a-z0-9.\-]*|gemini-[a-z0-9.\-]+|glm-[a-z0-9.\-]+|grok-[a-z0-9.\-]+)["']"""
)

_ALLOWED_FILES = {
    "orchestrator/llm/provider.py",
    "orchestrator/llm/pricing.py",
    "orchestrator/agent/cost.py",
    "orchestrator/agent/dispatch.py",
    "orchestrator/advice_eval.py",
}

# A models.yaml key whose value is an LLM slot. Only non-Claude vendor pins may live there now
# (Sarvam ASR ids are not chat models and are resolved by their own client).
_YAML_ALLOWED_KEYS = {"voice_transcription"}


def _scan_sources() -> list[str]:
    violations: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        if rel in _ALLOWED_FILES:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue  # a comment naming a model is documentation, not a choice
            match = _MODEL_LITERAL.search(line)
            if match:
                violations.append(
                    f"{rel}:{lineno}: hardcoded model id {match.group(1)!r} — resolve the tier "
                    f"instead (resolve_chat_model / resolve_model_id / structured_text_call)"
                )
    return violations


def _scan_models_yaml() -> list[str]:
    if not _MODELS_YAML.exists():
        return []
    violations: list[str] = []
    for lineno, line in enumerate(_MODELS_YAML.read_text(encoding="utf-8").splitlines(), start=1):
        if line.startswith("#") or not line.strip():
            continue
        if line.rstrip().endswith(":") and not line.startswith(" "):
            key = line.rstrip()[:-1]
            if key not in _YAML_ALLOWED_KEYS:
                violations.append(
                    f"config/models.yaml:{lineno}: LLM slot {key!r} re-added — model selection "
                    f"belongs to the TEAM_MODEL_* env tiers (VT-732)"
                )
    return violations


def main() -> int:
    violations = _scan_sources() + _scan_models_yaml()
    if violations:
        print("VT-732 model-governance gate FAILED:\n")
        for v in violations:
            print(f"  {v}")
        print(
            "\nEvery model choice comes from a TEAM_MODEL_* tier var. If a call site genuinely "
            "needs one vendor, route it through the seam and document the reason in-line."
        )
        return 1
    print("VT-732 model-governance gate: clean (no model ids outside the seam).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
