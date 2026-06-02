# Consent-text registry — `consent_text_version` → copy (VT-8.5)

Single source of truth for the consent copy a customer agrees to when they
scan a business QR and opt in. The `record_of_consent` table (migration 067)
stores **only the version string** in `consent_text_version`; the actual copy
+ locale text live here — exactly the `template_name → SID` pattern of
`.viabe/templates.md`.

**DRAFT — NOT legally validated.** Copy below is placeholder. Cowork drafts the
real text; Fazal / counsel legal-validates (controller = **RKeCom Services OPC
Pvt Ltd**) before any version is marked `status: live`. Build ships with the
placeholder version strings so the machinery is exercisable; no live version
exists until legal sign-off.

DPDP basis: customer QR opt-in = explicit consent (distinct from the
owner_inputs basis CL-425 that covers owner-entered customers). Sub-processors
the customer's data may reach must be named in the live copy
(Anthropic / Sarvam / Twilio / Voyage / Supabase / Apify; CL-416 retention).

---

## Versions

| version | status | locale | effective_from | notes |
|---|---|---|---|---|
| `qr_consent_v0_draft_en` | draft | en | — | placeholder English; not legally validated |
| `qr_consent_v0_draft_hi` | draft | hi | — | placeholder Hindi; not legally validated |

### `qr_consent_v0_draft_en` (DRAFT — placeholder)

> _[PLACEHOLDER — Cowork to draft, Fazal/counsel to validate.]_
> By sharing your number you agree to receive messages from this business on
> WhatsApp. Your number is stored securely and never sold. Reply STOP / use the
> opt-out link any time to withdraw. Controller: RKeCom Services OPC Pvt Ltd.

### `qr_consent_v0_draft_hi` (DRAFT — placeholder)

> _[PLACEHOLDER — Hindi translation pending Cowork draft + legal validation.]_

---

## How it's used

- Capture endpoint (`/api/orchestrator/consent/capture`) receives the
  `consent_text_version` the page rendered; it is persisted verbatim as the
  proof of which copy the customer agreed to.
- Re-consent UPSERTs the latest version onto the same row (and clears any
  opt-out — Fix 1).
- When a new live version is published, bump the version string (do NOT edit a
  shipped version's copy in place — that would falsify historical consent
  proof). Add a new row above and flip `status`.
