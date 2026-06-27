'use client'

/**
 * VT-406 (Part B) — the signup entity-match sub-step (bilingual EN/HI). Sits AFTER business name +
 * city, BEFORE the OTP step. Flow: fetch UNVERIFIED candidates → owner picks one (or "none of
 * these") → confirm the picked GSTIN → render the verified result (authoritative registry name +
 * a "verified" chip) OR a graceful reject / retry.
 *
 * Provenance discipline (Fazal 2026-06-23): a candidate is "found" (web/GBP, UNCONFIRMED) — it shows
 * a "found" chip, NEVER "verified". Only a Sandbox-confirmed entity (status gstin_verified) shows the
 * "verified" chip + the authoritative name. A web/LLM field is never rendered as verified.
 *
 * The decision logic (fetch sequence, classify, gate) lives in lib/entity-match.ts so it's unit-
 * testable in the node env; this component is the thin bilingual presentation + sub-step transitions.
 */

import { useEffect, useState } from 'react'

import {
  canCreateAccount,
  cityToStateCode,
  classifyConfirm,
  confirmCandidate,
  fetchCandidates,
  fetchGstinsByPan,
  findCinCandidate,
  findNamedNoGstin,
  isConfirmable,
  isValidGstinFormat,
  isValidPanFormat,
  type CinCandidate,
  type EntityCandidate,
  type VerifiedEntity,
  type WizardStep,
} from '@/lib/entity-match'
// VT-448 — PAN identify + the registry-CIN confirm are PARKED behind PAN_IDENTIFY_ENABLED (default
// OFF, Fazal 2026-06-26: Sandbox MCA/PAN are gov-unreliable). With it OFF, MANUAL GSTIN entry is the
// primary identify and the CIN-confirm affordance is not surfaced. The PAN/CIN code stays intact
// behind the flag — flip it back ON when a reliable provider lands.
import { PAN_IDENTIFY_ENABLED, primaryIdentifyStep } from '@/lib/feature-flags'

type Lang = 'en' | 'hi'

type EmMsgKey =
  | 'heading' | 'subhead' | 'looking' | 'found_chip' | 'verified_chip'
  | 'source_web' | 'source_gbp' | 'no_gstin' | 'pick' | 'confirming'
  | 'none_of_these' | 'verified_heading' | 'verified_note' | 'continue'
  | 'reject_heading' | 'reject_body' | 'retry_heading' | 'retry_body' | 'try_again'
  | 'empty_candidates'
  // VT-450 found-company-no-GSTIN keys
  | 'fnog_heading_prefix' | 'fnog_heading_suffix' | 'fnog_hint' | 'fnog_name_label'
  | 'fnog_change_name' | 'fnog_research' | 'fnog_researching' | 'fnog_enter_gstin'
  | 'manual_with_gstin' | 'manual_heading' | 'manual_hint' | 'manual_label'
  | 'manual_placeholder' | 'manual_verify' | 'manual_format_error' | 'manual_not_registered'
  | 'manual_back'
  // VT-448 PAN-identify (PRIMARY) keys
  | 'pan_cta' | 'pan_heading' | 'pan_hint' | 'pan_label' | 'pan_placeholder'
  | 'pan_state_label' | 'pan_state_hint' | 'pan_state_placeholder'
  | 'pan_identify' | 'pan_identifying' | 'pan_format_error' | 'pan_state_error'
  | 'pan_back' | 'pan_pick_heading' | 'pan_pick_hint' | 'pan_pick_this'
  | 'pan_pick_empty' | 'pan_no_pan'
  // VT-449 registry-CIN confirm keys
  | 'cin_heading' | 'cin_prefix' | 'cin_label' | 'cin_confirm' | 'cin_dismiss' | 'cin_confirmed'

