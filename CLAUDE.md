# Sparrow AI — instructions for Claude Code

WhatsApp AI assistant for independent real estate agents (RE/MAX first, leads from Lystos). New lead → WhatsApp from the agent's own number (Meta Coexistence) in 30s → Spanish seller conversation → valuation booked in the agent's Google Calendar. Pricing: €90 one-time per agent integration (Stripe) + €30 per booked meeting. The full product design lives in the Claude project (`sparrow-ai-design.md`); follow it.

## Talking to Naim
English, short, simple language, explain any jargon. Push back when something is wrong. Lead-facing copy: Spanish from Spain.

## Code map
- `app/engine.py` — the flow and job handlers. `send_to_lead()` is the ONLY way to message a lead.
- `app/guards.py` — `can_contact()` (single gate), allowed hours, 24h window.
- `app/prompt.py` — seller script. `app/llm.py` — AI tool loop + tool schemas (`LLM_PROVIDER`: azure_openai default, openai, anthropic).
- `app/templates.py` — ALL template names/texts (bump the `_vN` suffix when a text changes).
- `app/webhooks.py` — Meta webhooks. `app/web.py` — onboarding, OAuth, intake, legal. `app/admin.py` — ops console.
- `app/jobs.py` — Postgres job queue; handlers registered with `@jobs.handler("type")`.
- `app/casafari.py` — CASAFARI Private Seller Leads client (real + fake). `app/prospecting.py` — turns private-seller listings for one zone into leads (filters, dedupe, daily cap) via `engine.ingest_lead`; jobs `prospect_scan`, `prospect`, `source_cleanup`. `scripts/casafari_yield.py` — yield/cost check per area (masked output, capped, no DB writes).
- Real/fake clients: `WHATSAPP_MODE`, `CALENDAR_MODE`, `LLM_MODE`, `CASAFARI_MODE` = `live|fake` (CASAFARI defaults to fake).

