# Sparrow AI: MVP

A WhatsApp AI assistant for real estate agents.
- A new lead arrives → a WhatsApp from the agent's own number in 30 seconds.
- The AI chats in Spanish (sellers only) → books a valuation in the agent's Google Calendar.

How it works, with diagrams: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Rules for Claude Code: `CLAUDE.md`.

**Status:** live at app.sparrowai.eu. Meta has approved the app's WhatsApp permissions and Google has verified the OAuth app (Calendar scopes).

## What's inside

| Path | What it does |
|---|---|
| `app/engine.py` | The whole flow: intake → first message → AI turn → booking → follow-ups, reminders, handover, alerts |
| `app/guards.py` | Hard rules (`can_contact`, allowed hours, 24h window). Every message passes here |
| `app/prompt.py` | The seller conversation script (system prompt), in Spanish, French and English |
| `app/llm.py` | AI tool loop (Azure OpenAI by default; OpenAI or Claude also supported): `get_free_slots`, `book_slot`, `handoff`, `stop`, `save_facts` |
| `app/templates.py` | ALL WhatsApp template names + texts, in Spanish, French and English |
| `app/whatsapp.py`, `app/gcal.py` | Meta Cloud API and Google Calendar clients (real + fake) |
| `app/webhooks.py` | Meta webhook: signature check, stored once, processed after |
| `app/web.py`, `app/onboarding.py`, `web_templates/onboarding.html` | The onboarding: one continuous chat with a named, openly-AI guide (Google Calendar → name, agency, zones, preferences, mobile → preview of the first message with a real test event in the calendar → optional payment (off by default: no setup fee) → WhatsApp Embedded Signup with Coexistence). Scripted, so it costs no AI tokens. Agents can change any answer ("Mis datos") or ask the guide questions at any time |
| `app/i18n.py`, `app/i18n_onb.py` | Languages for agents: Spanish (default), English and French. The language comes from `?lang=`, then a cookie, then the browser. Onboarding texts use the Spanish text as the key; English and French live in `i18n_onb.py`, and a test fails if one is missing. This is the screen language only. What an agent's owners receive (templates, the chat, dates, opt-out word) follows the agent's lead language (`account.settings["lang"]`: es, fr or en), see `app/lang.py` |
| `app/helpchat.py` | Capped AI help inside the onboarding (questions, screenshots, change proposals the agent must confirm) |
| `app/transcribe.py` | Voice-note transcription for leads (needs an Azure transcription deployment) |
| `app/admin.py` | Ops console at `/admin` (also used for the App Review videos) |
| `app/casafari.py`, `app/prospecting.py` | Prospecting (off by default): private-seller listings with a phone from CASAFARI for one zone become ordinary leads (Spanish phones only, filters, de-duplication across portals, daily cap), then the normal flow and guards decide. `scripts/casafari_yield.py` estimates leads and cost per area without touching the DB |
| `app/jobs.py` | Job queue in Postgres (the worker runs inside the web process) |
| `tests/` | 104 tests covering guardrails G1–G14, onboarding, the full flow and prospecting |

## Run locally (5 min, no accounts needed)

```bash
pip install -r requirements.txt
pytest -q                                   # all green
python scripts/chat_demo.py                 # chat as a seller (scripted AI)
AZURE_OPENAI_ENDPOINT=... AZURE_OPENAI_KEY=... python scripts/chat_demo.py   # chat with the real AI
WHATSAPP_MODE=fake CALENDAR_MODE=fake LLM_MODE=fake uvicorn app.main:app --reload
# → http://localhost:8000 (onboarding) and http://localhost:8000/admin (admin / change-me)
```

## Deploy (Railway)

Production runs as one Docker service on Railway (web server and job worker in the same process) plus a Railway Postgres. The AI runs on Azure OpenAI (Claude also works with `LLM_PROVIDER=anthropic`).

1. **GitHub:** push to the repo. Railway deploys every push to `main` (a second "preview" service can deploy a branch with `WHATSAPP_MODE=fake CALENDAR_MODE=fake LLM_MODE=fake`).
2. **Railway:** New project → deploy from the GitHub repo (the `Dockerfile` is used) → add a **PostgreSQL** service and reference its URL as `DATABASE_URL`. Tables are created on start.
3. **Variables:** everything in `.env.example`. Secrets only go into Railway, never into the repo. Per-agent tokens are stored encrypted (`app/crypto.py`).
4. **Domain:** add a custom domain (for example `app.<yourdomain>`) and set `BASE_URL` to it. Open `BASE_URL/health` → `{"ok": true}`.
5. The Railway region is US; mention it in the privacy policy (it does).

## Meta setup

