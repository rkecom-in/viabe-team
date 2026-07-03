---
note: follow-up for VT-576 scope (found live 2026-07-03)
---
The owner_inputs (VT-303 data-processing consent) gate fired MID-TASK, post-onboarding — the owner
was changing a store link and got interrupted by "Reply ACTIVATE TEAM". Correct rail, wrong moment.
Fold the consent grant into the paced flow's READINESS beat ("want me to set up your data
connections?" — yes = the natural consent moment; record owner_inputs there with the same audit
trail the enable handler writes). The keyword path stays as fallback.
