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
- Real/fake clients: `WHATSAPP_MODE`, `CALENDAR_MODE`, `LLM_MODE` = `live|fake`.

## Onboarding (the product's promise: one hands-on conversation, then hands-off)
`app/web_templates/onboarding.html` is a guided chat with a named assistant (`ASSISTANT_NAME`) that is openly an AI (AI Act) but warm and human in voice. Steps live in `app/onboarding.py` (`STEPS`): Google (gives name + email, calendar is read once to prove it works) → agency + zones → free-text preferences ("cómo quieres que trabaje", saved in `extra_context`, goes into the AI prompt but never beats the hard rules) → preview of the exact first message an owner will get (same code path as the real message) → Stripe payment (skipped without `STRIPE_PAYMENT_LINK`) → WhatsApp Business check → Meta Embedded Signup → alert number → wait for Meta approval (the page flips to "listo" by itself). We never ask for a tax ID (autónomos don't need Meta business verification; only Sparrow does). Keep the chat short, one thing at a time, with "Hablar con una persona" always visible (they choose a call or a message; it alerts the operator). Quick-reply buttons must say exactly what the agent would say; no avatar or voice in the MVP. Lead-facing copy stays "el asistente virtual de {agente}" (G13): the named persona is only for agents.

## Languages
Agent-facing screens (landing, onboarding, legal pages, the onboarding help chat) speak Spanish, English and French. In `onboarding.html` every text goes through `tr("texto en español", {vars})`; add its English and French to `app/i18n_onb.py` (a test fails otherwise; keep `{placeholders}`, `**bold**` and tags). Legal pages carry all three languages in one page. Lead-facing messages (templates, the seller chat, the Marta demo bubbles) stay Spanish: the leads are in Spain. Ops console stays Spanish.

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