1. **App:** developers.facebook.com → Create app → type **Business** → add **WhatsApp** → link the Atlas business portfolio.
2. **Test number:** WhatsApp → API Setup. Copy the phone number ID, WABA ID and token into `WA_*`. Add your phone as a recipient.
3. **Webhook:** WhatsApp → Configuration → Callback URL `BASE_URL/webhooks/meta`, verify token = `META_VERIFY_TOKEN`. Subscribe to these fields:
   - `messages`
   - `smb_message_echoes`
   - `history`
   - `smb_app_state_sync`
   - `message_template_status_update`
4. **Embedded Signup:** Facebook Login for Business → Configurations → create one from the **WhatsApp Embedded Signup** template. Its ID is `META_CONFIG_ID`. Then, in Facebook Login for Business → Settings:
   - Allowed domains for the JavaScript SDK: add `BASE_URL`.
   - Valid OAuth redirect URIs: add `BASE_URL/`.

   (HTTPS only. The app must be set to **Live** for agents without a role on the app to complete the sign-up.)
5. **App settings → Basic:**
   - Privacy policy URL = `BASE_URL/privacidad`
   - Terms = `BASE_URL/terminos`
   - Data deletion = `BASE_URL/eliminar-datos`
   - Add an app icon and a category.

## Google setup

1. Create a Google Cloud project, then enable the **Google Calendar API**.
2. Google Auth Platform → Data Access: the scopes in the Console must match exactly what the code requests (`app/gcal.py`, `SCOPES`): `openid`, `email`, `profile`, `calendar.events` and `calendar.freebusy`. Do not add the broad `calendar` scope; Google rejects a mismatch.

   Then click **Publish app** ("In production"). In "Testing" mode, Google expires refresh tokens after 7 days and the agent's calendar would silently disconnect. The app is verified, so there is no warning screen and no user cap. (Verification needed a demo video, the privacy policy at `/privacidad`, scope justifications and a test account.)
3. Credentials → OAuth client ID (Web). Redirect URI: `BASE_URL/oauth/google/callback`. Copy the client ID and secret into `GOOGLE_*`.

## First real test (test number, your own phones)

1. In `/admin`, create an agent (your name, your second phone for alerts).
2. Click **Usar el número de prueba de Meta**, then **Crear plantillas en Meta**. Wait for `lead_first_contact_v2` to show APPROVED (minutes to hours).
3. Connect Google: open the onboarding link shown on the agent page and do the Google step.
4. **Lead de prueba** with your verified phone → the template arrives in ~30s → reply → the AI chats → pick a slot → the event appears in your calendar.

## App Review videos (Tech Provider)

Record the screen and your phone side by side, in English or with subtitles:
1. **whatsapp_business_messaging:** in `/admin` → agent → "Enviar un mensaje" (hello_world) → show it arriving on the phone. Then a test lead: template → reply → AI answer.
2. **whatsapp_business_management:** click "Crear plantillas en Meta" → show them in WhatsApp Manager → Message templates.
3. (If asked) **Embedded Signup:** open the onboarding link → "Conectar WhatsApp" → finish the popup.

These videos were used for the approved permissions (`whatsapp_business_messaging`, `whatsapp_business_management`, `public_profile`).

## Known gaps / next

- **Payment (optional):** set `STRIPE_PAYMENT_LINK` (a Stripe Payment Link) and `STRIPE_WEBHOOK_SECRET` (webhook → `/webhooks/stripe`, event `checkout.session.completed`). Without the link the payment step is skipped (free pilots). Prices shown in the chat: `INTEGRATION_FEE_EUR` (default 0 = no setup fee) and `MEETING_FEE_EUR` (30).
- **Help button:** set `OPS_WHATSAPP` to your mobile and submit the Sparrow templates from `/admin`: "Hablar con una persona" (call or message) then WhatsApps you who wants what, and where they are. Requests also show at the top of `/admin`.
- **Onboarding guide name:** `ASSISTANT_NAME` (default Lucía).
- **Lystos:** `/intake/{key}?source=lystos` accepts a generic JSON lead (English or Spanish field names). Write the real adapter in `app/intake.py` once we see their API.
- **Echo handover (J3/J5):** can only be tested end to end with Coexistence (a real agent number after Tech Provider approval).
- **Resume command:** "Sigue con <nombre>" sent to the Sparrow number.
- **Allowed hours:** from settings (Mon–Sat 9–21), not yet from Google Calendar working hours.
- **Billing:** count `meeting_booked` events per agent per month (see `/admin`).
- **Onboarding help chat:** capped by `AI_MAX_HELP_TURNS` (default 40 per agent); uses the same Azure deployment.
- **Voice dictation** in the onboarding uses the browser's speech recognition (hidden on iPhone and Firefox). The privacy policy says so.
- **Website → app:** `POST /start` accepts `agency`, `zones`, `name`, `phone` from the website demo and pre-fills the onboarding.
- The legal pages are drafts: have them reviewed before paid clients.
