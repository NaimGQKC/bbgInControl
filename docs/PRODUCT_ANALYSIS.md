# Sparrow AI — end-to-end product and technical analysis

*26 Nov 2026. Five independent reviews (product, agent UX, owner conversation, technical, platform/legal) merged into one decision register. Detail and evidence per lens: `docs/analysis/1..5`. Real-model rehearsals: `docs/rehearsal/`. Legal points are research, not legal advice.*

## 1. Bottom line

1. **The skeleton is right.** One gate for every message, code (not the AI) owns state, hard stops in code, AI discloses itself, per-agent Coexistence number, Postgres job queue. Do not rebuild any of it.
2. **The product has three holes that matter more than any bug:**
   - **No consented inbound lead source.** Agents' pain is getting listings (captación). The Lystos adapter is not built, so volume would come from cold CASAFARI, the riskiest module. Build inbound first.
   - **"Booked meeting" cannot be billed yet.** No cancel state, no "did it happen?", no proof of value for the agent.
   - **Cold WhatsApp prospecting is, per the research, against Meta's own opt-in policy and probably LSSI art. 21 / EDPB.** Treat it as "blocked until a lawyer says yes", not "built, so ship".
3. **The first real send failed for a business reason, not a code reason** (Meta error 131042, no payment method on the agent's WhatsApp account). The onboarding *tells* the agent to add a card but never *checks*. This will hit every agent. Add a check and a recovery step.
4. **Technically it is a good pilot system and not yet a paid-customer system:** duplicate-send and lost-webhook windows, a transient AI error that permanently mutes a lead, no migrations, no CI, nobody told when production breaks.
5. **Conversation quality is "safe but flat".** It gives the owner no reason to say yes, dodges the two questions every owner asks (price, commission), and has three real bugs (can't offer Saturday, opt-out fires on "no me llames por la mañana", promises it can't keep).

**Verdict for the demo:** blocked only by the Meta payment method plus a second phone. For a first pilot: fix the "Before the pilot" list (section 5), about 3-4 working days.

## 2. The end-to-end flow, stage by stage

| Stage | What happens | Status | Main problem |
|---|---|---|---|
| Lead source | Lystos (not built), admin/manual entry, intake endpoint, CASAFARI (cold) | RED | No consented inbound source; intake leads are treated as warm even if they are cold listings |
| Agent onboarding | Chat: Google → questions → (payment) → WhatsApp Business check → Meta Embedded Signup → alert number → template approval | AMBER | Meta step on a phone (QR on same device), payment never verified, no terms acceptance, abandoned onboarding never chased |
| First message | Marketing template in ~30 s, only 09-21 Mon-Sat | AMBER | "30 s" true for ~43% of the week; "Vi tu anuncio" wrong for form leads; no hook |
| Conversation | AI qualifies, offers 2 slots, handles objections, hands off | AMBER | Slot tool can't take a day/time; instant handoff on commission; no nudge after slots |
| Booking | Google Calendar event, confirmation, reminders 24h/2h | GREEN/AMBER | Call-meeting reminder wording; promise "estudio de precios" never reaches the agent |
| After booking | Nothing | RED | No "did it happen?", no summary, no proof of value, no billing |
| Alerts to the agent | WhatsApp from the Sparrow number | GREEN/AMBER | Template says "Ábrelo en tu WhatsApp Business" even for undelivered; no email backup; no daily/weekly digest |
| Operating it | `/admin`, logs | RED | Nobody is told when production breaks |

## 3. Decision register (what we chose, why it is good, what it costs, what to do)

### 3a. Technical decisions

| # | Decision | Pros | Cons / risks | Verdict |
|---|---|---|---|---|
| T1 | Monolith (FastAPI) + Postgres job queue + worker inside the web process | Simple, cheap, `SKIP LOCKED` so multiple instances are safe, no Redis/Celery | One slow AI turn (up to ~25 s × retries) delays every first message; breaks the 30 s promise around 50+ active agents or a prospecting burst | **KEEP.** Run the worker as its own Railway service with 2-4 threads at ~30 agents |
| T2 | `create_all` only; new data goes into JSON columns | No migration tooling to learn | Cannot alter tables; consent proof, pause state, counters already live in JSON; concurrent boots on deploy; safety state can be overwritten by a read-modify-write | **CHANGE now.** Adopt Alembic (baseline + `stamp head`), move consent/pause state to real columns |
| T3 | Code owns lead state; AI only calls tools; any AI error → handoff (fail closed) | Safe, auditable, AI can't invent state | A 30 s Azure hiccup hands every active chat to humans for good and floods agents with alerts | **KEEP the principle, CHANGE the failure rule:** retry timeouts/429/5xx twice, hand off only on real failures, always alert when the AI cap is hit |
| T4 | Every send goes through `send_to_lead` → `can_contact` | Strong, verified: the only bypass is admin manual send, which blocks known leads | The message record is saved *after* the send; an HTTP timeout after Meta accepted it makes the job re-send (owner gets the first message twice) | **KEEP, add an outbox:** save the message as "sending" before the POST, catch network errors, never auto-retry a template after an ambiguous failure |
| T5 | Send from the agent's own number (Coexistence), agent pays Meta directly | Real number, agent keeps control, no WhatsApp reseller risk | Agent must add a card at Meta (the 131042 failure); a block/ban hits the agent's real business number; Coexistence limits (no groups, 14-day inactivity disconnect) | **KEEP.** Add a post-connect payment check and a go-live canary; stop cold sends on early block signals |
| T6 | Azure OpenAI with a provider switch (azure/openai/anthropic), AI caps per lead/account | Cost-capped, swappable, caps are adequate (~€0.5-1.5 per booked meeting) | Azure region unknown (EU residency); turns don't store prompt version/usage; one-shot manual rehearsal is the only eval | **KEEP.** Pin an EU region, store prompt version+hash+usage per turn, add scored rehearsals |
| T7 | Secrets and access: onboarding link = the credential, admin = HTTP Basic, intake key in the URL path | Zero-login onboarding for agents | Tokens leak to logs/Referer; no CSRF on admin (a web page you visit can send text from an agent's number); encryption key falls back to the session secret; `/start` and `/help` are open to abuse | **CHANGE.** Refuse to boot on default secrets, `Referrer-Policy`, admin CSRF/Origin check, caps on `/start` and intake, expire onboarding tokens once live |
| T8 | Operator-only `/admin`, no agent dashboard | Lean, matches "hands-off" promise | The agent gets no proof of value and the operator can't see "stuck at step X" or alert failures | **KEEP no dashboard.** Replace with a day-after "did it happen?" WhatsApp and a weekly summary |
| T9 | Merge to `main` = deploy to production; no CI, no staging | Fast | 242 tests run on SQLite only; Postgres column limits can fail *after* the Google event is created (orphan event, duplicate on retry) | **CHANGE.** CI that runs the suite on Postgres, clip inputs at the boundary, pin `requirements.txt` |
| T10 | Observability = `/admin` failed jobs + logs | Zero setup | Nobody is told when production breaks; `/health` checks nothing | **CHANGE this week (about a day):** real `/health`, external uptime check, a 5-minute watchdog that WhatsApps the operator (the `alert_ops` helper already exists), Sentry |

### 3b. Product decisions

| # | Decision | Pros | Cons / risks | Verdict |
|---|---|---|---|---|
| P1 | €30 per booked meeting, no setup fee | Pay-for-result is an easy yes for an autónomo; no up-front risk | "Booked" isn't a billable event (no `cancelled_at`; cancelled-in-chat stays `booked`; agent can book by phone or on another calendar); small revenue (about €75-110/agent/month, a 10-agent pilot is €500-1,000 MRR); chasing €30 invoices costs more than the fee | **KEEP as the pilot hook, CHANGE the mechanics:** define billable = confirmed meeting not cancelled by T-2h; day-after "¿Se hizo?" prompt; bill by default after 72 h silence; card on file, monthly. Test €50-60 for owner-requested meetings |
| P2 | First customer: the independent agent (RE/MAX) | Single decision-maker, fast sales cycle | Small ceiling; scale needs a franchise-owner or network sale; the moat is thin (Lystos already sells a WhatsApp AI for private sellers; the visible gap is calendar booking) | **KEEP for the pilot.** Pick 5-10 agents in 2-3 offices; open the franchise conversation early |
| P3 | Onboarding as an AI chat with a named persona | Warm, one thing at a time, good fit for the first half | "Ready in 10 minutes" is not true (about 14 inputs, Facebook login + 2FA, QR, card, then hours of template approval); QR can't be scanned on the same phone; abandoned onboarding is never chased; no terms acceptance | **KEEP the chat, CHANGE the Meta part:** "continue on the computer" branch on phones, verify payment, pinned checklist for the long Meta flow and the wait, email the resume link right after Google, record terms acceptance |
| P4 | Build cold prospecting (CASAFARI) before an inbound source | Volume without waiting for a lead partner | Weakest conversion (est. 1-3% booking) and the highest legal/Meta risk; real data cost €0.08-0.27 per lead (~€48/month worst case), not €0.04; one CASAFARI key for many agents conflicts with their terms | **CHANGE order:** ship inbound first (hosted valuation form + click-to-WhatsApp link/QR), run it 30 days, then cold in one zone with one agent, only after the lawyer answers |
| P5 | The agent writes the cold first message and accepts the risk | Puts liability where it belongs | Untested agent copy varies a lot; the risk warning discourages "gratis", the only hook that works | **KEEP, add 2-3 centrally tested default variants;** edits become the exception |
| P6 | Allowed hours 09-21 Mon-Sat for everyone | Protective for cold contact | For owners who *asked*, speed is the product: a Friday 21:30 request waits until Saturday 09:00; 72 of 168 hours | **CHANGE:** inbound/solicited leads 08-22 seven days; keep 09-21 Mon-Sat for cold |
| P7 | Conversation: qualify briefly, offer 2 slots, hand off on commission/anger/legal | Short, safe, hard to derail | Slots appear too early and repeat; same-day slots with 2 h notice; instant handoff on first commission signal ends the lead ("Perfecto, se lo paso" with no SLA); no nudge after slots; promises ("te escribo en primavera") with no job behind them | **CHANGE:** objection ladder before handoff; day/time-aware slot tool; never two slots on the same day; 4 h slot nudge; no promises without a scheduled job; re-alert the agent if no reply in 3 h |
| P8 | First message template (v2) | Discloses the assistant, includes BAJA | "Vi tu anuncio" is false for form/portal-request leads; asks about condition before earning a reply; no reason to answer | **CHANGE to v3:** "Hola {{1}}, soy el asistente virtual de {{2}} ({{3}}). Te escribo por tu vivienda en {{4}}: {{2}} puede prepararte, sin coste ni compromiso, un estudio de mercado con los precios a los que se han vendido pisos parecidos por tu zona. ¿Sigues pensando en venderla? Si prefieres que no te escriba más, responde BAJA." (needs Meta approval) |
| P9 | Handoff = the agent owns the chat forever | Respects the agent | If the agent is busy the owner waits, the 24 h window closes, nothing happens | **CHANGE:** re-alert after 3 h; clock-aware handoff text ("te escribe hoy / mañana a primera hora"); let the AI resume on "Sigue con {lead}" (teach it) |
| P10 | Alerts only by WhatsApp from the Sparrow number | One place, no login | One template for all cases; no email fallback; no booking summary; alert number can equal the sender number | **KEEP WhatsApp as primary.** Split into "needs you" and "undelivered, call them", add a `wa.me` button, add email as a backup |
| P11 | Languages es/fr/en everywhere | Differentiator for the Costa/expat market | Three-times cost on every change; France cold prospecting unusable (1 in 25 listings had a phone) | **KEEP, low priority.** Don't add languages before inbound works |
| P12 | Legal model: agent = controller, Sparrow = processor | Standard SaaS split | Sparrow chooses sources, dedupes and keeps a cross-agent suppression list, so joint-controller risk is real; intake leads skip the cold safeguards | **DECIDE WITH A LAWYER** (see 6) |

## 4. The most important findings, ranked

**Fix before the first real agent (pilot):**
1. **Meta payment method not verified** (131042). Check after connect, hold "listo" until it passes, offer a recovery card. *(Alert for failures already shipped.)*
2. **Text invisible with "reduce motion" on** (reproduced): assistant words stay at opacity 0 in the onboarding. One CSS line.
3. **Opt-out regex fires on scheduling preferences** ("no me llames por la mañana", "lo di de baja"): a live lead becomes a permanent opt-out. Verified.
4. **Slot tool can't take a day or hour**, so the AI wrongly says "no hay hueco" for Saturday or 11:00.
5. **Duplicate sends** (timeout after Meta accepted → job re-sends) and **lost webhook events** (errored events never retried; two quick messages can collide on a unique job key).
6. **A transient AI error permanently silences a lead.**
7. **Nobody is told when production is broken** (watchdog + uptime + Sentry).
8. **Intake leads are warm by default**, even when they are cold listings; require an attestation or route as cold.
9. **Template/approval silence:** a rejected first-contact template alerts the agent only (not you); no readiness check across all six templates before go-live.
10. **No terms acceptance** before the Google grant.

**Fix before the first invoice / paying customer:**
`cancelled_at` + day-after "¿Se hizo?" + weekly summary + card on file; Alembic; boot refuses default secrets; clip inputs + Postgres CI; admin CSRF; caps on `/start`/intake; HMAC (keyed) suppression hashes and a decision on Sparrow-wide opt-out; `SENDING_ENABLED` kill switch and a one-page runbook; verified backups and one restore drill; retention job for webhook payloads.

**Fix before any cold prospecting:** lawyer sign-off on legal basis and channel; Lista Robinson / Lista STOP check; written CASAFARI answer (one key per agent); early-block auto-pause; EU region for Railway and Azure; erasure tooling with a one-month clock.

## 5. Suggested work plan

| When | What | Effort |
|---|---|---|
| This week | Items 2, 3, 4 above, AI retry-before-handoff + alert at cap, watchdog + real `/health`, outbox + network-error handling, template v3 + objection/slot prompt rewrites, readiness check in `/admin` (Google, Meta, card, 6 templates) | 3-4 days |
| Before pilot | Meta payment check, desktop branch for the QR step, terms acceptance, resume-link email + nudge, `cancelled_at` + day-after prompt + weekly summary, intake attestation, inbound hours 08-22 | 4-6 days |
| First 30 days of pilot | Inbound source (hosted form + click-to-WhatsApp link/QR), Alembic, CI on Postgres, snooze/reschedule tools | 1-2 weeks |
| After lawyer + CASAFARI answers | Cold module in one zone, one agent | when unblocked |

## 6. Questions for you and for a lawyer

**You decide:** (1) Is the pilot inbound-only for the first 30 days? (2) €30 or test €50-60 for owner-requested meetings? (3) Who pays the CASAFARI key (absorb, pass through, agent's own)? (4) Is "valoración gratuita" / "estudio de mercado" the promise you want to make for the agent? (5) Is BAJA Sparrow-wide (one agent's opt-out stops all)?

**For a Spanish lawyer (from the compliance research):** (1) Is a single "¿Sigues vendiéndolo por tu cuenta?" plus a valuation offer to a private seller a commercial communication under LSSI art. 21, and is there any lawful route without prior consent? (2) Is Sparrow a processor, joint controller or independent controller when it pulls and dedupes CASAFARI data and keeps a cross-agent suppression list? (3) Do the Lista Robinson / Lista STOP Publicidad duties apply, per agent or per Sparrow, and is a DPIA mandatory?

## 7. What is already good (keep)
Single send gate (verified end to end); code-first hard stops (BAJA, data request, threats); AI disclosure in every language; the AI can't book unoffered slots and can't write state; exclusive-agency handling; Catalan/English/French quality; burst grouping; the guardrail tests (G1-G14); Meta and Stripe signature checks; time handling (UTC + `zoneinfo`).

## 8. How reliable is this analysis
- Product numbers (booking rates, LLM price, Spanish commission levels) are **assumptions**. Meta rates come from third-party aggregators, not Meta's rate card.
- Compliance items were researched on 26 Nov 2026; some official pages were read through a summarizer, and several claims are marked unverified in `docs/analysis/5_platform_and_legal.md` (Coexistence country list, Meta's exact AI clause, whether a Cloud API restriction also disables the Business app). **Not legal advice.**
- The UX review ran on fake mode and Chromium emulation; real iOS/Android Meta flows, real Google OAuth and real Embedded Signup were not exercised.
- The conversation review used the real model with a fake WhatsApp; each scenario was run once or twice, and the model is stochastic.
