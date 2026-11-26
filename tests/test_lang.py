"""Lead-facing language: a French agent's owners get French (templates, chat, dates, opt-out), Spanish stays as it was."""
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import db, engine, lang as L, templates as T
from app.crypto import encrypt
from app.gcal import FakeCalendar
from app.llm import FakeBackend
from app.models import Account, Integration, Lead, Meeting
from app.whatsapp import FakeWhatsApp
from tests.conftest import run_jobs, sent
from tests.test_flow import ingest, inbound, lead_of

FR_LEAD = {"name": "Marie Dupont", "phone": "06 12 34 56 78", "zone": "Lyon 3e", "property_type": "appartement", "source": "portail"}
P = "+33612345678"


@pytest.fixture
def fr_account():
    with db.session_scope() as s:
        a = Account(agent_name="Laura Martin", agency="Agence Soleil", city="Lyon", zones="Lyon 3e, Lyon 6e", personal_whatsapp="+33611111111",
                    status="live", onboarding_step="done", timezone="Europe/Paris", settings={"lang": "fr"})
        s.add(a)
        s.flush()
        s.add(Integration(account_id=a.id, provider="meta", external={"waba_id": "w1", "phone_number_id": "pn1"}, token_enc=encrypt("t")))
        s.add(Integration(account_id=a.id, provider="google", external={"calendar_id": "primary"}, token_enc=encrypt("g")))
        return a.id


def _start(clock, account):
    lid, res = ingest(account, FR_LEAD)
    assert res == "queued"
    clock.advance(seconds=31)
    run_jobs()
    return lid


def test_french_first_message_is_the_french_template_with_stop(clock, fr_account):
    lid = _start(clock, fr_account)
    out = sent(P)
    assert len(out) == 1 and out[0]["template"] == "lead_first_contact_v2" and out[0]["language"] == "fr"
    assert out[0]["text"].startswith("Bonjour Marie, je suis l'assistant virtuel de Laura (Agence Soleil)")  # G13 in French, first name only
    assert "répondez STOP" in out[0]["text"] and "BAJA" not in out[0]["text"]
    assert lead_of(lid).phone == P  # a French national number gets +33, not +34


def test_spanish_agents_are_unchanged(clock, account):
    lid, _ = ingest(account)
    clock.advance(seconds=31)
    run_jobs()
    out = sent("+34600000001")
    assert out[0]["language"] == "es" and "Hola Marta" in out[0]["text"] and "responde BAJA" in out[0]["text"]


@pytest.mark.parametrize("word", ["STOP", "arrêt", "Désabonner"])
def test_french_opt_out_words_stop_everything_in_french(clock, fr_account, word):
    lid = _start(clock, fr_account)
    assert inbound(fr_account, word, "in1", phone=P) == "opted_out"
    assert sent(P)[-1]["text"].startswith("C'est bien noté, nous ne vous écrirons plus")
    assert lead_of(lid).do_not_contact


def test_a_plain_non_after_the_second_follow_up_is_an_opt_out_in_french(clock, fr_account):
    lid = _start(clock, fr_account)
    clock.advance(days=4)
    run_jobs()
    assert any("répondez « non »" in m["text"] for m in sent(P))
    assert inbound(fr_account, "Non merci", "in1", phone=P) == "opted_out"


def test_the_ai_speaks_french_and_introduces_itself_in_french(clock, fr_account):
    lid = _start(clock, fr_account)
    FakeBackend.push({"text": "Parfait, merci. Quel créneau vous convient ?"})
    inbound(fr_account, "Oui, c'est rénové", "in1", phone=P)
    clock.advance(seconds=31)
    run_jobs()
    assert sent(P)[-1]["text"].startswith("Parfait")
    with db.session_scope() as s:
        a = s.get(Account, fr_account)
        from app.prompt import build_system_prompt

        prompt = build_system_prompt(a, s.get(Lead, lid), must_introduce=True)
    assert "assistant IA" in prompt and "Vouvoiement" in prompt and "heure de France" in prompt and "STOP" in prompt


