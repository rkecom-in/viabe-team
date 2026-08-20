"""VT-757 — the declared capability surface, rendered for the triage classifier.

THE DEFECT THIS SERVES. An owner asked *"can you record and send a voice note in Tamil to my lapsed
customers instead of a text reminder"* and the Manager replied *"Got it — I'm on it and I'll update
you shortly."* Nothing followed, and nothing could: customer sends go out as approved WhatsApp
templates with text positionals, and there is no audio path. The ack fired because the request was
DISPATCHED as an async task before anything established the work was possible — `_COMPLETED_NO_REPLY_
FALLBACK` is downstream of dispatch, so by the time the impossibility could be discovered the promise
was already sent.

WHY THIS IS NOT A BLOCKLIST. The set of things an owner might ask for is unbounded, and the standing
no-lists rule (Fazal 2026-07-15) forbids enumerating natural-language intent. The honest primitive is
the INVERSE: a declared, closed set of what the Manager can actually cause to happen. That set already
exists — `CAPABILITY_REGISTRY` — and this module only renders it. Nothing here decides anything; the
classifier reads the surface and judges the ask against it.

THE TRAP THIS AVOIDS, stated because it is the way a naive fix goes wrong. **Voice notes ARE
supported — in the other direction.** VT-59 shipped owner→us voice-note ingestion. A check keyed on
the words "voice note" therefore finds a real, shipped capability and concludes the ask is
supportable. What separates them is DIRECTION and AUDIENCE, which is exactly what a summary sentence
carries and a keyword does not: *"Send an owner-approved win-back message to a lapsed-customer
cohort"* says who is sending to whom. Rendering summaries rather than names is the point.

`disabled` capabilities are rendered SEPARATELY and deliberately. They are the cases where the
product has already decided the answer is "no, and here is what I offer instead" (D2 — GST return
filing, paid ad boosts). Folding them in with the live ones would tell the classifier we can do
things we have decided not to do.
"""

from __future__ import annotations

__all__ = ["render_capability_surface"]


def render_capability_surface() -> str:
    """The capability surface as prompt text: what the Manager CAN cause, and what it explicitly
    cannot.

    Generated from the registry on every call rather than pasted into the prompt file, so a
    capability added, retired or flipped to ``disabled`` cannot leave the classifier believing a
    stale surface — the drift that would make this check confidently wrong.
    """
    from orchestrator.capability.registry import CAPABILITY_REGISTRY

    can: list[str] = []
    cannot: list[str] = []
    for spec in CAPABILITY_REGISTRY.values():
        line = f"- {spec.summary}"
        (cannot if spec.mode == "disabled" else can).append(line)

    parts = ["## What the Manager can actually cause to happen", *sorted(can)]
    if cannot:
        parts += [
            "",
            "## Explicitly NOT supported (decided, not missing — offer the stated alternative)",
            *sorted(cannot),
        ]
    return "\n".join(parts)
