# PARKED (Fazal 2026-07-07): Founder-journey simulator — pick up AFTER the VT-611 gate objective

Fazal's ask (verbatim intent): script + run a few real-like founder journeys — different business
types + regions join Viabe-Team, integrate, gauge Viabe-Team's outcome over a fast-forwarded virtual
month (~1hr). Record + measure team-manager AND individual specialist performance. Present the full
founder journey — gains, losses, benefits, outcome — in the most easily understandable way, so we can
figure out what went wrong and where. Status: **HELD** — "you complete the objective and we will then
bring this up later." Do NOT start until Fazal reopens; finish VT-611 first.

## Two reframes already surfaced to Fazal (carry these — don't rebuild revenue-theater)
1. **Outcome can't be REAL.** Viabe acts (onboard/plan/send); whether a customer returns + spends is
   external. No real customers on dev (VT-231 blocks it). Any revenue number is a SIMULATED market WE
   author — only as true as our assumptions. What IS real + rigorously measurable: Viabe's BEHAVIOR
   (onboard cleanliness, integration, intent understanding, plan quality, correct delegation, safety/
   no-unapproved-send, communication, no-re-ask, use-of-real-context). Outcome must be a LABELED model,
   never presented as real earnings — else it's the exact false-proof theater the VT-611 gate exists to kill.
2. **"Virtual month" = a month WE script.** No virtual clock in the system. Simulating a month = author
   the events (lapses, campaign responses, owner touchpoints, time-advanced states) + drive compressed.
   Tests Viabe's RESPONSES to an authored month, not autonomous month-long living.

## Proposed design (agreed direction, not yet built)
Founder-journey simulator:
- **Personas**: N founders = business type × region × language × integration, each with a realistic
  business state + customer base + a HIDDEN "market-truth" response model (how their customers react).
- **Journey driver**: signup → onboard → integrate (seed their data) → a scripted "month" of owner
  touchpoints + time-advanced customer states + Viabe's autonomous actions, on DEPLOYED dev.
- **Measurement**: (REAL) per-turn manager + specialist behavior quality (judge dims + DB-state + safety);
  (MODELED) business outcome — customers recovered / revenue / ROI — via the transparent market model.
- **Report (the centerpiece)**: per-founder VISUAL journey — timeline of what happened, Viabe's decisions,
  modeled outcome, and **"where Viabe FAILED this founder, and why"** (dumb moments, re-asks, mis-routes,
  missed opportunities). That diagnostic is the real deliverable.

## 3 open decisions (Fazal wanted to clarify these first — resolve when reopened)
1. Outcome framing: real-behavior + 3-band range (pess/real/opt) [recommended] · single transparent model · behavior-only.
2. Founder set: 3 diverse [recommended: Delhi kirana/Hinglish/Sheet, Chennai sweets-D2C/Tamil/Shopify,
   Pune salon/Marathi/manual] · 5 · 1-deep-first.
3. Run timing: after the gate run [no dev interference] · prioritize-sim-first · concurrent [rate-limit risk].
Fazal signaled he has clarifications/additions to these before we lock them — ASK what he wants to clarify when reopening.

## Relationship to VT-611
Complementary, different lens. VT-611 gate = manager is safe + intelligent on SCRIPTED turns. This sim =
Viabe delivers value across a founder's WHOLE journey + surfaces where the experience breaks. Reuses the
same infra (convo_harness --ingress-url, transcript_judge, the H1 DB-state asserts, deployed dev).