def test_g13_french_intro_is_added_when_the_model_forgets(clock, fr_account):
    with db.session_scope() as s:
        a, (lead, _) = s.get(Account, fr_account), engine.ingest_lead(s, s.get(Account, fr_account), dict(FR_LEAD))
        lead.status, lead.introduced = "in_conversation", False
        lead.last_inbound_at, lead.has_replied = db.utcnow(), True
        lid = lead.id
    FakeBackend.push({"text": "Bonjour ! Quel créneau vous convient ?"})
    inbound(fr_account, "Bonjour", "in1", phone=P)
    clock.advance(seconds=31)
    run_jobs()
    assert sent(P)[-1]["text"].startswith("Je suis l'assistant IA de Laura. ")


def test_slots_dates_and_the_calendar_event_are_in_french(clock, fr_account):
    lid = _start(clock, fr_account)
    FakeBackend.push({"tool": "get_free_slots"}, {"text": "Quel créneau ?"})
    inbound(fr_account, "Oui", "in1", phone=P)
    clock.advance(seconds=31)
    run_jobs()
    lead = lead_of(lid)
    slot = lead.offered_slots[-1]
    with db.session_scope() as s:
        labels = [x["label"] for x in engine.compute_free_slots(s, s.get(Account, fr_account))]
    assert all(re.match(r"^(lundi|mardi|mercredi|jeudi|vendredi|samedi) \d+ \w+ à \d{2}h\d{2}$", x) for x in labels), labels
    FakeBackend.push({"tool": "book_slot", "input": {"slot_id": slot, "meeting_type": "visit", "address": "12 rue de la République"}}, {"text": "C'est noté ✅"})
    inbound(fr_account, "Le dernier", "in2", phone=P)
    clock.advance(seconds=31)
    run_jobs()
    with db.session_scope() as s:
        m = s.execute(select(Meeting).where(Meeting.lead_id == lid)).scalar_one()
        ev = FakeCalendar.events[m.google_event_id]
        assert ev["summary"].startswith("Estimation · Marie D.") and "Booked" not in ev["description"] and "Planifié par Sparrow AI" in ev["description"]
        assert "Type: visite" in ev["description"] and "Origine: portail" in ev["description"]


def test_french_reminder_uses_the_french_template_and_hour_format(clock, fr_account):
    lid = _start(clock, fr_account)
    with db.session_scope() as s:
        lead = s.get(Lead, lid)
        start = db.utcnow() + __import__("datetime").timedelta(days=3)
        ev_id = FakeCalendar().create_event(summary="x", description="", location="", start_utc=start, end_utc=start, timezone="Europe/Paris", request_id="r")
        s.add(Meeting(lead_id=lid, account_id=fr_account, start=start, end=start, type="visit", address="12 rue de la République", google_event_id=ev_id))
        lead.status = "booked"
        engine.jobs.schedule(s, "reminder", db.utcnow(), lead_id=lid, account_id=fr_account, dedupe_key="rem", payload={"which": "24h"})
    run_jobs()
    txt = sent(P)[-1]["text"]
    assert txt.startswith("Bonjour Marie, petit rappel") and re.search(r"à \d{2}h\d{2}", txt) and "à 12 rue de la République" in txt


def test_every_template_exists_in_every_language_and_follows_meta_rules():
    for tpl in T.AGENT_TEMPLATES + T.SPARROW_TEMPLATES:
        langs = ("es",) if tpl is T.OPS_NEEDS_HELP else ("es", "fr", "en")  # the operator's template stays Spanish
        want = sorted(re.findall(r"\{\{\d\}\}", tpl.body))
        for lg in langs:
            body = tpl.body_for(lg)
            assert sorted(re.findall(r"\{\{\d\}\}", body)) == want, (tpl.name, lg)
            assert len(tpl.example_for(lg)) == len(tpl.params), (tpl.name, lg)
            assert not body.startswith("{{") and not body.rstrip().rstrip(".").endswith("}}"), (tpl.name, lg)  # Meta: no variable at the edges
            order = re.findall(r"\{\{(\d)\}\}", body)
            assert order == sorted(order), (tpl.name, lg, "variables must appear in order")
    assert "STOP" in T.LEAD_FIRST_CONTACT.body_for("fr") and "STOP" in T.LEAD_FIRST_CONTACT.body_for("en") and "BAJA" in T.LEAD_FIRST_CONTACT.body_for("es")


