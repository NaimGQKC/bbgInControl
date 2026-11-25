# Sparrow AI — Product Requirements Document (PRD)

*Version 1.0 · 25 Nov 2026 · Owner: Naim · Status: pilot-ready, demo on 26 Nov 2026*
*Source of truth for code rules is `CLAUDE.md`; this document says what the product must do and why.*

---

## 1. Summary
Sparrow AI is **agentic pre-sales for real-estate agents**. It turns a property owner who may want to sell into a **valuation meeting in the agent's Google Calendar**, end to end, with no work from the agent until the meeting:

> New owner lead → WhatsApp from the **agent's own number** within ~30 seconds → AI conversation in Spanish/French/English that qualifies the owner → valuation booked in the agent's calendar → reminders → the agent shows up and closes.

**Business model:** pay per result. **EUR 30 per booked valuation meeting. No setup fee.** Meta's WhatsApp message fees are paid by the agent directly to Meta.

**One-line pitch:** "The assistant opens the door; the agent decides and closes."

## 2. Problem
- Independent and small-office agents lose valuations (and therefore listings) because first contact is slow and follow-up is inconsistent.
- Listing scarcity (captación) is the stated top concern of Spanish agencies (idealista ESI Q1 2026).
- Agents do not want to learn or manage AI tooling; they only need the meetings.
- Network directors want agents to earn more and stay (retention, office profitability) but fear tech that leaves the agent alone or replaces the human.

## 3. Goals and non-goals
### Goals (pilot, next 30 days)
1. A single agent or office goes from sign-up to receiving booked valuations with **one 10-minute conversation** and nothing else to do.
2. Prove the funnel with real numbers: contacts → replies → qualified → meetings booked → meetings held (agent-confirmed).
3. Never send anything unsafe: opt-outs respected, quality of the agent's WhatsApp number protected, AI never claims to be human.
4. Keep variable cost per booked meeting under ~EUR 6 (excluding Meta fees) so EUR 30 leaves a healthy margin.

### Non-goals (until the core loop is proven)
Dashboards and settings pages, Google Ads intake, new CRMs, voice/avatar, multi-office reporting, French cold prospecting, billing automation beyond a monthly summary, reselling or pooling data across agents.

## 4. Users
| User | Need | Notes |
|---|---|---|
| **Agent / small office owner** (customer) | Valuation meetings without extra work; stays in control; protects their number and brand | Onboards through the chat; keeps their WhatsApp Business app (Coexistence) |
| **Property owner** (lead) | A short, respectful, honest message; easy way to stop | Receives WhatsApp from the agent's number; AI says it is the agent's virtual assistant |
| **Operator** (Naim) | See that it works, pause anything, get alerts, bill per meeting | `/admin` ops console, WhatsApp alerts |
| **Network director** (next segment) | Agent retention, office profitability, no brand risk | Needs team coordination and visibility (planned) |

## 5. User journeys
### 5.1 Agent onboarding (one conversation, then hands-off)
Guided chat with a named AI guide (openly an AI). Screens in **ES / EN / FR**; the owners' language is chosen in the chat.
1. **Google sign-in** (name, email; calendar proven by reading it once). Scopes: openid, email, profile, calendar.events, calendar.freebusy (verified by Google).
2. **Agency + zones.**
3. **Preferences** ("how do you want me to work?" free text → goes into the AI prompt, never overrides hard rules).
4. **Preview** of the exact first message an owner would receive (same code path as the real one).
5. **Prospecting (optional)** — see 5.3.
6. **Payment** — skipped unless `STRIPE_PAYMENT_LINK` is set (no setup fee).
7. **WhatsApp Business check → Meta Embedded Signup (Coexistence)** → alert number → wait for Meta template approval (page flips to "ready" by itself).
8. "Hablar con una persona" is always visible; "Mis datos" lets the agent edit any answer.

