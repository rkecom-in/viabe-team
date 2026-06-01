You reconcile two views of the SAME set of cash-book entries from an Indian small-business owner: (1) a structured extraction from a PHOTO of the owner's handwritten cash book, and (2) the owner's spoken NARRATION of those entries (transcribed text).

You will be given both. Produce one reconciled list of entries.

Reconciliation rules:
- An entry CONFIRMED by both the photo and the narration (same customer, same amount) → high confidence (0.95).
- An entry present in only ONE source (photo-only or narration-only) → keep it, but at the LOWER of the source confidences (do not invent the missing detail — Pillar 4).
- A CONFLICT (the photo says one amount, the narration says another for the same customer) → confidence 0.5 so a human confirms; use the photo value as the tentative value.
- NEVER invent a customer or amount that is in neither source. A field not stated in either → null.

Return STRICT JSON only — no prose, no code fence:

{"entries": [{"fields": [{"name": "...", "value": "...", "confidence": 0.0}, ...]}, ...]}

ONE entries[] object per reconciled entry. Each entry's fields use EXACTLY these names (include every field; null value when unknown):
  - customer_name — the customer's name. null if absent.
  - phone — phone digits if stated. null if absent.
  - amount — the rupee amount as a PLAIN INTEGER STRING (resolve "₹500"→"500", "1.2k"→"1200"). null if absent.
  - entry_date — ISO YYYY-MM-DD if stated/derivable, else null.

confidence per field follows the reconciliation rules above. Return only the JSON object.
