"""Talk to the AI as if you were a seller, in your terminal. No Meta, no Google needed.

    AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com AZURE_OPENAI_KEY=... python scripts/chat_demo.py   # real AI
    python scripts/chat_demo.py                                  # scripted fake answers

WhatsApp and Google Calendar are simulated; the conversation engine, prompt, tools and guardrails are the real ones.
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///./demo.db")
os.environ["WHATSAPP_MODE"] = "fake"
os.environ["CALENDAR_MODE"] = "fake"
os.environ["LLM_MODE"] = "live" if (os.environ.get("AZURE_OPENAI_KEY") or os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")) else "fake"
os.environ.setdefault("SPARROW_PHONE_NUMBER_ID", "sparrow")

import logging  # noqa: E402

logging.disable(logging.INFO)

from app import db, engine, jobs, onboarding  # noqa: E402,F401
from app.gcal import FakeCalendar  # noqa: E402
from app.models import Account, Lead  # noqa: E402
from app.whatsapp import FakeWhatsApp  # noqa: E402

if os.path.exists("demo.db"):
    os.remove("demo.db")
db.init_db()

# Freeze the clock on the next weekday at 10:00 Madrid so allowed hours apply
now = datetime.utcnow()
d = now + timedelta(days=1)
while d.weekday() > 4:
    d += timedelta(days=1)
db.FROZEN_NOW = d.replace(hour=8, minute=0, second=0, microsecond=0)
FakeCalendar.busy_blocks = [(db.FROZEN_NOW + timedelta(hours=6), db.FROZEN_NOW + timedelta(hours=8))]  # a busy afternoon

name = input("Nombre del lead [Marta Gómez]: ").strip() or "Marta Gómez"
with db.session_scope() as s:
    acc = Account(agent_name="Laura García", agency="RE/MAX Confort", city="Zaragoza", zones="Delicias, Actur",
                  personal_whatsapp="+34611111111", status="live")
    s.add(acc)
    s.flush()
    lead, _ = engine.ingest_lead(s, acc, {"name": name, "phone": "+34600000001", "zone": "Delicias", "property_type": "piso",
                                          "price": 185000, "source": "lystos"})
    acc_id, lead_id = acc.id, lead.id

shown = 0


def tick():
    global shown
    db.FROZEN_NOW += timedelta(seconds=31)
    jobs.run_until_idle()
    for m in FakeWhatsApp.outbox[shown:]:
        who = "SPARROW → agente" if m["from"] == "sparrow" else "Asistente"
        print(f"\n\033[92m{who}:\033[0m {m['text']}")
    shown = len(FakeWhatsApp.outbox)


print(f"\n(Modo IA: {os.environ['LLM_MODE']}. Escribe como el propietario. 'salir' para terminar.)")
tick()
i = 0
while True:
    try:
        text = input("\n\033[94mTú:\033[0m ").strip()
    except EOFError:
        break
    if text.lower() in ("salir", "exit", "quit"):
        break
    i += 1
    with db.session_scope() as s:
        engine.handle_inbound(s, s.get(Account, acc_id), "34600000001", {"id": f"demo{i}", "type": "text", "text": text})
    tick()
    with db.session_scope() as s:
        lead = s.get(Lead, lead_id)
        status, facts = lead.status, lead.facts
    print(f"\033[90m  [estado: {status} · datos: {facts}]\033[0m")
    if status == "booked":
        for e in FakeCalendar.events.values():
            print(f"\n\033[93m📅 Evento en el calendario de Laura:\033[0m {e['summary']}\n{e['description']}")
        break
    if status in ("handoff", "opted_out", "not_interested"):
        break
