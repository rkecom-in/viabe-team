# Documentation hierarchy (VT-119)

The canonical-document map for Viabe **Team**. Every canonical doc carries a 📖 source-of-truth
banner (what it IS / is NOT authoritative for, where it sits, update protocol). This page is the
index of that hierarchy (Pillar 8: one source of truth per topic, explicit hierarchy — no shadow
docs).

## Authority tree (Team)

```
PRODUCT + PILLARS  (highest level)
├── concept-team.md ............... product strategy + Phase-1 scope     [authority doc; not in this repo*]
└── concept-team-pillars.md ....... the 8 inviolable Pillars             [authority doc; not in this repo*]
        │  (Pillar changes = Type-3 board governance)
        ▼
ENGINEERING
└── docs/Viabe_Team_Technical_Reference_v1_0.md ... architecture · schema · contracts (the *what*)
        ├── docs/adr/ ............ decision rationale (the *why*; immutable; cite CL)   [under the Reference]
        └── docs/runbooks/ ....... operational procedures (incident response)          [parallel to the Reference]

GOVERNANCE / OPERATING
├── docs/clau/decisions-ledger.md . every Standing decision (CL-N)
├── docs/clau/operating-brief.md .. the four-role model + sequencing
├── .viabe/sprint/VT-*.md ......... the task board (source of truth; Notion is archive)
└── CLAUDE.md ..................... session bootstrap + disciplines
```

\* `concept-team.md` + `concept-team-pillars.md` are the product/Pillars authority but live in the
shared concept space, not this repo — so they cannot be bannered from here; the engineering docs
redirect to them by name. If they are ever vendored into this repo, add the 📖 banner then.

## Cross-product perspective (Team vs Reports)

| Topic | Team (this repo) | Reports (sibling repo) |
|---|---|---|
| Engineering reference | `docs/Viabe_Team_Technical_Reference_v1_0.md` | `Viabe_AI_Technical_Reference_v2_0.md` |
| Pipeline/architecture | the Technical Reference + `docs/adr/` | `viabe_pipeline_intelligence_architecture_v1.md` |
| Pillars | `concept-team-pillars.md` (Team-specific) | Reports' own (its `CLAUDE.md`, if produced) |

The two products have SEPARATE engineering references — Team's docs are authoritative for Team,
Reports' for Reports. The cross-product addendum banners on the **Reports** docs (clarifying Team
has its own equivalents) are Clau's to add in the Reports repo (out of scope here — flagged for
the Reports co-maintainer).