def test_templates_are_submitted_in_the_agents_language_and_the_account_goes_live_on_that_one(clock):
    from app.onboarding import advance, connect_meta, job_check_templates  # noqa: F401

    with db.session_scope() as s:
        a = Account(agent_name="Laura Martin", agency="Agence Soleil", zones="Lyon", personal_whatsapp="+33611111111", settings={"lang": "fr"}, timezone="Europe/Paris")
        s.add(a)
        s.flush()
        connect_meta(s, a, code="c", waba_id="w9", phone_number_id="pn9")
        aid = a.id
    assert {t["language"] for t in FakeWhatsApp.created_templates} == {"fr"} and len(FakeWhatsApp.created_templates) == 6
    clock.advance(minutes=2)
    run_jobs()
    with db.session_scope() as s:
        assert s.get(Account, aid).status == "live"
    # a template approved only in Spanish does not make a French account live
    FakeWhatsApp.created_templates[:] = [dict(t, language="es") for t in FakeWhatsApp.created_templates]
    with db.session_scope() as s:
        s.get(Account, aid).status = "onboarding"
    clock.advance(minutes=20)
    run_jobs()
    with db.session_scope() as s:
        assert s.get(Account, aid).status == "onboarding"


def test_phone_numbers_by_country():
    from app.guards import normalize_phone as n

    assert n("06 12 34 56 78", "33") == "+33612345678" == n("+33 6 12 34 56 78") == n("33612345678")
    assert n("611111111") == "+34611111111" and n("+3361234567") is None and n("123") is None


def test_french_agent_alerts_and_resume_command(clock, fr_account):
    lid = _start(clock, fr_account)
    FakeBackend.push({"tool": "handoff", "input": {"reason": "demande les honoraires"}})
    inbound(fr_account, "Quels sont vos honoraires ?", "in1", phone=P)
    clock.advance(seconds=31)
    run_jobs()
    alert = [m for m in sent("+33611111111") if m["from"] == "sparrow"][-1]
    assert alert["language"] == "fr" and alert["text"].startswith("Bonjour Laura, un prospect a besoin de vous")
    with db.session_scope() as s:
        engine.handle_sparrow_inbound(s, "33611111111", {"id": "a1", "type": "text", "text": "Reprends avec Marie"})
    assert [m for m in sent("+33611111111") if m["from"] == "sparrow"][-1]["text"].startswith("C'est fait, je reprends avec Marie")
    assert not lead_of(lid).human_owned


def test_onboarding_picks_the_owners_language_and_country(clock):
    from app.main import app

    with TestClient(app) as c:
        token = c.post("/start?lang=fr", data={}, follow_redirects=False).headers["location"].split("/")[-1].split("?")[0]
        st = c.get(f"/api/o/{token}").json()
        assert st["lead_lang"] == "fr" and st["cc"] == "33"
        assert c.post(f"/api/o/{token}/answers", json={"personal_whatsapp": "06 12 34 56 78"}).json()["answers"]["personal_whatsapp"] == "+33612345678"
        r = c.post(f"/api/o/{token}/answers", json={"agent_name": "Laura Martin", "lead_lang": "en"}).json()
        assert r["lead_lang"] == "en" and r["first_message"].startswith("Hi Emma, I'm writing on behalf of Laura")
        assert c.post(f"/api/o/{token}/answers", json={"lead_lang": "xx"}).json()["lead_lang"] == "en"  # unknown languages are ignored


def test_intake_understands_french_field_names():
    from app.intake import normalize

    d = normalize({"nom": "Marie Dupont", "téléphone": "06 12 34 56 78", "ville": "Lyon 3e", "prix": "350 000 €", "annonce": "https://x.fr/1", "type": "vendeur"}, "portail")
    assert d["name"] == "Marie Dupont" and d["phone"] == "06 12 34 56 78" and d["zone"] == "Lyon 3e" and d["price"] == 350000.0 and d["lead_type"] == "seller"