const EM_MESSAGES: Record<Lang, Record<EmMsgKey, string>> = {
  en: {
    heading: 'Confirm your business',
    subhead: 'We found these. Pick yours so we work on the right business — or say none match.',
    looking: 'Looking up your business…',
    found_chip: 'Found',
    verified_chip: 'Verified',
    source_web: 'web',
    source_gbp: 'maps',
    no_gstin: 'No GST number found — can’t verify this one.',
    pick: 'This is mine',
    confirming: 'Verifying…',
    none_of_these: 'None of these match',
    verified_heading: 'Verified',
    verified_note: 'We confirmed your GST registration. This is the official registered name.',
    continue: 'Continue',
    // Generic terminus — SAME copy whether the GSTIN was inactive or simply not found (no oracle).
    reject_heading: 'We couldn’t verify a GST registration',
    reject_body: 'Viabe Team is for GST-registered businesses. We couldn’t confirm one for this business, so we can’t create an account right now.',
    retry_heading: 'Couldn’t check right now',
    retry_body: 'This is on our side — the verification service didn’t respond. Please try again in a moment.',
    try_again: 'Try again',
    empty_candidates: 'We couldn’t find your business in public records — you can enter your GST number to verify.',
    // VT-450 — a company WAS found (name) but no GSTIN. Show the found name; offer change-name + enter-GST.
    fnog_heading_prefix: 'We found ',
    fnog_heading_suffix: ' but couldn’t find a GST number for it.',
    fnog_hint: 'Fix the name to search again, or enter your GST number to verify.',
    fnog_name_label: 'Business name',
    fnog_change_name: 'Change company name',
    fnog_research: 'Search again',
    fnog_researching: 'Searching…',
    fnog_enter_gstin: 'Enter my GST number',
    manual_with_gstin: 'Enter my GST number',
    manual_heading: 'Enter your GST number',
    manual_hint: 'We’ll verify it against the official GST registry.',
    manual_label: 'Your 15-character GSTIN',
    manual_placeholder: '22AAAAA0000A1Z5',
    manual_verify: 'Verify',
    manual_format_error: 'That doesn’t look like a valid 15-character GSTIN. Please check and re-enter.',
    manual_not_registered: 'I’m not GST-registered',
    manual_back: 'Back',
    // VT-448 PAN-identify (PRIMARY) — owner enters PAN, we find their GSTIN(s).
    pan_cta: 'Find my GST with PAN',
    pan_heading: 'Enter your PAN',
    pan_hint: 'We’ll look up the GST number(s) registered to it — no typing a 15-character GSTIN.',
    pan_label: 'Your 10-character PAN',
    pan_placeholder: 'ABCDE1234F',
    pan_state_label: 'Your state',
    pan_state_hint: 'We couldn’t tell your state from your city — pick it so we find the right GST.',
    pan_state_placeholder: 'e.g. Maharashtra',
    pan_identify: 'Find my GST',
    pan_identifying: 'Looking up…',
    pan_format_error: 'That doesn’t look like a valid 10-character PAN. Please check and re-enter.',
    pan_state_error: 'We don’t recognise that state yet — please use your GST number instead.',
    pan_back: 'Back',
    pan_pick_heading: 'Pick your GST registration',
    pan_pick_hint: 'These are registered to your PAN. Tap yours to verify it.',
    pan_pick_this: 'This is mine',
    pan_pick_empty: 'We couldn’t find a GST registration for that PAN — you can enter your GST number instead.',
    pan_no_pan: 'Don’t have your PAN? Enter your GST number',
    // VT-449 — registry-CIN confirm. Surfaced on the verified screen when discovery found a company
    // registration. The owner CONFIRMS it's theirs (never auto-captured) → it rides into create.
    cin_heading: 'We also found your company registration',
    cin_prefix: 'Is this your company?',
    cin_label: 'CIN',
    cin_confirm: 'Yes, that’s my company',
    cin_dismiss: 'Not mine',
    cin_confirmed: 'Company registration confirmed.',
  },
  hi: {
    heading: 'अपना व्यवसाय पुष्टि करें',
    subhead: 'हमें ये मिले। अपना चुनें ताकि हम सही व्यवसाय पर काम करें — या बताएं कोई मेल नहीं खाता।',
    looking: 'आपका व्यवसाय खोजा जा रहा है…',
    found_chip: 'मिला',
    verified_chip: 'सत्यापित',
    source_web: 'वेब',
    source_gbp: 'मैप्स',
    no_gstin: 'कोई GST नंबर नहीं मिला — इसे सत्यापित नहीं कर सकते।',
    pick: 'यह मेरा है',
    confirming: 'सत्यापित किया जा रहा है…',
    none_of_these: 'इनमें से कोई मेल नहीं खाता',
    verified_heading: 'सत्यापित',
    verified_note: 'हमने आपका GST पंजीकरण पुष्टि किया। यह आधिकारिक पंजीकृत नाम है।',
    continue: 'जारी रखें',
    reject_heading: 'हम GST पंजीकरण सत्यापित नहीं कर सके',
    reject_body: 'Viabe Team GST-पंजीकृत व्यवसायों के लिए है। हम इस व्यवसाय के लिए इसकी पुष्टि नहीं कर सके, इसलिए अभी खाता नहीं बना सकते।',
    retry_heading: 'अभी जांच नहीं कर सके',
    retry_body: 'यह हमारी ओर से है — सत्यापन सेवा ने जवाब नहीं दिया। कृपया थोड़ी देर में पुनः प्रयास करें।',
    try_again: 'पुनः प्रयास करें',
    empty_candidates: 'हमें सार्वजनिक रिकॉर्ड में आपका व्यवसाय नहीं मिला — आप सत्यापित करने के लिए अपना GST नंबर दर्ज कर सकते हैं।',
    // VT-450 — कंपनी मिली (नाम) पर GST नंबर नहीं। मिला नाम दिखाएं; नाम बदलें + GST दर्ज करें विकल्प दें।
    fnog_heading_prefix: 'हमें ',
    fnog_heading_suffix: ' मिला पर इसका GST नंबर नहीं मिला।',
    fnog_hint: 'फिर से खोजने के लिए नाम ठीक करें, या सत्यापित करने के लिए अपना GST नंबर दर्ज करें।',
    fnog_name_label: 'व्यवसाय का नाम',
    fnog_change_name: 'कंपनी का नाम बदलें',
    fnog_research: 'फिर से खोजें',
    fnog_researching: 'खोजा जा रहा है…',
    fnog_enter_gstin: 'मेरा GST नंबर दर्ज करें',
    manual_with_gstin: 'मेरा GST नंबर दर्ज करें',
    manual_heading: 'अपना GST नंबर दर्ज करें',
    manual_hint: 'हम इसे आधिकारिक GST रजिस्ट्री से सत्यापित करेंगे।',
    manual_label: 'आपका 15-अंकीय GSTIN',
    manual_placeholder: '22AAAAA0000A1Z5',
    manual_verify: 'सत्यापित करें',
    manual_format_error: 'यह एक मान्य 15-अंकीय GSTIN नहीं लगता। कृपया जांचें और पुनः दर्ज करें।',
    manual_not_registered: 'मैं GST-पंजीकृत नहीं हूं',
    manual_back: 'वापस',
    // VT-448 PAN-identify (PRIMARY) — स्वामी PAN दर्ज करते हैं, हम उनका GST नंबर खोजते हैं।
    pan_cta: 'PAN से मेरा GST खोजें',
    pan_heading: 'अपना PAN दर्ज करें',
    pan_hint: 'हम इससे पंजीकृत GST नंबर खोज लेंगे — 15-अंकीय GSTIN टाइप करने की जरूरत नहीं।',
    pan_label: 'आपका 10-अंकीय PAN',
    pan_placeholder: 'ABCDE1234F',
    pan_state_label: 'आपका राज्य',
    pan_state_hint: 'हम आपके शहर से राज्य नहीं पहचान सके — सही GST खोजने के लिए इसे चुनें।',
    pan_state_placeholder: 'जैसे महाराष्ट्र',
    pan_identify: 'मेरा GST खोजें',
    pan_identifying: 'खोजा जा रहा है…',
    pan_format_error: 'यह एक मान्य 10-अंकीय PAN नहीं लगता। कृपया जांचें और पुनः दर्ज करें।',
    pan_state_error: 'हम उस राज्य को अभी नहीं पहचानते — कृपया इसके बजाय अपना GST नंबर उपयोग करें।',
    pan_back: 'वापस',
    pan_pick_heading: 'अपना GST पंजीकरण चुनें',
    pan_pick_hint: 'ये आपके PAN से पंजीकृत हैं। सत्यापित करने के लिए अपना टैप करें।',
    pan_pick_this: 'यह मेरा है',
    pan_pick_empty: 'हमें उस PAN के लिए कोई GST पंजीकरण नहीं मिला — आप इसके बजाय अपना GST नंबर दर्ज कर सकते हैं।',
    pan_no_pan: 'PAN नहीं है? अपना GST नंबर दर्ज करें',
    // VT-449 — रजिस्ट्री-CIN पुष्टि। स्वामी पुष्टि करते हैं कि यह उनका है (कभी स्वतः नहीं) → यह create में जाता है।
    cin_heading: 'हमें आपकी कंपनी का पंजीकरण भी मिला',
    cin_prefix: 'क्या यह आपकी कंपनी है?',
    cin_label: 'CIN',
    cin_confirm: 'हाँ, यह मेरी कंपनी है',
    cin_dismiss: 'मेरी नहीं',
    cin_confirmed: 'कंपनी पंजीकरण की पुष्टि हुई।',
  },
}