### 5.2 Lead journey (inbound / intake)
Lead arrives (webhook `/intake/{key}`, CSV/Lystos export, manual) → first WhatsApp in ~30 s inside allowed hours → AI conversation → slots offered → owner picks → calendar event created → confirmation → reminders (24 h, 2 h) → agent alert. Owner can reply BAJA/STOP/ARRÊT at any time; the agent can take over at any time and the AI then stops.

### 5.3 Prospecting journey (private sellers, optional, Spain only)
1. Agent opts in during onboarding: picks **one zone**, property types, price range, daily cap (3/5/10).
2. Agent **writes or edits their own first message** (placeholders `{nombre} {tipo} {zona} {agente} {agencia}`), sees the exact text, reads four plain risks and explicitly accepts responsibility.
3. The message goes to Meta as the **agent's own template** `cold_first_contact_v{n}`; nothing is pulled or sent until Meta approves it.
4. Every 3 h (inside allowed hours) new private-seller listings with phone are pulled (CASAFARI Private Seller Leads), filtered, de-duplicated and fed into the normal lead pipeline.
5. Objection ladder (code-enforced): hard stops end the chat; one soft-no gets the agent's own success line; a second no ends it.

### 5.4 Operator journey
Receives alerts (agent needs help, template rejected, prospecting paused). `/admin`: accounts, templates (resubmit), prospecting status with pause/resume. Pauses are never lifted automatically.

## 6. Functional requirements
Status: ✅ built and covered by tests · 🟡 built, needs live verification · ⏳ planned (not built).

### 6.1 Onboarding
| ID | Requirement | Status |
|---|---|---|
| ON-1 | One continuous chat, one question at a time, resumable after reload, mobile-first | ✅ |
| ON-2 | All agent-facing text translated ES/EN/FR; a test fails if a string is missing | ✅ |
| ON-3 | Google OAuth with calendar read proof; tokens encrypted at rest | ✅ / 🟡 live |
| ON-4 | Meta Embedded Signup (Coexistence): existing WhatsApp Business number + QR; personal WhatsApp numbers are explained as unsupported | 🟡 live |
| ON-5 | Templates auto-submitted to Meta in the owners' language; go-live when that language is approved | ✅ / 🟡 live |
| ON-6 | Preview of the exact first message (same code path as sending) | ✅ |
| ON-7 | "Hablar con una persona" always visible, alerts the operator | ✅ |
| ON-8 | Agent can edit answers later ("Mis datos") | ✅ |
| ON-9 | No tax ID requested | ✅ |

### 6.2 Lead handling and AI
| ID | Requirement | Status |
|---|---|---|
| LD-1 | First message within ~30 s (configurable), only inside allowed hours (Madrid time), otherwise queued | ✅ |
| LD-2 | Intake is idempotent (unique (account, phone), `dedupe_key`, unique `wa_message_id`); phone normalisation for +34/+33 | ✅ |
| LD-3 | AI introduces itself as "el asistente virtual de {agente}" in each language (G13) and never claims to be human | ✅ |
| LD-4 | The LLM never writes lead state; only tools/code do; any LLM/tool error → hand off to the agent (fail closed) | ✅ |
| LD-5 | When the agent writes in a chat, the AI stops for that lead until resumed (G8); never read/answer non-leads (G1) | ✅ |
| LD-6 | Per-lead and per-account AI caps (`ai_max_*`); long inbound messages cut; at cap, chat is handed to the agent | ✅ |
| LD-7 | Opt-out words (BAJA, STOP, ARRÊT, …) set do_not_contact immediately; every outbound goes through `send_to_lead` → `can_contact` | ✅ |
| LD-8 | Every message and every LLM turn is logged | ✅ |
| LD-9 | Voice notes transcribed (when configured); other media handled gracefully | 🟡 |