## Onboarding (the product's promise: one hands-on conversation, then hands-off)
`app/web_templates/onboarding.html` is a guided chat with a named assistant (`ASSISTANT_NAME`) that is openly an AI (AI Act) but warm and human in voice. Steps live in `app/onboarding.py` (`STEPS`): Google (gives name + email, calendar is read once to prove it works) → agency + zones → free-text preferences ("cómo quieres que trabaje", saved in `extra_context`, goes into the AI prompt but never beats the hard rules) → preview of the exact first message an owner will get (same code path as the real message) → Stripe payment (skipped without `STRIPE_PAYMENT_LINK`) → WhatsApp Business check → Meta Embedded Signup → alert number → wait for Meta approval (the page flips to "listo" by itself). We never ask for a tax ID (autónomos don't need Meta business verification; only Sparrow does). Keep the chat short, one thing at a time, with "Hablar con una persona" always visible (they choose a call or a message; it alerts the operator). Quick-reply buttons must say exactly what the agent would say; no avatar or voice in the MVP. Lead-facing copy stays "el asistente virtual de {agente}" (G13): the named persona is only for agents.

## Languages
Two different things:
1. **Screen language** (agent-facing: landing, onboarding, legal pages, onboarding help chat): Spanish, English, French. In `onboarding.html` every text goes through `tr("texto en español", {vars})`; add its English and French to `app/i18n_onb.py` (a test fails otherwise; keep `{placeholders}`, `**bold**` and tags).
2. **Lead language** (what the agent's owners receive): `account.settings["lang"]` = `es` (default) | `fr` | `en`, chosen in the onboarding chat. Everything lead-facing goes through `app/lang.py` (dates, free texts, opt-out words, phone prefix, calendar texts) and `app/templates.py` (each WhatsApp template has es/fr/en; same name per language at Meta; submitted in the agent's language at connect; the account goes live when ITS language is approved). The AI prompt exists in the three languages (`app/prompt.py`); G13 (introduce as the virtual assistant) holds in each. Opt-out words: BAJA, STOP, ARRÊT and so on (`guards.OPT_OUT_WORDS`). Agent alerts from the Sparrow number use the agent's language; the operator's template stays Spanish.
Adding a language later: texts in `lang.py`, bodies in each template's `texts`, a prompt function, the demo texts in `onboarding.html`, and a test.

## Rules (never break)
- Never hardcode Meta IDs, tokens, phone numbers or agency data: env vars or DB. Per-agent tokens are encrypted (`app/crypto.py`).
- Every outbound message goes through `send_to_lead` → `can_contact`. Check `do_not_contact` everywhere.
- The LLM never writes lead state; only tools and code do. LLM/tool error → handoff (fail closed).
- Webhooks and intake are idempotent (unique `wa_message_id`, `dedupe_key`, unique (account, phone)).
- Never read or answer chats with contacts that aren't leads (G1). Once the agent writes after the lead replied, the AI stops for that lead until resumed (G8).
- The AI always introduces itself as "el asistente virtual de {agente}" and never claims to be human (G13).
- Log every message and every LLM turn.
- AI usage is capped (`ai_max_*` settings in `app/config.py`): at the per-lead cap the chat is handed to the agent and the model is not called; long lead messages are cut before the model. Never remove these caps.
- Keep the pilot lean: no dashboards, settings pages, Google Ads intake or new CRMs until the core loop is proven.
- Prospecting (CASAFARI) is opt-in twice: global `PROSPECTING_ENABLED` (default false) and `account.settings["prospecting"]["enabled"]` (default false). Spain only, live accounts only. It never messages anyone: listings go through `engine.ingest_lead` and the normal first_contact → `can_contact` → `send_to_lead`. Keep the caps: pages per run (`PROSPECTING_MAX_PAGES_PER_RUN`, hard max 4, 10 tokens each), `daily_cap` (hard max 50), errors (402/429) stop the run without a retry. Skip numbers opted out on any account or on the suppression hash list.
- Every CASAFARI lead records its source in `lead.facts["_source"]` (provider, listing id, portal, `expires`). Keys starting with `_` are code-only: they never reach the AI prompt and `save_facts` can't write them. CASAFARI terms: store results only while our CASAFARI account is open + 30 days (`CASAFARI_CLOSED_ON` shortens `expires`), never resell, never keep raw API payloads, never print phones or names from it (mask them).
- No schema changes without a migration path: `create_all` does not alter existing tables, so new data goes into existing columns/JSON (e.g. the `stripe` row in `integrations`).

## Tests
`pytest -q` must stay green. Every guardrail G1–G14 has a test in `tests/test_flow.py`; add one for every new rule. Use the frozen clock (`db.FROZEN_NOW`) and fakes.

## Pending
1. Lystos adapter (`app/intake.py:from_lystos`) once their API/payload is known.
2. End-to-end Coexistence test (echoes) after Tech Provider approval.
3. Allowed hours from Google Calendar working hours.
4. Monthly billing summary per agent (€30 per booked meeting).
5. Booking summary WhatsApp to the agent (today they only get alerts when a lead needs them). Needs a Sparrow template.
6. Looped clips for each Meta screen in the onboarding chat (need real screen recordings).
7. (done) Google OAuth verification approved: the "unverified app" warning is gone.
8. Data retention: Google tokens are now deleted automatically when an agent revokes access (daily `google_check` job, `gcal.mark_revoked`) and by hand on request/closure within 30 days. Still manual: deleting an agent's data 12 months after they stop (the policy promises it). Do that cleanup before the first agents reach that date.
9. CASAFARI data retention: the daily `source_cleanup` job deletes CASAFARI leads past `facts._source.expires` (default 365 days, or `CASAFARI_CLOSED_ON` + 30) that never answered (an opted-out number survives only as a hash so it is never contacted again); leads that answered are kept but lose the listing URL/id. Check before the first expiry that this matches the privacy policy, and set `CASAFARI_CLOSED_ON` the day the CASAFARI account closes. Prospecting is not yet in the onboarding: set `settings["prospecting"]` by hand (location id from `scripts/casafari_yield.py`).
