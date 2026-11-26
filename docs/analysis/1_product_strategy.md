# Sparrow AI: product strategy and business model review

Source tags: [repo] file in /home/user/bbgInControl; [web] third-party page, not Meta's own rate card, treat as unverified; [assumption] my estimate, no data behind it.

## 1. Value proposition and ICP

**HIGH. The product sells the first reply and the booking, but the agent's stated pain is listing scarcity, and there is no inbound source.** PRD §2 names captación as the top concern. Independent agents have few inbound owner leads; the Lystos adapter is not built and the founder is not buying the API [repo: PRODUCT_CONTEXT "Open items"]. So volume depends on cold CASAFARI, the riskiest module. Instead: ship a consented inbound source first, a hosted valuation form and a click-to-WhatsApp link/QR (owner writes first, consent captured). Evidence: PRD §11 item 4 (own form listed as "later"); CLAUDE.md G1.

**MED. Buyer, user and decider are one autónomo: right for a pilot, wrong for scale.** RE/MAX Spain: ~164 offices, ~2,316 agents at end 2024 [web: emprendedores.es, inmodiario]. At EUR 100-150/agent/month, 100% of the network is a ~EUR 3-4M/year ceiling; 5% is ~EUR 150-200k. Instead: pilot 5-10 agents in 2-3 offices; the scale path is selling to a franchise owner (10-15 agents per deal). Evidence: PRD §4 treats the director only as "next segment".

**MED. Defensibility is thin.** Lystos already sells a proactive WhatsApp AI for private sellers, from the agent's own number if configured, at EUR 0.50 per conversation; its help article mentions no calendar booking [web: help.lystos.com]. The gap is one feature, and Lystos is also the planned lead source. Real moat candidates: calendar booking plus guardrails, per-meeting pricing, a network endorsement. Evidence: PRD §9 last row.

## 2. Pricing and unit economics

Assumptions: Meta Spain marketing ~EUR 0.0585, utility ~EUR 0.0166 [web: chatmaxima, agenciareinicia]; LLM EUR 2/M input, EUR 10/M output [assumption, deployment price unknown].

| | Inbound (owner asked) | Cold (CASAFARI) |
|---|---|---|
| Lead cost to Sparrow | 0 | EUR 0.08-0.27 per lead (below) |
| AI per booked meeting | ~EUR 0.5-1.5 incl. non-booking chats | ~EUR 1-3 |
| Meta (agent pays) | ~EUR 0.06-0.18 per lead (first message and both follow-ups are MARKETING) | EUR 0.06 per lead |
| Booking rate [assumption] | ~10% of contacted | 1-3% of contacted |
| Sparrow margin at EUR 30 | ~95% | ~+EUR 10 to +EUR 25 per meeting at 2% |

LLM load from caps: Spanish prompt ~11k chars (~3k tokens) per call, up to 6 calls per message, 20 turns/lead/day, 60/week [repo: config.py, llm.py]. A typical booked chat ~70k input tokens; worst case at the caps ~EUR 4-5 per lead. Caps are adequate.

**MED. The prospecting cost in the docs is a best case.** "EUR 0.04 per usable lead" comes from one backlog page. Code pulls every 3 h but stops once the daily cap is filled (`room <= 0`, prospecting.py). At cap 3-5 one page (EUR 0.40) fills the cap: ~EUR 0.08-0.13/lead. At cap 10 in a zone with ~6 new usable/day (Zaragoza test), up to 4 pages/day = EUR 1.60 for ~6 leads: ~EUR 0.27/lead, ~EUR 48/month per agent. Instead: one pull per day by default; pass-through or absorb the CASAFARI per-agent key (EUR 49/month Agent plan) explicitly.

**HIGH. Revenue per agent is small.** 10 inbound leads/month at ~25% booked = EUR 75; cold ~3.6 meetings/month = EUR 108 minus up to EUR 48 data cost. A 10-agent pilot is EUR 500-1,000 MRR: enough to prove the loop, not a business yet. A valuation meeting is worth thousands in expected commission [assumption]. Instead: test EUR 30 against EUR 50-60 for owner-requested meetings; keep EUR 30 as the pilot hook.

**HIGH. "Booked" is not a verifiable billing event.** BL-1/BL-3 undecided; no `cancelled_at`; a chat-cancelled meeting stays `booked` and "still counts" [repo: engine.do_handoff]; an agent deleting the event is noticed only when a reminder job runs; calendar is `primary` only [repo: gcal.py], so an agent on another calendar gets clashes and can hide meetings; the agent can take over (G8) and book by phone. Instead: billable = owner-confirmed meeting not cancelled by T-2h; next day WhatsApp the agent "¿Se hizo? Sí / No-show / Cancelada", billable by default after 72h silence, no-show credit with a cap; card on file charged monthly (chasing EUR 30 invoices from autónomos costs more than the fee).

