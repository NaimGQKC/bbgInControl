# Sparrow AI — handoff for a new session (QA, fixes, release)

*Written 25 Nov 2026. Read this fully, then `CLAUDE.md`, `docs/PRD.md`, `docs/PRODUCT_CONTEXT.md`. Reply to the founder (Naim) in the language he writes in (Spanish or English), short and simple, explain jargon, push back when something is wrong.*

## 1. Situation in one paragraph
Sparrow AI is a WhatsApp AI assistant for real-estate agents: new owner lead → WhatsApp from the agent's own number (Meta Coexistence) in ~30 s → AI qualifies (es/fr/en) → valuation booked in the agent's Google Calendar. **EUR 30 per booked meeting, no setup fee.** An optional **prospecting** module pulls private-seller listings with phones from CASAFARI and messages them with a first message the agent writes. The founder shows the end-to-end flow **live on someone else's phone tomorrow (26 Nov 2026)** and wants to leave and come back to a finished result. Your job: finish QA on the real model and the real deployed app, fix what you find, release safely, and leave a full report.

## 2. Repo, branches, deploy
- GitHub: `NaimGQKC/bbgInControl`. Railway deploys **`main`** to `https://app.sparrowai.eu` (FastAPI + Postgres). Landing site `www.sparrowai.eu` is separate (Lovable).
- `origin/main` already contains the prospecting feature (PR #12). **Not yet on main** (branch `claude/prospecting`): the PRD, the pre-demo fixes (`d8ce99e`), phone-QA fixes (`d821aa0`), the rehearsal script (`f6e6d7a`), and the prompt fix described in §6 (committed with this file). Production therefore still runs the older code until a PR is merged.
- 209 tests pass (`pytest -q`). Install if missing: `pip install --ignore-installed cryptography -r requirements.txt pytest --break-system-packages`.
- Never use `pkill`. Never print phone numbers, names or secrets from real data.

## 3. Access and credentials (never ask for secrets in chat)
- Use `request_credential` for secrets; the proxy injects them, you never see the value. If you run in the same cloud environment these may already exist; test with a harmless call first:
  - **Azure OpenAI** host `ale-9663-resource.openai.azure.com`, header `api-key`, deployment **`sparrow-chat`**. For scripts set `LLM_PROVIDER=azure_openai AZURE_OPENAI_ENDPOINT=https://ale-9663-resource.openai.azure.com AZURE_OPENAI_LLM_DEPLOYMENT=sparrow-chat AZURE_OPENAI_KEY=proxy-injected`.
  - **CASAFARI** host `platform.casafari.com` (Bearer). Free plan: ~960 tokens left; one search page = 10 tokens. Do not spend more than 30 tokens.
  - **Production `/admin`**: HTTP basic auth with `ADMIN_USER`/`ADMIN_PASSWORD` from Railway. Ask for it with `request_credential` (host `app.sparrowai.eu`, type basic). Without Chrome you can read `/admin` pages with curl through the proxy.
- Railway MCP needs the founder to sign in; skip it. Meta and Google consoles need the founder's own session.
- Skills/plugins to use (activated in the new session, check with `ListPlugins`): **SuperMatt** (`qa`, `diagnosing-bugs`, `fix-findings`), **pr-review-toolkit**, **Design**/**frontend-design** only for UI polish. Do not repeat the code reviews already done (§5). Chrome tools (`mcp__claude-in-chrome__*`): read the `chrome-browser` skill first.

## 4. What is verified, and what is not
**Verified (fakes, frozen clock, real browser on phone viewport, real model in rehearsal):** full lead flow es/fr/en; opt-outs; handoffs; booking; reminders; onboarding chat on iPhone 390x844 and Android 360x740 in ES/EN/FR; prospecting pipeline with fake CASAFARI; live CASAFARI pull (Zaragoza, 25 listings/page, 60% with phone); live Azure model answers conversations.
**Not verified:** live Meta (Embedded Signup with Coexistence, template approval, real WhatsApp delivery and echoes), live Google OAuth with this version, Postgres-specific behaviour, anything on the deployed app after the latest fixes, real conversations with a human owner.

## 5. QA already done (do not redo; the reports are not in git)
Three reviews ran on 25 Nov: code/security review, lead-flow QA (60 scenarios, fakes), phone onboarding QA. **Fixed** (commits above): "Ahora no" no longer submits a Meta template; prospecting step hidden unless `PROSPECTING_ENABLED` and Spanish owners; fake CASAFARI refused with live WhatsApp; go-live activates prospecting after recorded acceptance and re-arms queued leads; admin refuses default password when WhatsApp is live; page-state XSS closed and `<>` stripped from names; opt-out recognised in any wording ("¡BAJA!", "STOP 🙏", "no me escribas más"); bare "no" mid-chat is not a refusal; booking confirmation sent by code if the model is silent; admin test lead repeatable; LLM timeout 25 s; help chat no longer quotes a setup fee; long-word layout bug; test event always cleaned; slot dedupe in FR/EN; tap targets; `Cache-Control: no-store` on onboarding.
**Still open (decide, then fix or document for the demo):**
- Billing is not built (no `cancelled_at`; count booked meetings by hand).
- No sending outside Mon–Sat 09:00–21:00 Europe/Madrid, test leads included; `next_allowed_time` and `in_allowed_hours` disagree on the window.
- Reminder text for phone-call meetings reads wrong in Spanish ("pasa por por teléfono").
- Undeliverable first message (number without WhatsApp, landline) is lost silently; no alert to the agent. A calendar failure does not hand the lead off. A lead who cancels in chat gets handed off but reminders still go out and the meeting stays `booked`.
- No WhatsApp booking summary to the agent (CLAUDE.md pending item 5).
- Open endpoints: `/start` creates unlimited accounts; `/help` sends the operator a WhatsApp each call; help-chat cap is per account; admin POSTs have no CSRF protection.
- Legal pages ignore `?lang=`; landing → "Configurar mi asistente" creates a second account; `requirements.txt` unpinned; Postgres column lengths unverified (long names/agencies may fail); `SESSION_SECRET` doubles as encryption fallback; `BASE_URL`/`CONTACT_EMAIL` defaults are unsafe (check Railway has real values).
- Opt-out hash is plain SHA-256 (brute-forceable); the privacy text now says "one-way", not "irreversible".
- Coexistence echoes untested live.

## 6. Real-model rehearsal (run 25 Nov, `scripts/ai_rehearsal.py`, 9 scenarios) — findings to act on
Good: identifies itself as the virtual assistant; answers "are you a robot" honestly; refuses to give a price and offers the visit; soft "no" then second "no" ends the chat politely; FR and EN flows read natural; facts are saved correctly (condition, timeline, notes).
Problems seen:
1. **"¿De dónde sacaste mi número?"** → the model answered and called handoff, and the engine discards text written with a handoff, so the owner only got "se lo paso a Laura". The standard prompt (es/fr/en, `app/prompt.py`) told it to use handoff. **Fixed in the prompt (committed now) but NOT re-run: re-run `scripts/ai_rehearsal.py es_who_are_you` to confirm.**
2. **Over-eager handoff:** an owner who says "Pido 250.000 pero el vecino vendió el suyo por 300.000" was handed off immediately. Stating an asking price is qualification data, not a negotiation request: refine the handoff rule so the assistant keeps qualifying and books the visit.
3. **Objection ignored:** "no me gusta pagar comisiones" got no acknowledgement and the same slot offer was repeated three times in a row. The assistant should acknowledge the objection once, then offer slots without repeating verbatim.
4. **Data-deletion / threat message from a normal (non-cold) lead** ("os denuncio a la AEPD, borrad mis datos") ends as a silent opt-out with no alert to the agent. `guards.cold_hard_stop` already classifies "data"/"threat" for cold leads: apply it to all leads so the agent is alerted (GDPR one-month deadline).
5. Minor: French slots offered twice on the same day; "valoración gratuita" wording in the first template (check it does not promise more than the agent wants).

## 7. The task (do in this order; stop and ask only where marked ASK)
1. **Orient:** read this file, `CLAUDE.md`, `docs/PRD.md`; `git status`; run `pytest -q`; check `ListPlugins`.
2. **Rehearse again on the real model** (`scripts/ai_rehearsal.py`, all scenarios, plus 5 new ones you design: owner asks commission, owner is a buyer, owner says already has an agent, owner writes in broken Spanish, owner sends a voice-note placeholder). Judge quality as a senior real-estate sales coach would: tone, one question at a time, no pressure, no promises, G13 intro. Fix prompt/code for §6 items 1–5 with tests; re-run until clean. Keep AI caps and every rule in `CLAUDE.md`.
3. **Fix the still-open items that could embarrass the demo** (§5): at minimum the call-meeting reminder wording, silent undeliverable first message (alert the agent), cancel-in-chat leaving reminders, and the hours-window mismatch. Add tests. Document the rest.
4. **Release:** open a PR `claude/prospecting` → `main`, run the code review skills once on the PR diff, merge **only if the authorizations in the prompt say so and tests are green**, then wait for the Railway deploy (poll `https://app.sparrowai.eu/` until the new build answers; the landing 200 and `/privacidad` 200 are the smoke test).
5. **Verify production read-only:** `/admin` (basic auth credential), accounts list, the connected WhatsApp number, templates status, `PROSPECTING_ENABLED` state (must stay off), that the account for the demo is `live`. Report anything missing. Do not change Railway env vars.
6. **Real WhatsApp test (only to the authorized test phone, only inside Mon–Sat 09:00–21:00 Madrid):** first confirm the connected sender number differs from the test phone (a number cannot message itself; if equal, STOP and tell the founder you need another phone). Then create ONE test lead in `/admin` (test flag on) and report that the first message left, from which number, and its exact text. The founder replies from his phone; you cannot send inbound messages (Meta signs webhooks). If he is not there, stop after the first message and list what to check when he replies.
7. **If Chrome tools are connected:** walk the live onboarding at `https://app.sparrowai.eu/` on a phone viewport once (ES), without completing steps that use the founder's real Google/Meta accounts unless he explicitly said so.
8. **Final report** (Spanish unless he writes English): green/red checklist for tomorrow's demo, what changed (commits/PR), what is still risky, the exact demo runbook with times, and anything only he can do. Keep it to one page plus an appendix.

## 8. Rules that override everything
Every outbound message goes through `send_to_lead` → `can_contact`. Honour `do_not_contact` everywhere. The AI always says it is the virtual assistant of the agent (G13). No schema changes (use existing JSON columns). Prospecting stays off in production. No secrets or personal data in logs, commits or chat. Do not send anything to anyone but the authorized test phone. Commit with the attribution lines the harness gives you; push only to the branch you were assigned; open a PR only when the prompt says so.
