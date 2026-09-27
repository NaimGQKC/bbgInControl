# Sparrow AI — instructions for Claude Code

WhatsApp AI assistant for independent real estate agents (RE/MAX first, leads from Lystos). New lead → WhatsApp from the agent's own number (Meta Coexistence) in 30s → Spanish seller conversation → valuation booked in the agent's Google Calendar. Pricing: €30 per booked meeting. The full product design lives in the Claude project (`sparrow-ai-design.md`); follow it.

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

## Rules (never break)
- Never hardcode Meta IDs, tokens, phone numbers or agency data: env vars or DB. Per-agent tokens are encrypted (`app/crypto.py`).
- Every outbound message goes through `send_to_lead` → `can_contact`. Check `do_not_contact` everywhere.
- The LLM never writes lead state; only tools and code do. LLM/tool error → handoff (fail closed).
- Webhooks and intake are idempotent (unique `wa_message_id`, `dedupe_key`, unique (account, phone)).
- Never read or answer chats with contacts that aren't leads (G1). Once the agent writes after the lead replied, the AI stops for that lead until resumed (G8).
- The AI always introduces itself as "el asistente virtual de {agente}" and never claims to be human (G13).
- Log every message and every LLM turn.
- Keep the pilot lean: no dashboards, settings pages, Google Ads intake or new CRMs until the core loop is proven.

## Tests
`pytest -q` must stay green. Every guardrail G1–G14 has a test in `tests/test_flow.py`; add one for every new rule. Use the frozen clock (`db.FROZEN_NOW`) and fakes.

## Pending
1. Lystos adapter (`app/intake.py:from_lystos`) once their API/payload is known.
2. End-to-end Coexistence test (echoes) after Tech Provider approval.
3. Allowed hours from Google Calendar working hours.
4. Monthly billing summary per agent.
