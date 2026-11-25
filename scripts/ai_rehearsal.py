"""Rehearse owner conversations through the REAL engine with the REAL model (WhatsApp and calendar stay fake).

Nothing is sent to anyone: the WhatsApp client and Google Calendar are fakes; only the AI model is real (LLM_MODE=live).
Prints each conversation (owner message -> what the assistant would send, plus tool calls) so a human can judge quality.

Usage (env must hold the model settings, e.g. LLM_PROVIDER=azure_openai AZURE_OPENAI_ENDPOINT=... AZURE_OPENAI_KEY=...):
    python scripts/ai_rehearsal.py                 # all scenarios
    python scripts/ai_rehearsal.py es_normal       # one scenario
    python scripts/ai_rehearsal.py --list
Add --fake-llm to dry-run the harness without calling any model. Output goes to stdout; phones are fake test numbers.
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

FAKE_LLM = "--fake-llm" in sys.argv
os.environ.setdefault("WHATSAPP_MODE", "fake")
os.environ.setdefault("CALENDAR_MODE", "fake")
os.environ["WHATSAPP_MODE"] = "fake"
os.environ["CALENDAR_MODE"] = "fake"
os.environ["LLM_MODE"] = "fake" if FAKE_LLM else "live"
os.environ.setdefault("RUN_WORKER_IN_WEB", "false")
os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp(prefix='rehearsal-')}/r.db"
os.environ.setdefault("FIRST_MESSAGE_DELAY", "30")
os.environ.setdefault("REPLY_DELAY", "30")
os.environ.setdefault("REPLY_MAX_DELAY", "60")
os.environ.setdefault("BASE_URL", "https://app.sparrowai.eu")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app import db, engine, jobs  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.crypto import encrypt  # noqa: E402
from app.gcal import FakeCalendar  # noqa: E402
from app.models import Account, Integration, LlmTurn, Message, Lead  # noqa: E402
from app.whatsapp import FakeWhatsApp  # noqa: E402

# Tuesday 10:00 Madrid: inside allowed hours
T0 = datetime(2026, 12, 1, 9, 0, 0)

SCENARIOS = {
    "es_normal": dict(lang="es", name="Marta Gómez", phone="+34600555001", zone="Delicias", ptype="piso", turns=[
        "Hola, sí, lo tengo a la venta yo solo desde hace un mes",
        "Porque no me gusta pagar comisiones, pero no he tenido muchas visitas",
        "Está reformado hace 3 años, 3 habitaciones, 95 m2. Lo quiero vender en unos 3 meses",
        "Pues no sé, ¿cuánto vale según vosotros?",
        "Vale, ¿qué días tenéis?",
        "El primero me viene bien",
    ]),
    "es_who_are_you": dict(lang="es", name="Luis", phone="+34600555002", zone="Actur", ptype="piso", turns=[
        "¿Quién eres? ¿Eres un robot?",
        "¿De dónde has sacado mi número?",
        "Vale, pues sí sigo vendiendo. ¿Qué queréis?",
    ]),
    "es_no_agencies": dict(lang="es", name="Carmen", phone="+34600555003", zone="Delicias", ptype="piso", turns=[
        "No quiero agencias, gracias",
        "Ya os he dicho que no",
    ]),
    "es_baja": dict(lang="es", name="Pedro", phone="+34600555004", zone="Delicias", ptype="piso", turns=["¡BAJA!"]),
    "es_price_haggle": dict(lang="es", name="Ana", phone="+34600555005", zone="Delicias", ptype="piso", turns=[
        "Sí, vendo. Pido 250.000 pero el vecino vendió el suyo por 300.000",
        "Dime ya cuánto vale mi piso, sin visitas",
        "¿Y cobráis algo por esto?",
        "Mejor llamadme por teléfono mañana",
    ]),
    "es_hostile": dict(lang="es", name="Jorge", phone="+34600555006", zone="Delicias", ptype="piso", turns=[
        "Dejad de molestar o os denuncio a la AEPD, borrad mis datos",
    ]),
    "es_voice_and_odd": dict(lang="es", name="Rosa", phone="+34600555007", zone="Delicias", ptype="piso", turns=[
        "👍",
        "ok",
        "jajaja y tú quien eres exactamente",
        "Estoy en Valencia ahora, ¿podéis el sábado?",
    ]),
    "fr_normal": dict(lang="fr", name="Sophie Martin", phone="+33600555008", zone="Delicias", ptype="appartement", turns=[
        "Bonjour, oui je vends seule",
        "Je cherche à vendre dans 2 mois, 80 m2 rénové",
        "Quels jours êtes-vous disponibles ?",
        "Le premier me convient",
    ]),
    "en_normal": dict(lang="en", name="James Clark", phone="+34600555009", zone="Delicias", ptype="flat", turns=[
        "Hi yes I'm selling it myself",
        "Two bedrooms, renovated, I'd like to sell within 3 months",
        "What days can you do?",
        "First one works",
    ]),
}


def setup(lang: str) -> int:
    db.reset_engine()
    db.init_db()
    FakeWhatsApp.reset()
    FakeCalendar.reset()
    with db.session_scope() as s:
        a = Account(agent_name="Laura García", agency="RE/MAX Confort", city="Zaragoza", zones="Delicias, Actur",
                    personal_whatsapp="+34611111111", status="live", onboarding_step="done",
                    settings={"lang": lang, "lang_confirmed": True} if lang != "es" else {})
        s.add(a)
        s.flush()
        s.add(Integration(account_id=a.id, provider="meta", external={"waba_id": "w1", "phone_number_id": "pn1"}, token_enc=encrypt("t")))
        s.add(Integration(account_id=a.id, provider="google", external={"calendar_id": "primary"}, token_enc=encrypt("g")))
        return a.id


def advance(seconds: int) -> None:
    db.FROZEN_NOW = db.FROZEN_NOW + timedelta(seconds=seconds)
    jobs.run_until_idle()


def out_for(phone: str, start: int) -> list[dict]:
    to = phone.lstrip("+")
    return [m for m in FakeWhatsApp.outbox if m["to"] == to][start:]


def run(name: str, sc: dict) -> None:
    print("=" * 78)
    print(f"SCENARIO {name}  (lead language {sc['lang']})")
    print("=" * 78)
    acc = setup(sc["lang"])
    db.FROZEN_NOW = T0
    with db.session_scope() as s:
        lead, res = engine.ingest_lead(s, s.get(Account, acc), {"name": sc["name"], "phone": sc["phone"], "zone": sc["zone"],
                                                                "property_type": sc["ptype"], "source": "lystos"})
        lead_id = lead.id
    advance(31)
    seen = 0
    for m in out_for(sc["phone"], seen):
        print(f"  ASSISTANT (first message, template {m.get('template')}):\n    {m.get('text')}\n")
        seen += 1
    mid = 0
    for text in sc["turns"]:
        mid += 1
        print(f"  OWNER: {text}")
        with db.session_scope() as s:
            result = engine.handle_inbound(s, s.get(Account, acc), sc["phone"].lstrip("+"), {"id": f"{name}-{mid}", "type": "text", "text": text})
        for _ in range(6):  # burst wait + reply delay
            advance(31)
        new = out_for(sc["phone"], seen)
        for m in new:
            print(f"  ASSISTANT: {m.get('text')}")
            seen += 1
        if not new:
            print(f"  (no message sent; engine result: {result})")
        with db.session_scope() as s:
            lead = s.get(Lead, lead_id)
            if lead.do_not_contact or lead.status in ("opted_out", "handoff", "booked"):
                print(f"  -> lead status: {lead.status}{' (do not contact)' if lead.do_not_contact else ''}")
                if lead.status in ("opted_out", "handoff", "booked"):
                    break
        print()
    with db.session_scope() as s:
        lead = s.get(Lead, lead_id)
        turns = s.execute(select(LlmTurn).where(LlmTurn.lead_id == lead_id)).scalars().all()
        print(f"\n  FINAL STATUS: {lead.status}; AI turns: {len(turns)}; facts: {lead.facts}")
        for ev in list(FakeCalendar.events.values()):
            print(f"  CALENDAR EVENT: {ev.get('summary')} @ {ev.get('start')}")
    print()


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--list" in sys.argv:
        print("\n".join(SCENARIOS))
        return
    for name in args or list(SCENARIOS):
        run(name, SCENARIOS[name])


if __name__ == "__main__":
    get_settings.cache_clear()
    main()
