# Sparrow AI: MVP

A WhatsApp AI assistant for real estate agents.
- A new lead arrives → a WhatsApp from the agent's own number in 30 seconds.
- The AI chats in Spanish (sellers only) → books a valuation in the agent's Google Calendar.

Design: `sparrow-ai-design.md` (in the Claude project). Rules for Claude Code: `CLAUDE.md`.

## What's inside

| Path | What it does |
|---|---|
| `app/engine.py` | The whole flow: intake → first message → AI turn → booking → follow-ups, reminders, handover, alerts |
| `app/guards.py` | Hard rules (`can_contact`, allowed hours, 24h window). Every message passes here |
| `app/prompt.py` | The seller conversation script (system prompt) |
| `app/llm.py` | AI tool loop (Azure OpenAI by default; OpenAI or Claude also supported): `get_free_slots`, `book_slot`, `handoff`, `stop`, `save_facts` |
| `app/templates.py` | ALL WhatsApp template names + texts |
| `app/whatsapp.py`, `app/gcal.py` | Meta Cloud API and Google Calendar clients (real + fake) |
| `app/webhooks.py` | Meta webhook: signature check, stored once, processed after |
| `app/web.py` + `web_templates/onboarding.html` | The one-chat onboarding with a named, openly-AI guide (Google → 2 questions → optional €90 payment → WhatsApp Embedded Signup with Coexistence) |
| `app/admin.py` | Ops console at `/admin` (also used for the App Review videos) |
| `app/jobs.py` | Job queue in Postgres (the worker runs inside the web process) |
| `tests/` | 42 tests covering guardrails G1–G14 and the full flow |

## Run locally (5 min, no accounts needed)

```bash
pip install -r requirements.txt
pytest -q                                   # all green
python scripts/chat_demo.py                 # chat as a seller (scripted AI)
AZURE_OPENAI_ENDPOINT=... AZURE_OPENAI_KEY=... python scripts/chat_demo.py   # chat with the real AI
WHATSAPP_MODE=fake CALENDAR_MODE=fake LLM_MODE=fake uvicorn app.main:app --reload
# → http://localhost:8000 (onboarding) and http://localhost:8000/admin (admin / change-me)
```

## Deploy (Azure App Service + Supabase)

The AI runs on Azure OpenAI (covered by Microsoft for Startups credits). Claude also works (`LLM_PROVIDER=anthropic`) but Azure credits don't cover it.

1. **GitHub:** push this folder to the repo.
2. **Supabase:** Connect (top bar) → **Session pooler** URI. Replace `[YOUR-PASSWORD]`. That's `DATABASE_URL`. Tables are created automatically on start.
3. **Azure:** portal.azure.com → Create a resource → **Web App**:
   - Publish: Code · Runtime: **Python 3.11** · OS: Linux · Region: **West Europe**
   - Pricing plan: **Basic B1** (needed for "Always on": the job worker runs inside the web app)
4. Once created, in the Web App:
   - **Configuration → General settings:**
     - Startup command: `uvicorn app.main:app --host 0.0.0.0 --port 8000`
     - Always on: **On**
   - **Environment variables:** add everything from `.env.example`, plus `SCM_DO_BUILD_DURING_DEPLOYMENT=true`.
   - **Deployment Center:** Source GitHub → your repo → branch `main` → Save. Every push now deploys.
5. `BASE_URL` = `https://<app-name>.azurewebsites.net`. Open `BASE_URL/health` → `{"ok": true}`.

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

   (HTTPS only. Until App Review passes, only people with a role on the app can complete it. That's enough to test with your own WhatsApp Business.)
5. **App settings → Basic:**
   - Privacy policy URL = `BASE_URL/privacidad`
   - Terms = `BASE_URL/terminos`
   - Data deletion = `BASE_URL/eliminar-datos`
   - Add an app icon and a category.

## Google setup

1. Create a Google Cloud project, then enable the **Google Calendar API**.
2. OAuth consent screen: External. Scopes:
   - `calendar.events`
   - `calendar.freebusy`

   Then click **Publish app** ("In production"). In "Testing" mode, Google expires refresh tokens after 7 days and the agent's calendar would silently disconnect. Unverified production apps show a warning screen and are capped at 100 users, which is fine for the pilot.
3. Credentials → OAuth client ID (Web). Redirect URI: `BASE_URL/oauth/google/callback`. Copy the client ID and secret into `GOOGLE_*`.

## First real test (test number, your own phones)

1. In `/admin`, create an agent (your name, your second phone for alerts).
2. Click **Usar el número de prueba de Meta**, then **Crear plantillas en Meta**. Wait for `lead_first_contact_v1` to show APPROVED (minutes to hours).
3. Connect Google: open the onboarding link shown on the agent page and do the Google step.
4. **Lead de prueba** with your verified phone → the template arrives in ~30s → reply → the AI chats → pick a slot → the event appears in your calendar.

## App Review videos (Tech Provider)

Record the screen and your phone side by side, in English or with subtitles:
1. **whatsapp_business_messaging:** in `/admin` → agent → "Enviar un mensaje" (hello_world) → show it arriving on the phone. Then a test lead: template → reply → AI answer.
2. **whatsapp_business_management:** click "Crear plantillas en Meta" → show them in WhatsApp Manager → Message templates.
3. (If asked) **Embedded Signup:** open the onboarding link → "Conectar WhatsApp" → finish the popup.

Submit once business verification is approved.

## Known gaps / next

- **Payment (optional):** set `STRIPE_PAYMENT_LINK` (a Stripe Payment Link) and `STRIPE_WEBHOOK_SECRET` (webhook → `/webhooks/stripe`, event `checkout.session.completed`). Without the link the payment step is skipped (free pilots). Prices shown in the chat: `INTEGRATION_FEE_EUR` (90) and `MEETING_FEE_EUR` (30).
- **Help button:** set `OPS_WHATSAPP` to your mobile and submit the Sparrow templates from `/admin`: "Necesito ayuda" then WhatsApps you who is stuck and where. Requests also show at the top of `/admin`.
- **Onboarding guide name:** `ASSISTANT_NAME` (default Lucía).
- **Lystos:** `/intake/{key}?source=lystos` accepts a generic JSON lead (English or Spanish field names). Write the real adapter in `app/intake.py` once we see their API.
- **Echo handover (J3/J5):** can only be tested end to end with Coexistence (a real agent number after Tech Provider approval).
- **Resume command:** "Sigue con <nombre>" sent to the Sparrow number.
- **Allowed hours:** from settings (Mon–Sat 9–21), not yet from Google Calendar working hours.
- **Billing:** count `meeting_booked` events per agent per month (see `/admin`).
- The legal pages are drafts: have them reviewed before paid clients.