### 6.3 Booking
| ID | Requirement | Status |
|---|---|---|
| BK-1 | Offer real free slots from the agent's Google Calendar (free/busy), in the owner's language and time format | ✅ |
| BK-2 | Create the event with owner details and a briefing, in the agent's calendar; confirm to the owner | ✅ |
| BK-3 | Reminders 24 h and 2 h; cancelled/deleted events are not reminded; agent deleting the event cancels the meeting | ✅ |
| BK-4 | Revoked Google access detected daily; tokens deleted; agent alerted | ✅ |
| BK-5 | WhatsApp booking summary to the agent | ⏳ (needs a Sparrow template) |
| BK-6 | Allowed hours from the agent's Google Calendar working hours | ⏳ |

### 6.4 Prospecting (cold private sellers)
| ID | Requirement | Status |
|---|---|---|
| PR-1 | Opt-in twice: global `PROSPECTING_ENABLED` (default off) and per account; Spain only; live accounts only | ✅ |
| PR-2 | Agent configures zone/types/price/cap and writes the cold message in the chat; explicit risk acknowledgement stored (`ack_at`) | ✅ |
| PR-3 | Mandatory, non-removable footer: names the assistant and the agent, BAJA/STOP, source of the number, privacy link to the owners' section | ✅ |
| PR-4 | Agent's own Meta template; nothing pulled/sent until approved; rejection alerts agent and operator | ✅ / 🟡 live |
| PR-5 | Pull cadence `PROSPECTING_POLL_HOURS` (default 3); 1 page = 10 tokens ≈ EUR 0.40; page cap per run | ✅ |
| PR-6 | Filters: valid Spanish numbers, property types, price range, sale/rent, date window; dedupe by phone, URL, cross-portal flat, existing leads, opted-out numbers (shared hash list) | ✅ |
| PR-7 | Daily cap (agent max 20, hard max 50) and ramp-up (3/day days 0-2, 5/day to day 7) | ✅ |
| PR-8 | **Auto-pause, never auto-resume:** number quality not green, opt-outs > 15% of last 50 cold leads (min 10), CASAFARI out of tokens; operator + agent alerts | ✅ |
| PR-9 | Cold leads: no automatic follow-ups; hard stops in code; one soft-no reply using the agent's own success line (never invented numbers); second no ends | ✅ |
| PR-10 | Source recorded per lead with expiry; data deleted at expiry (CASAFARI: account open + 30 days); opted-out numbers survive only as a hash | ✅ |
| PR-11 | Team coordination: two agents of one office never message the same owner | ⏳ |
| PR-12 | One CASAFARI key per agent or written CASAFARI approval for pooling | ⏳ (decision/legal) |

### 6.5 Operator and admin
| ID | Requirement | Status |
|---|---|---|
| AD-1 | `/admin` behind basic auth; production must not run with default credentials | 🟡 (see §9) |
| AD-2 | Account page, template resubmission, per-account prospecting line with pause/resume | ✅ |
| AD-3 | WhatsApp alerts to the operator for "needs help", template rejected, paused prospecting | ✅ |

### 6.6 Legal and trust
| ID | Requirement | Status |
|---|---|---|
| LG-1 | Terms, privacy, data-deletion pages in ES/EN/FR; Google Limited Use section; draft pending legal review | ✅ (draft) |
| LG-2 | Agent = data controller and responsible for contacts, messages and lawful basis; Sparrow = processor (DPA in terms) and own controller only for aggregated analytics; agent indemnifies Sparrow | ✅ (draft) |
| LG-3 | Recorded click-accept of the terms at sign-up | ⏳ |
| LG-4 | Spanish lawyer review before any real cold prospecting (LSSI art. 21; AEPD PS/00234/2024 precedent; EDPB 1/2024 para. 113) | ⏳ |

### 6.7 Billing
| ID | Requirement | Status |
|---|---|---|
| BL-1 | A meeting is billable when accepted by the owner and created in the calendar; not billable if the owner cancels more than 24 h before | ⏳ rule is in the terms; the code does not record cancellations (`cancelled_at`) yet, so count booked meetings by hand and check cancellations |
| BL-2 | Monthly summary per agent (meetings × EUR 30) | ⏳ |
| BL-3 | No-show policy (bill booked vs held) | ⏳ decision |
| BL-4 | Stripe payment link for a setup fee | optional, off |

