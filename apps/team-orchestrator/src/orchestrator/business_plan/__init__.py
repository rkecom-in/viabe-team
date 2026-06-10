"""VT-368 Gap-4 — the business-plan SPINE (summary + 6-month roadmap).

Generated proactively on onboarding-journey completion; Gap-5 specialist agents execute against the
roadmap items; Gap-6 VTR edits them (each edit = a new immutable version). Submodules: ``store``
(versioned persistence — the contract), ``schema`` (the JSON contract + citation validator +
degrade template), ``generator`` (grounding + Sonnet + the DBOS workflow), ``delivery`` (paced
bilingual WhatsApp), ``seams`` (the Gap-5 consume + Gap-6 edit contracts). Gap-5/6 import
``store``/``seams`` only — never the LLM generator.
"""