**MED. Meta card friction (error 131042) is a funnel step.** "No setup fee" is true; "zero friction" is not. Instead: count agents lost at the card step and pre-check it before any demo. Evidence: DEMO_REPORT.

## 3. Funnel end to end

**HIGH. The 30-second promise holds for ~43% of the week.** Sending is 09:00-21:00 Mon-Sat: 72 of 168 hours [repo: guards.py]. An owner who requests a valuation at 22:00 or on Sunday waits up to ~36 h, so the headline claim fails for most arrival times. Instead: for owner-requested leads use 08:00-22:00 seven days; keep 09-21 Mon-Sat for cold only.

**HIGH. Nothing after "booked", no proof of value.** The agent is alerted only when a lead needs them; no booking summary (CLAUDE.md pending 5); no weekly report; no held/signed capture. Without it Sparrow cannot bill, show ROI, or give the director case studies (PRD §9 "Unproven funnel"). Instead: pull forward a weekly agent WhatsApp/email (contacted, replied, booked, owed) plus the day-after prompt above. This is a billing and trust loop, not a dashboard.

**MED. Follow-up stops after two MARKETING templates (+1 and +3 days)** [repo: engine.job_first_contact]. Sellers who decide in 3-12 months are the real captación pool; no reschedule or no-show re-engagement exists (handoff only). Instead: one in-chat reschedule attempt before handoff; one 30-day re-touch for warm leads.

**MED. Template 1 says "Vi tu anuncio en {{4}}" and "valoración gratuita"** [repo: templates.py]. Wrong for form/QR leads, and the founder owes the "free" decision (DEMO_REPORT §e). Instead: a second first-contact template for requested leads.

**MED. Account health is not visible**: one account had templates missing and a failed job (DEMO_REPORT). Instead: an admin readiness check per account (Google, Meta, card, templates approved) before any lead is accepted.

## 4. Prospecting module

**HIGH. Cold is the volume engine, with the weakest conversion; do it second.** Spanish owners who sell privately often say no to agencies [assumption, unsourced], so reply and booking rates will trail inbound. Supply is limited: one zone, ~6 usable/day in a mid-size city, France unusable (1/25 with a phone), CASAFARI key-sharing unresolved. Agent-writes-the-message is right for liability but wrong for results: untested agent copy varies widely. Instead: 2-3 pre-tested variants as defaults, tested centrally; edits are the exception.

**MED. Auto-pause is sound but slow.** The opt-out rule needs 10+ leads, 2-3 weeks at 3-5/day; the quality rating is the early signal. Add the yield rule from PRD §10 (fewer than 3 meetings in 300 contacts) in code, not as a promise.

## 5. Competition and director objections

GoHighLevel-style builders: anyone can wire WhatsApp, AI and a calendar in days; Sparrow wins on onboarding and Coexistence know-how only. Portal/Spanish CRMs own the leads and can add AI chat cheaply. Lystos is the closest threat. Generic bots lack calendar and a Spanish seller script.

A RE/MAX director would object to: RE/MAX name in agent-written cold messages; who is data controller (office or agent) and data going to Azure/Railway US; leads living in the agent's number when an agent leaves; no office visibility; AI talking to owners without their quality control. Answer: team plan plus office report (PRD §11 item 1).

## 6. Roadmap sanity

Pull forward: `cancelled_at` plus monthly summary plus day-after prompt (one piece); consented inbound intake; owner-request hours; recorded terms click-accept; readiness check. Defer or drop: looped Meta clips (pending 6), working hours from Google (pending 3), Lystos adapter until the payload is known, 12-month agent cleanup (no one reaches it before month 12; keep the note), prospecting inside onboarding. Over-built for 5-20 agents: cold guardrails, ramp, dedupe and three-language screens before one real inbound meeting exists. Under-built: lead source, billing, proof.

## 7. Pilot design

Pre-register: contacts, median first-reply time and share inside 5 min, reply rate, qualified, booked, held (agent-confirmed), valuations, listings signed, opt-out rate, number quality, cost per meeting, day-30 willingness to pay. Stop signals [assumption]: booked/contacted under 10% inbound, held/booked under 60%. What kills it: no lead source (agents with under 10 owner leads/month), Meta card or template friction before go-live, a quality drop or number ban, one rude AI chat seen by an agent, billing disputes. Run inbound 30 days first, then cold in one zone with one agent.
