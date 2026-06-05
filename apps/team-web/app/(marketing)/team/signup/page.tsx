/**
 * VT-96 — owner signup form (bilingual EN/HI). Consumes VT-82 POST /api/signup
 * via the /api/team/signup proxy; business_type options from /api/team/business-types
 * (the orchestrator taxonomy — single source of truth).
 *
 * NEEDS-FAZAL / public-exposure: this form must NOT be linked/deployed publicly until
 * VT-326 (OTP-before-create + per-IP throttle) lands — the backend front door is
 * un-throttled with no proof-of-control of the number (flooding + squatting). The
 * page structures a phone field that the later OTP step hooks before submit.
 *
 * CL-390: NO PII (name / phone / city) in any analytics/telemetry event.
 */
'use client'

import { useEffect, useState } from 'react'

type Lang = 'en' | 'hi'
type BizType = { key: string; label_en: string; label_hi: string }
type MsgKey =
  | 'title' | 'business_name' | 'owner_name' | 'whatsapp_number' | 'city'
  | 'business_type' | 'language' | 'consent_dpdpa' | 'consent_residency'
  | 'submit' | 'invalid_phone' | 'required' | 'duplicate' | 'generic' | 'success'

const MESSAGES: Record<Lang, Record<MsgKey, string>> = {
  en: {
    title: 'Sign up for Viabe Team',
    business_name: 'Business name',
    owner_name: 'Your name',
    whatsapp_number: 'WhatsApp number (+91…)',
    city: 'City',
    business_type: 'Business type',
    language: 'Language',
    consent_dpdpa: 'I agree to the data-processing notice (DPDP).',
    consent_residency: 'I agree to data being stored in India.',
    submit: 'Create my account',
    invalid_phone: 'Enter a valid +91 mobile number.',
    required: 'Please fill all fields and accept both consents.',
    duplicate: 'This number is already registered.',
    generic: 'Something went wrong. Please try again.',
    success: 'Account created — check WhatsApp for your welcome message.',
  },
  hi: {
    title: 'Viabe Team के लिए साइन अप करें',
    business_name: 'व्यवसाय का नाम',
    owner_name: 'आपका नाम',
    whatsapp_number: 'WhatsApp नंबर (+91…)',
    city: 'शहर',
    business_type: 'व्यवसाय का प्रकार',
    language: 'भाषा',
    consent_dpdpa: 'मैं डेटा-प्रोसेसिंग सूचना (DPDP) से सहमत हूँ।',
    consent_residency: 'मैं डेटा भारत में संग्रहीत होने से सहमत हूँ।',
    submit: 'मेरा खाता बनाएं',
    invalid_phone: 'एक मान्य +91 मोबाइल नंबर दर्ज करें।',
    required: 'कृपया सभी फ़ील्ड भरें और दोनों सहमतियाँ स्वीकार करें।',
    duplicate: 'यह नंबर पहले से पंजीकृत है।',
    generic: 'कुछ गलत हुआ। कृपया पुनः प्रयास करें।',
    success: 'खाता बन गया — स्वागत संदेश के लिए WhatsApp देखें।',
  },
}

const PHONE_RE = /^\+91[6-9]\d{9}$/

export default function SignupPage() {
  const [lang, setLang] = useState<Lang>('en')
  const [bizTypes, setBizTypes] = useState<BizType[]>([])
  const [form, setForm] = useState({
    business_name: '',
    owner_name: '',
    whatsapp_number: '',
    city: '',
    business_type: '',
    consent_dpdpa: false,
    consent_residency: false,
  })
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const t = MESSAGES[lang]

  useEffect(() => {
    fetch('/api/team/business-types')
      .then((r) => r.json())
      .then((d) => setBizTypes(d.business_types ?? []))
      .catch(() => setBizTypes([]))
  }, [])

  function update<K extends keyof typeof form>(k: K, v: (typeof form)[K]) {
    setForm((f) => ({ ...f, [k]: v }))
  }

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    if (
      !form.business_name ||
      !form.owner_name ||
      !form.city ||
      !form.business_type ||
      !form.consent_dpdpa ||
      !form.consent_residency
    ) {
      setError(t.required)
      return
    }
    if (!PHONE_RE.test(form.whatsapp_number)) {
      setError(t.invalid_phone)
      return
    }
    setSubmitting(true)
    try {
      const res = await fetch('/api/team/signup', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ ...form, preferred_language: lang }),
      })
      if (res.status === 201) {
        setDone(true)
        return
      }
      const body = await res.json().catch(() => ({}))
      const code = body?.detail?.code
      setError(code === 'duplicate' ? t.duplicate : t.generic)
    } catch {
      setError(t.generic)
    } finally {
      setSubmitting(false)
    }
  }

  if (done) {
    return (
      <main className="signup-success">
        <p>{t.success}</p>
      </main>
    )
  }

  return (
    <main className="signup">
      <div className="signup-lang">
        <button type="button" onClick={() => setLang('en')} aria-pressed={lang === 'en'}>
          English
        </button>
        <button type="button" onClick={() => setLang('hi')} aria-pressed={lang === 'hi'}>
          हिंदी
        </button>
      </div>
      <h1>{t.title}</h1>
      <form onSubmit={onSubmit}>
        <label>
          {t.business_name}
          <input
            value={form.business_name}
            onChange={(e) => update('business_name', e.target.value)}
            maxLength={200}
            required
          />
        </label>
        <label>
          {t.owner_name}
          <input
            value={form.owner_name}
            onChange={(e) => update('owner_name', e.target.value)}
            maxLength={120}
            required
          />
        </label>
        <label>
          {t.whatsapp_number}
          <input
            value={form.whatsapp_number}
            onChange={(e) => update('whatsapp_number', e.target.value)}
            placeholder="+919876543210"
            inputMode="tel"
            required
          />
        </label>
        <label>
          {t.city}
          <input
            value={form.city}
            onChange={(e) => update('city', e.target.value)}
            maxLength={120}
            required
          />
        </label>
        <label>
          {t.business_type}
          <select
            value={form.business_type}
            onChange={(e) => update('business_type', e.target.value)}
            required
          >
            <option value="" disabled>
              —
            </option>
            {bizTypes.map((b) => (
              <option key={b.key} value={b.key}>
                {lang === 'hi' ? b.label_hi : b.label_en}
              </option>
            ))}
          </select>
        </label>
        <label className="signup-consent">
          <input
            type="checkbox"
            checked={form.consent_dpdpa}
            onChange={(e) => update('consent_dpdpa', e.target.checked)}
          />
          {t.consent_dpdpa}
          {/* NEEDS-FAZAL: link to the DPDP disclosure copy (dpdpa_v1_2026-06). */}
        </label>
        <label className="signup-consent">
          <input
            type="checkbox"
            checked={form.consent_residency}
            onChange={(e) => update('consent_residency', e.target.checked)}
          />
          {t.consent_residency}
          {/* NEEDS-FAZAL: link to the residency disclosure copy (residency_v1_2026-06). */}
        </label>
        {error && <p className="signup-error" role="alert">{error}</p>}
        <button type="submit" disabled={submitting}>
          {t.submit}
        </button>
      </form>
    </main>
  )
}
