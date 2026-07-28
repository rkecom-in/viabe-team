# VT-710 O8 corpus conversion report

Generated deterministically from `executional_scenarios.jsonl` on 2026-07-27.

- Input cards: **118**
- Unique source-rights records: **104**
- Rights statuses: `{'live_link_only': 5, 'permission_granted': 3, 'unknown': 96}`
- Card statuses: `{'candidate': 103, 'research_only': 15}`
- Embedding states: `{'pending': 7, 'rights_blocked': 111}`
- Retrieval-eligible cards: **0**

The rights inventory was completed before conversion began. Public accessibility was not treated
as a licence. Third-party sources without an explicit grant remain `unknown`; the five audited
unarchived sources remain `live_link_only`. Only RKECOM-authored local synthesis has
`permission_granted`, and its embedding remains deferred because VT-710 has no egress authority.
All cards remain candidate/research-only and are consumed by no live route.

Claim keys use the audited primary topic plus normalized hard-gate mechanism, jurisdiction,
population and channel. They are deterministic candidate keys, not a human finding that two cards
are comparable; domain review remains mandatory before validation/admission.