export function EntityMatchStep({
  businessName,
  city,
  lang,
  onVerified,
  onReject,
}: {
  businessName: string
  city: string
  lang: Lang
  /** Called when a gstin_verified confirm lands — the verified entity unlocks account-creation. */
  onVerified: (entity: VerifiedEntity) => void
  /** Called on the graceful terminus (none-of-these / not GST-registered). */
  onReject: () => void
}) {
  const t = EM_MESSAGES[lang]
  const [step, setStep] = useState<WizardStep>('idle')
  const [candidates, setCandidates] = useState<EntityCandidate[]>([])
  // VT-450 — the (editable) name we SEARCH on. Seeded from the businessName prop; the found-no-GSTIN
  // state lets the owner correct it and re-run the discovery search (the typed name may be off).
  const [searchName, setSearchName] = useState(businessName)
  const [researching, setResearching] = useState(false) // a change-name re-search is in flight
  // VT-450 — the found-but-no-GSTIN candidate name (e.g. "RKeCom"); null when none / a confirmable hit.
  const [foundNoGstin, setFoundNoGstin] = useState<{ tradeName: string } | null>(null)
  const [confirming, setConfirming] = useState<string | null>(null) // the gstin being confirmed
  const [verified, setVerified] = useState<VerifiedEntity | null>(null)
  const [manualGstin, setManualGstin] = useState('') // VT-448 manual-entry input
  const [manualError, setManualError] = useState(false) // client-side GSTIN format error
  // VT-448 PAN-identify (PRIMARY) state
  const [pan, setPan] = useState('') // owner's 10-char PAN
  const [panState, setPanState] = useState('') // state code, derived from city or owner-picked hint
  const [panError, setPanError] = useState<'format' | 'state' | null>(null) // inline PAN/state error
  const [panLoading, setPanLoading] = useState(false) // PAN→GSTIN lookup in flight
  const [panGstins, setPanGstins] = useState<string[]>([]) // the IDENTIFIED GSTIN(s) for pan_pick
  // VT-449 — the discovered registry CIN candidate (from the fetched candidates) + the owner's
  // confirm/dismiss decision. `cinCandidate` is null when discovery found no registry CIN (no
  // affordance shown). `cinConfirmed` flips only on an explicit owner confirm — NEVER auto-captured;
  // `cinDismissed` hides the affordance when the owner says "not mine".
  const [cinCandidate, setCinCandidate] = useState<CinCandidate | null>(null)
  const [cinConfirmed, setCinConfirmed] = useState<string>('') // the CONFIRMED CIN ('' until confirmed)
  const [cinDismissed, setCinDismissed] = useState(false)
  // The state code derived from the city prop (null when we don't know the city → owner hint needed).
  const derivedStateCode = cityToStateCode(city)

  // Apply a candidates result to state — shared by the mount fetch and the VT-450 change-name
  // re-search. Captures the registry-CIN affordance (VT-449) and routes to the right screen:
  // VT-450 — when discovery returned a company NAME but NO confirmable GSTIN (e.g. RKeCom from GBP),
  // show the found-no-GSTIN state ("We found <name>…" + recover), NOT the "couldn't find" empty-state;
  // that empty-state is reserved for a genuinely ZERO-candidate result.
  function applyCandidates(found: EntityCandidate[]) {
    setCandidates(found)
    // VT-449: capture any discovered registry CIN candidate now (surfaced for owner confirm on the
    // verified screen). null when discovery found none → no CIN affordance, create sends cin: ''.
    // VT-448: when PAN identify is OFF (default) the MCA enrich is parked too — never surface the
    // CIN-confirm affordance, so create always sends cin: '' (the orchestrator's MCA enrich is off).
    setCinCandidate(PAN_IDENTIFY_ENABLED ? findCinCandidate(found) : null)
    const named = findNamedNoGstin(found)
    setFoundNoGstin(named)
    setStep(named ? 'found_no_gstin' : 'picking')
  }

  // Step 1: fetch candidates on mount. The component mounts in 'idle' (the loading screen); the
  // async result flips to 'picking' (or 'found_no_gstin'). Fail-closed → empty list → the picking
  // screen still renders with the not-listed path (lookup never blocks signup). name/city are fixed
  // for the wizard's lifetime (props from the details step), so this runs once — no cascade.
  useEffect(() => {
    let cancelled = false
    fetchCandidates(businessName, city).then((r) => {
      if (cancelled) return
      applyCandidates(r.candidates)
    })
    return () => {
      cancelled = true
    }
  }, [businessName, city])

  // VT-450 — re-run discovery with the EDITED company name (the typed name may be off). Reuses the
  // existing fetchCandidates/applyCandidates spine; on a still-no-GSTIN result the found-no-GSTIN
  // screen simply re-renders with the new name. Fail-closed (empty → the empty-state) like the mount.
  async function rerunSearch() {
    const name = searchName.trim()
    if (!name || researching) return
    setResearching(true)
    try {
      const r = await fetchCandidates(name, city)
      applyCandidates(r.candidates)
    } finally {
      setResearching(false)
    }
  }

  // The shared confirm spine — a GSTIN (from a pick OR manual entry) → Sandbox confirm → classify.
  // The Sandbox confirm is the AUTHORITATIVE gate for both paths (a manually-typed GSTIN is verified
  // exactly like a picked one — it is never self-asserted). `discoveredPhone` (VT-411) rides along on
  // the GBP-pick path only — it's the public business number the ownership step OTPs (null otherwise).
  async function confirmGstin(gstin: string, discoveredPhone: string | null = null) {
    setConfirming(gstin)
    try {
      const outcome = classifyConfirm(await confirmCandidate(gstin), gstin)
      if (outcome.kind === 'verified') {
        // Show the verified result (authoritative name + chip) FIRST; Continue bridges to OTP/create
        // via onVerified. No auto-advance — the owner sees the confirmed entity before proceeding.
        setVerified({ gstin: outcome.gstin, name: outcome.name, phone: discoveredPhone })
        setStep('verified')
      } else if (outcome.kind === 'retry') {
        setStep('retry')
      } else {
        setStep('reject')
        onReject()
      }
    } finally {
      setConfirming(null)
    }
  }

  function pick(candidate: EntityCandidate) {
    const gstin = candidate.candidate_gstin
    if (!gstin) return // GBP-only candidate (no registry id) → the manual-GSTIN path (VT-448), guarded in render
    // VT-411: carry the candidate's discovered public number into the verified entity (GBP only) so
    // the ownership step can OTP it. Manual/PAN paths have no discovered number → ownership asks for it.
    void confirmGstin(gstin, candidate.phone ?? null)
  }

  // VT-448 — the manual-GSTIN path: discovery is thin, OR the owner's only match is a bare/closed GBP
  // listing with no GSTIN (e.g. RKeCom). The owner types their GSTIN; the Sandbox confirm stays the gate.
  function openManual() {
    setManualGstin('')
    setManualError(false)
    setStep('manual_gstin')
  }

  function submitManualGstin() {
    const gstin = manualGstin.trim().toUpperCase()
    if (!isValidGstinFormat(gstin)) {
      setManualError(true) // format typo → inline retry (NOT a reject; the format gate is not an oracle)
      return
    }
    setManualError(false)
    void confirmGstin(gstin)
  }

  function notRegistered() {
    // The honest terminus when the owner has no GSTIN — the SAME generic reject (no enumeration oracle).
    setStep('reject')
    onReject()
  }

  function retry() {
    setStep('picking')
  }

  // VT-450 — the "Back" target from the manual/PAN screens: return to the found-no-GSTIN screen when
  // that's where the owner came from (a found company with no GSTIN), else the normal pick list. This
  // keeps Back from dumping the owner onto the bare pick list after they entered via the found state.
  function backToList() {
    setStep(foundNoGstin ? 'found_no_gstin' : 'picking')
  }

  // VT-448 — the PRIMARY identify entry, gated by PAN_IDENTIFY_ENABLED via primaryIdentifyStep:
  // PAN-entry when PAN identify is ON, MANUAL GSTIN entry when OFF (default). Both the picking-screen
  // primary CTA and any "not listed" path route through this single decision point so the flag has
  // exactly one effect on the primary path.
  function openPrimaryIdentify() {
    if (primaryIdentifyStep() === 'pan_entry') openPanEntry()
    else openManual()
  }

  // VT-448 PRIMARY identify — open the PAN-entry screen. Seed the state code from the city (so the
  // owner usually doesn't even see the state field); when the city is unknown, the screen shows a
  // small state hint and the owner types it.
  function openPanEntry() {
    setPan('')
    setPanState(derivedStateCode ?? '')
    setPanError(null)
    setPanGstins([])
    setStep('pan_entry')
  }

  // Submit the PAN → IDENTIFY the GSTIN(s). Format-gate the PAN first; resolve the state code from
  // the derived city OR (when unknown) the owner's typed state hint via cityToStateCode. On success
  // show pan_pick; the GSTIN(s) here are IDENTIFIED, not verified — the pick round-trips the Sandbox
  // confirm (the sole verify gate). Fail-CLOSED: any lookup failure routes to the manual fallback.
  async function submitPan() {
    const normalized = pan.trim().toUpperCase()
    if (!isValidPanFormat(normalized)) {
      setPanError('format')
      return
    }
    const stateCode = derivedStateCode ?? cityToStateCode(panState)
    if (!stateCode) {
      setPanError('state') // unknown state hint → ask for the GSTIN instead (no guessing a code)
      return
    }
    setPanError(null)
    setPanLoading(true)
    try {
      const r = await fetchGstinsByPan(normalized, stateCode)
      setPanGstins(r.gstins)
      setStep('pan_pick')
    } finally {
      setPanLoading(false)
    }
  }

  const chip = (text: string, tone: 'found' | 'verified') => (
    <span
      className={
        tone === 'verified'
          ? 'rounded-full bg-emerald-100 px-2 py-0.5 text-xs font-semibold text-emerald-700'
          : 'rounded-full bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-700'
      }
    >
      {text}
    </span>
  )

  const card =
    'rounded-2xl border border-gray-200 bg-white p-6 shadow-sm sm:p-8'

  if (step === 'idle') {
    return (
      <section data-entity-step="loading" className={`mt-8 ${card}`}>
        <p className="text-sm leading-relaxed text-gray-600">{t.looking}</p>
      </section>
    )
  }

  if (step === 'verified' && verified) {
    return (
      <section data-entity-step="verified" className={`mt-8 ${card}`}>
        <div className="flex items-center gap-2">
          <h2 className="text-lg font-semibold text-gray-900">{t.verified_heading}</h2>
          {chip(t.verified_chip, 'verified')}
        </div>
        {/* The AUTHORITATIVE registry name — Sandbox, not the candidate's web/LLM name. */}
        <p data-verified-name className="mt-3 text-base font-medium text-gray-900">
          {verified.name ?? businessName}
        </p>
        <p className="mt-2 text-sm leading-relaxed text-gray-600">{t.verified_note}</p>
        {/* VT-449 — registry-CIN confirm. Shown ONLY when discovery surfaced a registry CIN and the
            owner hasn't dismissed it. The owner must CONFIRM it's theirs — we NEVER auto-capture a
            SERP-scraped CIN. On confirm, the CIN rides into create for the MCA-canonical name-match;
            on dismiss (or if none surfaced), create sends cin: '' (name-match falls back to the
            typed business_name). */}
        {cinCandidate && !cinDismissed && (
          <div data-cin-affordance className="mt-4 rounded-xl border border-gray-200 bg-gray-50 p-4">
            <p className="text-sm font-medium text-gray-900">{t.cin_heading}</p>
            {cinCandidate.tradeName && (
              <p className="mt-1 text-sm text-gray-700">{cinCandidate.tradeName}</p>
            )}
            <p className="mt-1 text-xs text-gray-500">
              {t.cin_label} <span data-cin-value className="font-mono tracking-wide text-gray-700">{cinCandidate.cin}</span>
            </p>
            {cinConfirmed ? (
              <p data-cin-confirmed className="mt-2 text-sm font-medium text-emerald-700">
                {t.cin_confirmed}
              </p>
            ) : (
              <>
                <p className="mt-2 text-sm text-gray-600">{t.cin_prefix}</p>
                <div className="mt-3 flex flex-wrap items-center gap-3">
                  <button
                    type="button"
                    data-cin-confirm
                    onClick={() => setCinConfirmed(cinCandidate.cin)}
                    className="rounded-lg bg-emerald-600 px-4 py-2 text-sm font-semibold text-white transition hover:bg-emerald-700"
                  >
                    {t.cin_confirm}
                  </button>
                  <button
                    type="button"
                    data-cin-dismiss
                    onClick={() => setCinDismissed(true)}
                    className="rounded-lg border border-gray-300 px-4 py-2 text-sm font-medium text-gray-700 transition hover:bg-gray-100"
                  >
                    {t.cin_dismiss}
                  </button>
                </div>
              </>
            )}
          </div>
        )}
        <button
          type="button"
          data-entity-continue
          disabled={!canCreateAccount(verified)}
          // VT-449: thread the owner-CONFIRMED CIN (or '' when none confirmed/dismissed) into the
          // verified entity — the create payload sends `cin`. A SERP-scraped CIN never rides unconfirmed.
          onClick={() => onVerified({ ...verified, cin: cinConfirmed })}
          className="mt-5 rounded-xl bg-emerald-600 px-5 py-3 font-semibold text-white shadow-sm transition hover:bg-emerald-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {t.continue}
        </button>
      </section>
    )
  }

  if (step === 'reject') {
    return (
      <section data-entity-step="reject" className={`mt-8 ${card}`}>
        <h2 className="text-lg font-semibold text-gray-900">{t.reject_heading}</h2>
        <p data-reject-body className="mt-3 text-sm leading-relaxed text-gray-600">
          {t.reject_body}
        </p>
      </section>
    )
  }

  if (step === 'retry') {
    return (
      <section data-entity-step="retry" className={`mt-8 ${card}`}>
        <h2 className="text-lg font-semibold text-gray-900">{t.retry_heading}</h2>
        <p data-retry-body className="mt-3 text-sm leading-relaxed text-gray-600">{t.retry_body}</p>
        <button
          type="button"
          data-entity-retry
          onClick={retry}
          className="mt-5 rounded-xl bg-emerald-600 px-5 py-3 font-semibold text-white shadow-sm transition hover:bg-emerald-700"
        >
          {t.try_again}
        </button>
      </section>
    )
  }

  if (step === 'found_no_gstin' && foundNoGstin) {
    // VT-450 — discovery FOUND the company (real returned name) but no candidate carried a GSTIN.
    // We say so honestly ("We found <name> but couldn't find a GST number for it.") and offer BOTH
    // recovery paths: (a) correct the name + re-search, (b) the existing manual-GSTIN verify path.
    // This is NOT the "couldn't find your business" empty-state — we DID find the company.
    return (
      <section data-entity-step="found_no_gstin" className={`mt-8 ${card}`}>
        <h2 className="text-lg font-semibold text-gray-900">
          {t.fnog_heading_prefix}
          <span data-found-name className="font-semibold text-gray-900">{foundNoGstin.tradeName}</span>
          {t.fnog_heading_suffix}
        </h2>
        <p className="mt-2 text-sm leading-relaxed text-gray-600">{t.fnog_hint}</p>
        {/* (a) Change the company name → re-run the discovery search with the edited name. */}
        <label className="mt-4 block text-sm font-medium text-gray-700" htmlFor="fnog-name">
          {t.fnog_name_label}
        </label>
        <input
          id="fnog-name"
          data-found-name-input
          type="text"
          autoComplete="off"
          value={searchName}
          onChange={(e) => setSearchName(e.target.value)}
          className="mt-1 w-full rounded-xl border border-gray-300 px-4 py-3 text-gray-900 outline-none focus:border-emerald-500"
        />
        <div className="mt-5 flex flex-wrap items-center gap-3">
          <button
            type="button"
            data-found-research
            disabled={researching || searchName.trim() === ''}
            onClick={() => void rerunSearch()}
            className="rounded-xl border border-gray-300 px-5 py-2.5 font-medium text-gray-700 transition hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {researching ? t.fnog_researching : t.fnog_research}
          </button>
          {/* (b) Enter my GST number → the existing manual-GSTIN verify path (Sandbox stays the gate). */}
          <button
            type="button"
            data-found-enter-gstin
            disabled={researching}
            onClick={openManual}
            className="rounded-xl bg-emerald-600 px-5 py-3 font-semibold text-white shadow-sm transition hover:bg-emerald-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {t.fnog_enter_gstin}
          </button>
        </div>
      </section>
    )
  }

  if (step === 'manual_gstin') {
    return (
      <section data-entity-step="manual" className={`mt-8 ${card}`}>
        <h2 className="text-lg font-semibold text-gray-900">{t.manual_heading}</h2>
        <p className="mt-1 text-sm leading-relaxed text-gray-600">{t.manual_hint}</p>
        <label className="mt-4 block text-sm font-medium text-gray-700" htmlFor="manual-gstin">
          {t.manual_label}
        </label>
        <input
          id="manual-gstin"
          data-manual-gstin-input
          type="text"
          autoCapitalize="characters"
          autoComplete="off"
          maxLength={15}
          value={manualGstin}
          onChange={(e) => {
            setManualGstin(e.target.value.toUpperCase())
            if (manualError) setManualError(false)
          }}
          placeholder={t.manual_placeholder}
          className="mt-1 w-full rounded-xl border border-gray-300 px-4 py-3 font-mono uppercase tracking-wide text-gray-900 outline-none focus:border-emerald-500"
        />
        {manualError && (
          <p data-manual-error className="mt-2 text-sm text-red-600">{t.manual_format_error}</p>
        )}
        <div className="mt-5 flex flex-wrap items-center gap-3">
          <button
            type="button"
            data-manual-verify
            disabled={confirming !== null || manualGstin.trim() === ''}
            onClick={submitManualGstin}
            className="rounded-xl bg-emerald-600 px-5 py-3 font-semibold text-white shadow-sm transition hover:bg-emerald-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {confirming !== null ? t.confirming : t.manual_verify}
          </button>
          <button
            type="button"
            data-manual-back
            disabled={confirming !== null}
            onClick={backToList}
            className="rounded-xl border border-gray-300 px-5 py-2.5 font-medium text-gray-700 transition hover:bg-gray-50 disabled:opacity-50"
          >
            {t.manual_back}
          </button>
        </div>
        <button
          type="button"
          data-manual-not-registered
          disabled={confirming !== null}
          onClick={notRegistered}
          className="mt-4 block text-sm text-gray-500 underline underline-offset-2 transition hover:text-gray-700 disabled:opacity-50"
        >
          {t.manual_not_registered}
        </button>
      </section>
    )
  }

  if (step === 'pan_entry') {
    // When the city resolved to a state code we hide the state field entirely (the common path —
    // owner just enters their PAN). Only when the city is unknown do we surface the state hint.
    const needsStateHint = derivedStateCode === null
    return (
      <section data-entity-step="pan_entry" className={`mt-8 ${card}`}>
        <h2 className="text-lg font-semibold text-gray-900">{t.pan_heading}</h2>
        <p className="mt-1 text-sm leading-relaxed text-gray-600">{t.pan_hint}</p>
        <label className="mt-4 block text-sm font-medium text-gray-700" htmlFor="pan-input">
          {t.pan_label}
        </label>
        <input
          id="pan-input"
          data-pan-input
          type="text"
          autoCapitalize="characters"
          autoComplete="off"
          maxLength={10}
          value={pan}
          onChange={(e) => {
            setPan(e.target.value.toUpperCase())
            if (panError) setPanError(null)
          }}
          placeholder={t.pan_placeholder}
          className="mt-1 w-full rounded-xl border border-gray-300 px-4 py-3 font-mono uppercase tracking-wide text-gray-900 outline-none focus:border-emerald-500"
        />
        {panError === 'format' && (
          <p data-pan-error className="mt-2 text-sm text-red-600">{t.pan_format_error}</p>
        )}
        {needsStateHint && (
          <>
            <label className="mt-4 block text-sm font-medium text-gray-700" htmlFor="pan-state">
              {t.pan_state_label}
            </label>
            <p className="mt-1 text-xs leading-relaxed text-gray-500">{t.pan_state_hint}</p>
            <input
              id="pan-state"
              data-pan-state-input
              type="text"
              autoComplete="off"
              value={panState}
              onChange={(e) => {
                setPanState(e.target.value)
                if (panError === 'state') setPanError(null)
              }}
              placeholder={t.pan_state_placeholder}
              className="mt-1 w-full rounded-xl border border-gray-300 px-4 py-3 text-gray-900 outline-none focus:border-emerald-500"
            />
          </>
        )}
        {panError === 'state' && (
          <p data-pan-state-error className="mt-2 text-sm text-red-600">{t.pan_state_error}</p>
        )}
        <div className="mt-5 flex flex-wrap items-center gap-3">
          <button
            type="button"
            data-pan-identify
            disabled={panLoading || pan.trim() === ''}
            onClick={() => void submitPan()}
            className="rounded-xl bg-emerald-600 px-5 py-3 font-semibold text-white shadow-sm transition hover:bg-emerald-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {panLoading ? t.pan_identifying : t.pan_identify}
          </button>
          <button
            type="button"
            data-pan-back
            disabled={panLoading}
            onClick={() => setStep('picking')}
            className="rounded-xl border border-gray-300 px-5 py-2.5 font-medium text-gray-700 transition hover:bg-gray-50 disabled:opacity-50"
          >
            {t.pan_back}
          </button>
        </div>
        {/* FALLBACK: don't have your PAN? → the manual 15-char GSTIN path. */}
        <button
          type="button"
          data-pan-no-pan
          disabled={panLoading}
          onClick={openManual}
          className="mt-4 block text-sm text-gray-500 underline underline-offset-2 transition hover:text-gray-700 disabled:opacity-50"
        >
          {t.pan_no_pan}
        </button>
      </section>
    )
  }

  if (step === 'pan_pick') {
    return (
      <section data-entity-step="pan_pick" className={`mt-8 ${card}`}>
        <h2 className="text-lg font-semibold text-gray-900">{t.pan_pick_heading}</h2>
        <p className="mt-1 text-sm leading-relaxed text-gray-600">{t.pan_pick_hint}</p>
        {panGstins.length === 0 ? (
          <p data-pan-pick-empty className="mt-4 text-sm leading-relaxed text-gray-600">
            {t.pan_pick_empty}
          </p>
        ) : (
          <ul className="mt-4 flex flex-col gap-3">
            {panGstins.map((g) => (
              <li
                key={g}
                data-pan-gstin
                className="flex flex-col gap-2 rounded-xl border border-gray-200 p-4 sm:flex-row sm:items-center sm:justify-between"
              >
                {/* IDENTIFIED, not verified — the pick round-trips the Sandbox confirm. */}
                <span className="font-mono text-sm tracking-wide text-gray-900">{g}</span>
                <button
                  type="button"
                  data-pan-pick
                  disabled={confirming !== null}
                  onClick={() => void confirmGstin(g)}
                  className="self-start rounded-lg bg-emerald-600 px-4 py-2 text-sm font-semibold text-white transition hover:bg-emerald-700 disabled:cursor-not-allowed disabled:opacity-50 sm:self-auto"
                >
                  {confirming === g ? t.confirming : t.pan_pick_this}
                </button>
              </li>
            ))}
          </ul>
        )}
        <div className="mt-5 flex flex-wrap items-center gap-3">
          <button
            type="button"
            data-pan-pick-retry
            disabled={confirming !== null}
            onClick={openPanEntry}
            className="rounded-xl border border-gray-300 px-5 py-2.5 font-medium text-gray-700 transition hover:bg-gray-50 disabled:opacity-50"
          >
            {t.pan_back}
          </button>
          {/* FALLBACK: enter the GSTIN directly. */}
          <button
            type="button"
            data-pan-pick-manual
            disabled={confirming !== null}
            onClick={openManual}
            className="rounded-xl border border-gray-300 px-5 py-2.5 font-medium text-gray-700 transition hover:bg-gray-50 disabled:opacity-50"
          >
            {t.manual_with_gstin}
          </button>
        </div>
      </section>
    )
  }

  // step === 'picking'
  // VT-449: registry (CIN-only) candidates are NOT GST-verify picks — they're surfaced as the
  // CIN-confirm affordance on the verified screen. Show only web/GBP rows in the pick list (a
  // registry row has no GSTIN and would otherwise render a confusing "no GST number" line here).
  const pickable = candidates.filter((c) => c.source !== 'registry')
  return (
    <section data-entity-step="picking" className={`mt-8 ${card}`}>
      <h2 className="text-lg font-semibold text-gray-900">{t.heading}</h2>
      <p className="mt-1 text-sm leading-relaxed text-gray-600">{t.subhead}</p>
      {pickable.length === 0 ? (
        <p data-entity-empty className="mt-4 text-sm leading-relaxed text-gray-600">
          {t.empty_candidates}
        </p>
      ) : (
        <ul className="mt-4 flex flex-col gap-3">
          {pickable.map((c, i) => {
            const confirmable = isConfirmable(c)
            const display = c.trade_name || c.legal_name || businessName
            return (
              <li
                key={`${c.candidate_gstin ?? c.trade_name ?? 'c'}-${i}`}
                data-candidate
                data-source={c.source}
                className="flex flex-col gap-2 rounded-xl border border-gray-200 p-4"
              >
                <div className="flex items-center gap-2">
                  <span className="font-medium text-gray-900">{display}</span>
                  {/* Provenance: web/GBP candidates are FOUND, never verified. */}
                  {chip(t.found_chip, 'found')}
                  <span className="text-xs text-gray-400">
                    {c.source === 'gbp' ? t.source_gbp : t.source_web}
                  </span>
                </div>
                {c.legal_name && c.legal_name !== display && (
                  <span className="text-sm text-gray-600">{c.legal_name}</span>
                )}
                {c.detail && <span className="text-xs text-gray-500">{c.detail}</span>}
                {confirmable ? (
                  <button
                    type="button"
                    data-candidate-pick
                    disabled={confirming !== null}
                    onClick={() => pick(c)}
                    className="mt-1 self-start rounded-lg bg-emerald-600 px-4 py-2 text-sm font-semibold text-white transition hover:bg-emerald-700 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {confirming === c.candidate_gstin ? t.confirming : t.pick}
                  </button>
                ) : (
                  <span data-no-gstin className="mt-1 text-xs text-gray-400">{t.no_gstin}</span>
                )}
              </li>
            )
          })}
        </ul>
      )}
      {/* VT-448: identify-and-confirm. With PAN identify ON, PRIMARY = find-my-GST-with-PAN (owner
          enters their PAN, we identify the GSTIN(s) → pick → verify) + a manual-GSTIN fallback.
          With PAN identify OFF (default, Fazal 2026-06-26 — Sandbox MCA/PAN unreliable), the PAN
          path is not offered at all and MANUAL GSTIN entry is the PRIMARY identify (emerald CTA).
          "not listed / found-but-no-GSTIN" is never a dead end either way. */}
      <div className="mt-5 flex flex-wrap items-center gap-3">
        {PAN_IDENTIFY_ENABLED ? (
          <>
            <button
              type="button"
              data-entity-pan
              disabled={confirming !== null}
              onClick={openPrimaryIdentify}
              className="rounded-xl bg-emerald-600 px-5 py-3 font-semibold text-white shadow-sm transition hover:bg-emerald-700 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {t.pan_cta}
            </button>
            <button
              type="button"
              data-entity-manual
              disabled={confirming !== null}
              onClick={openManual}
              className="rounded-xl border border-gray-300 px-5 py-2.5 font-medium text-gray-700 transition hover:bg-gray-50 disabled:opacity-50"
            >
              {t.manual_with_gstin}
            </button>
          </>
        ) : (
          // PAN identify OFF — manual GSTIN entry is the PRIMARY identify (no PAN affordance shown).
          // Routed through openPrimaryIdentify (→ manual via primaryIdentifyStep) — the single gate.
          <button
            type="button"
            data-entity-manual
            disabled={confirming !== null}
            onClick={openPrimaryIdentify}
            className="rounded-xl bg-emerald-600 px-5 py-3 font-semibold text-white shadow-sm transition hover:bg-emerald-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {t.manual_with_gstin}
          </button>
        )}
      </div>
    </section>
  )
}