## 7. Non-functional requirements
- **Compliance:** AI Act art. 50 disclosure (assistant says it is virtual); GDPR art. 14 information to owners (footer link); opt-out honoured in code; retention limits (§6.4 PR-10; agent data 12 months after the end — manual cleanup pending).
- **Security:** per-agent tokens encrypted (`app/crypto.py`); no secrets in repo; Meta webhook signature checked when `META_APP_SECRET` is set; admin protected; agent-written text always escaped when rendered.
- **Reliability:** Postgres job queue with retries and dedupe keys; webhook idempotency; fail closed on LLM/tool errors; worker runs inside the web process by default.
- **Performance:** first message ~30 s; AI reply 30-60 s humanised delay.
- **Cost controls:** AI caps, CASAFARI page cap, daily lead caps.
- **Internationalisation:** screen language (ES/EN/FR) separate from lead language (ES/FR/EN); dates, phones and opt-out words per language.
- **Observability:** Events table for the funnel (lead_received, first_message_sent, replied, slots_offered, meeting_booked, handoff, prospect_run, quality_rating, …).
- **Schema:** no ALTERs; new data goes into existing JSON columns (`settings`, `facts`, `external`).

## 8. Architecture and configuration
FastAPI + SQLAlchemy (SQLite dev/tests, Postgres on Railway), Jinja templates, Postgres job queue (`@jobs.handler`). Real/fake clients per integration: `WHATSAPP_MODE`, `CALENDAR_MODE`, `LLM_MODE`, `CASAFARI_MODE` (`live|fake`).

Key modules: `engine.py` (flow, `send_to_lead`), `guards.py` (`can_contact`), `prompt.py`, `llm.py`, `templates.py`, `coldmsg.py`, `prospecting.py`, `casafari.py`, `quality.py`, `onboarding.py`, `web.py`, `webhooks.py`, `admin.py`, `lang.py`.

