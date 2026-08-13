# CODEX BRIEF — primary-source verification of the four India-SMB playbooks

**Paste this to Codex AFTER CC merges PR #553 (finish-before-start: VT-723 must be fully landed
first). Paste the CONTENT of the four kb files along with it** — Codex's clone does not contain
`/Users/fazalkhan/kb`, and it must verify the text as it actually stands, not from memory:
`01_india_smb_gst_working_capital_and_cash_flow_manual.md` ·
`02_india_d2c_logistics_rto_and_cod_economics_playbook.md` ·
`03_india_whatsapp_conversational_commerce_and_festive_marketing.md` ·
`04_kirana_tiffin_and_local_retail_operational_playbook.md`

---

## ⬇️ PASTE FROM HERE ⬇️

You are verifying a small knowledge base of India-SMB operational facts that an AI business manager
will state to real Indian shop owners as ground truth. **Every number it gets wrong will be said,
confidently, to a real business.** Your job is adversarial: assume each claim is wrong until a
primary source confirms it.

## The task

For **every** rate, fee, date, threshold, and legal rule in the four documents provided:

1. **Find the PRIMARY source** — the authority that sets the fact, not a blog that reports it:
   - GST law/procedure → CBIC (cbic.gov.in, taxinformation.cbic.gov.in), the CGST Act text, the
     specific Notification number
   - WhatsApp/Meta pricing and policy → Meta's own developer documentation ONLY
     (developers.facebook.com / business.whatsapp.com)
   - Courier rates → the carrier's or aggregator's OWN published rate card (shiprocket.in/pricing,
     delhivery.com), not comparison blogs
   - UPI hardware → Paytm/PhonePe's own business pages
2. **Verdict per claim:** `CONFIRMED` (primary source, cite it + access date) · `CORRECTED` (state
   the right value + source) · `UNVERIFIABLE-PRIMARY` (only secondary sources exist — say which, and
   mark the claim for a caveat) · `STALE-RISK` (true now, known change coming — state the date).
3. **Priority #1 — the 1 October 2026 change to WhatsApp's free customer-service window.** The
   current text rests on BSP blogs. Find Meta's own announcement: exactly WHAT becomes chargeable
   on 1 Oct (which message types, in which window), at what rate, and whether utility-in-window is
   affected. Pricing decisions are being made against this fact. If Meta's documentation is
   ambiguous or the change is not confirmed by Meta directly, SAY SO — that itself is the finding.
4. **Also flag:** any claim with no date · any claim whose number you cannot reproduce from the
   cited source · any internal contradiction between the four documents.

## Output — one file, `INDIA-PLAYBOOK-VERIFICATION.md`

- A verdict table: `# | document | claim (verbatim) | verdict | primary source (URL) | access date | corrected value if any`
- A short section: **the three most consequential corrections** (the ones that would most change
  what the manager tells an owner)
- A short section: **claims that CANNOT be primary-verified** and what caveat each needs
- No rewriting of the playbooks themselves — verification report only; edits are applied downstream
  after review.

## Rules
Web research only. No repo access needed beyond the pasted text. No code. If a source is paywalled
or geo-blocked, record that rather than substituting a secondary source silently. Do not trust the
documents' own citations — re-resolve every one.

## ⬆️ END PASTE ⬆️