**Production environment (names only, values live in Railway):**
`BASE_URL`, `DATABASE_URL`, `ENCRYPTION_KEY`, `SESSION_SECRET`, `ADMIN_USER`, `ADMIN_PASSWORD`, `META_APP_ID`, `META_APP_SECRET`, `META_CONFIG_ID`, `META_VERIFY_TOKEN`, `SPARROW_PHONE_NUMBER_ID`, `SPARROW_WA_TOKEN`, `SPARROW_WABA_ID`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_KEY`, `AZURE_OPENAI_LLM_DEPLOYMENT`, `OPS_WHATSAPP`, `CONTACT_EMAIL`, `LEGAL_ENTITY`, `LEGAL_NIF`, `LEGAL_ADDRESS`, optional `STRIPE_PAYMENT_LINK`, `STRIPE_WEBHOOK_SECRET`, prospecting: `CASAFARI_API_KEY`, `CASAFARI_MODE=live`, `PROSPECTING_ENABLED` (last).

## 9. Known risks and open decisions
| Risk / decision | Why it matters | Owner / next step |
|---|---|---|
| **Cold WhatsApp to portal advertisers** may breach LSSI art. 21; AEPD fined EUR 2,000 in PS/00234/2024 and rejected the "portal terms allow it" defence; EDPB says messenger marketing needs consent | The agent is responsible by contract, but authorities can still pursue Sparrow; Meta can lock the agent's own number | Lawyer review; start the pilot with inbound / consented leads; keep cold prospecting small and opt-in |
| **CASAFARI terms** ban passing results to third parties | One Sparrow key serving many agents may count | One key per agent, or written approval |
| **Production availability** (Railway credit exhausted, 25 Nov 2026) | Demo depends on app.sparrowai.eu | Move to a paid plan, redeploy, verify before the demo |
| **Admin default credentials** (`admin` / `change-me`) | Anyone could open `/admin` if env vars are unset | Set `ADMIN_USER` / `ADMIN_PASSWORD` in Railway; refuse defaults at startup |
| **Setup fee wording** (code default EUR 90 vs "no setup fee") | Inconsistent promise | Set the default to 0 and align `CLAUDE.md` |
| **No-show billing, who is billable** | Margin and trust | Decide before the first invoice |
| **Meta quality / block thresholds** are unpublished | A drop affects the agent's whole business channel | Auto-pause is in place; keep daily caps low at first |
| **Unproven funnel** (no case studies) | Directors will ask for proof | 30-day pilot with pre-agreed metrics and stop rules |
| **Lystos** sells its own WhatsApp AI agent (EUR 0.50/conversation) | Competitor and lead source | Differentiate on booked meeting + agent's own number + per-meeting price |

## 10. Pilot plan and success metrics
**Pilot:** one office, 2–3 agents, 30 days. EUR 30 per booked valuation only; credit if no meeting is held. Start with owners who already asked to be contacted (inbound/consented); add a small cold module only after legal sign-off. Aim for 300–1,000 delivered contacts.

**Report weekly:** delivered contacts, first-message latency, reply rate, opt-out rate, number quality, qualified rate, meetings booked, meetings held (agent confirms), valuations done, signed listings (as case studies), cost per meeting, speed A/B (instant AI vs the agent's usual response time).

**Stop rules (pre-registered):** pause cold flow if fewer than 3 meetings in the first 300 contacts, opt-outs > 15%, or quality rating drops below green. **Go decision at day 30:** at least X meetings held and agent willing to continue.

**Unit economics (assumptions to be replaced by pilot data):** variable cost per booked meeting about EUR 2–6 excluding Meta fees (data about EUR 0.04 per usable lead; AI about EUR 0.5–1; Meta about EUR 0.06 per first message, paid by the agent); gross margin 81–93% at EUR 30 at base conversion.

## 11. Roadmap (after the pilot starts)
1. Team plan for offices: no duplicate contact between agents of one office, per-agent routing, monthly report per agent and per zone (answers "tech must not leave the agent alone").
2. Booking summary WhatsApp to the agent; allowed hours from the calendar.
3. Monthly billing summary per agent.
4. Inbound adapters: Lystos (when payload known), the agent's own valuation form, email/CSV.
5. Recorded terms acceptance; 12-month agent data cleanup job.
6. Faster pulls or alerts (only after PMF; today CASAFARI is poll-only).

## 12. Demo runbook (26 Nov 2026, on another person's phone)
**Before:**
1. `https://app.sparrowai.eu/` must load (it returned Railway's "Application not found" on 25 Nov: fix hosting first).
2. `ADMIN_USER`/`ADMIN_PASSWORD` set; `BASE_URL` correct; Google OAuth redirect URI matches; Meta app live; Sparrow's own WhatsApp number and templates approved; payment method on the WABA.
3. Use a **WhatsApp Business** number for the demo (personal WhatsApp numbers cannot be connected). Have the QR step ready on a laptop or a second device.
4. Open `/admin` once to confirm it works; keep `PROSPECTING_ENABLED` off for the demo unless you intend to show it.
**Fallback if Meta/Google is slow:** show the flow on the fake-mode preview service or the Playwright recording; explain the Meta approval wait.
**During:** show 1) the chat onboarding, 2) the first-message preview, 3) a test lead → WhatsApp in ~30 s, 4) the booked event in the calendar.

## 13. Test status
Automated: 175 tests pass (flow, language, prospecting, cold contact, quality). QA run before this demo: see `research_notes/QA/` (onboarding on phone, lead flow end to end, code review). Not testable here: live Meta, Google and CASAFARI behaviour beyond three manual searches; these are verified in the runbook above.
