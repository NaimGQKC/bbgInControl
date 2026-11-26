"""Cold first contact to private owners found through prospecting (CASAFARI): the agent's own template, the cold
prompt block, and the soft-no / hard-stop ladder enforced in code. Fake WhatsApp / LLM, frozen clock."""
import re
from sqlalchemy import select

from app import coldmsg, db, engine, jobs, onboarding, templates as T
from app.llm import FakeBackend
from app.models import Account, Event, Job, Lead
from app.prompt import build_system_prompt
from app.whatsapp import FakeWhatsApp
from tests.conftest import run_jobs, sent

BODY = "Hola {nombre}, soy el asistente virtual de {agente} ({agencia}). He visto tu {tipo} en {zona}. ¿Sigues vendiéndolo tú?"
PITCH = "Laura lleva 12 años vendiendo en Delicias."
OWNER = "+34600200001"
SOURCE = {"provider": "casafari", "listing_id": "psl_secret_42", "portal": "Idealista", "expires": "2027-09-29"}


def configure(account_id, lang="es", **kw):
    with db.session_scope() as s:
        a = s.get(Account, account_id)
        settings = {**(a.settings or {}), "lang": lang}
        settings["prospecting"] = {**settings.get("prospecting", {}), "cold_body": BODY, **kw}
        a.settings = settings


def cfg(account_id):
    with db.session_scope() as s:
        return engine.prospecting_cfg(s.get(Account, account_id))


def submit(account_id):
    with db.session_scope() as s:
        return onboarding.submit_cold_template(s, s.get(Account, account_id))


def check(account_id):
    with db.session_scope() as s:
        job = jobs.schedule(s, "check_templates", db.utcnow(), account_id=account_id, dedupe_key=f"check_templates:{account_id}", reuse=True, replace_run_at=True)
        job.status = "pending"
    run_jobs()


def approved(account_id, lang="es", **kw):
    configure(account_id, lang=lang, **kw)
    submit(account_id)
    check(account_id)  # the fake Meta approves everything it was given


def add_cold_lead(account_id, name="marta pérez", phone=OWNER, ptype="piso", zone="Delicias"):
    with db.session_scope() as s:
        lead, res = engine.ingest_lead(s, s.get(Account, account_id), {"name": name, "phone": phone, "source": "casafari",
                                                                         "property_type": ptype, "zone": zone, "price": 185000})
        lead.facts = {**(lead.facts or {}), "_source": dict(SOURCE)}
        return lead.id


def get_lead(lead_id):
    with db.session_scope() as s:
        return s.get(Lead, lead_id)


_n = iter(range(1, 10_000))


def reply(account_id, text, phone=OWNER):
    with db.session_scope() as s:
        return engine.handle_inbound(s, s.get(Account, account_id), phone, {"id": f"wamid.in.{next(_n)}", "type": "text", "text": text})


def contacted(account_id, clock, **kw):
    approved(account_id, **kw)
    lead_id = add_cold_lead(account_id)
    clock.advance(seconds=31)
    run_jobs()
    return lead_id


def events(account_id, name):
    with db.session_scope() as s:
        return s.execute(select(Event).where(Event.account_id == account_id, Event.name == name)).scalars().all()


# ------------------------------------------------------------------ the template

def test_template_is_the_agents_text_plus_the_footer():
    tpl = T.cold_first_contact(3, "es", BODY, "https://sparrow.test/privacidad#propietarios")
    assert tpl.name == "cold_first_contact_v3" and tpl.category == "MARKETING"
    text = tpl.body_for("es")
    assert text.startswith("Hola {{1}}, soy el asistente virtual de {{4}} ({{5}}). He visto tu {{2}} en {{3}}.")
    assert "responde BAJA" in text and text.endswith("https://sparrow.test/privacidad#propietarios")
    assert tpl.params == tuple(coldmsg.used_fields(BODY)) == ("nombre", "tipo", "zona", "agente", "agencia")
    assert tpl.example_for("es") == ("Marta", "piso", "Zaragoza", "Laura", "Inmobiliaria Sur")
    assert "{" not in tpl.render(["Marta", "piso", "Delicias", "Laura", "RE/MAX"], "es").replace("{{", "")


def test_submit_then_approval_then_new_version(account):
    configure(account)
    r = submit(account)
    assert r["ok"] and r["name"] == "cold_first_contact_v1"
    assert FakeWhatsApp.created_templates[-1] == {"name": "cold_first_contact_v1", "category": "MARKETING", "language": "es"}
    assert cfg(account)["cold_template_status"] == "pending" and cfg(account)["cold_version"] == 1
    check(account)
    assert cfg(account)["cold_template_status"] == "approved"
    assert submit(account)["status"] == "unchanged" and len(FakeWhatsApp.created_templates) == 1  # same text: nothing new
    configure(account, cold_body=BODY.replace("¿Sigues", "¿Lo sigues"))
    assert submit(account)["name"] == "cold_first_contact_v2"
    assert cfg(account)["cold_template_status"] == "pending"


def test_invalid_text_is_never_submitted(account):
    configure(account, cold_body="{nombre} mira www.example.com {zona}")
    assert submit(account)["ok"] is False
    assert FakeWhatsApp.created_templates == [] and "cold_template_status" not in cfg(account)


def test_go_live_does_not_wait_for_the_cold_template_and_rejection_alerts(account, monkeypatch):
    with db.session_scope() as s:
        a = s.get(Account, account)
        a.status, a.onboarding_step = "onboarding", "whatsapp"
    configure(account)
    with db.session_scope() as s:
        onboarding.connect_meta(s, s.get(Account, account), code="c", waba_id="w1")
    names = [t["name"] for t in FakeWhatsApp.created_templates]
    assert "cold_first_contact_v1" in names and T.LEAD_FIRST_CONTACT_V3.name in names
    verdict = {"cold_first_contact_v1": "PENDING"}
    monkeypatch.setattr(FakeWhatsApp, "list_templates", lambda self: [
        {"name": t["name"], "language": t["language"], "category": t["category"], "status": verdict.get(t["name"], "APPROVED"),
         "rejected_reason": "PROMOTIONAL" if verdict.get(t["name"]) == "REJECTED" else None} for t in FakeWhatsApp.created_templates])
    check(account)
    with db.session_scope() as s:
        assert s.get(Account, account).status == "live"  # the first-contact template alone decides
        job = s.execute(select(Job).where(Job.dedupe_key == f"check_templates:{account}")).scalar_one()
        assert job.status == "pending"  # still watching the cold one
    assert cfg(account)["cold_template_status"] == "pending"
    verdict["cold_first_contact_v1"] = "REJECTED"
    check(account)
    assert cfg(account)["cold_template_status"] == "rejected" and cfg(account)["cold_template_reason"] == "PROMOTIONAL"
    alerts = [m for m in sent("+34611111111") if "rechazó tu primer mensaje a propietarios" in m["text"]]
    assert len(alerts) == 1 and "PROMOTIONAL" in alerts[0]["text"]
    check(account)  # no new alert for the same verdict
    assert len([m for m in sent("+34611111111") if "rechazó tu primer mensaje" in m["text"]]) == 1


# ------------------------------------------------------------------ first contact

def test_cold_lead_waits_until_approved_then_gets_the_agents_template(account, clock):
    configure(account)
    submit(account)  # pending at Meta
    lead_id = add_cold_lead(account)
    clock.advance(seconds=31)
    run_jobs()
    assert sent(OWNER) == [] and get_lead(lead_id).status == "queued"
    check(account)  # approved → the waiting lead is released
    clock.advance(minutes=1)
    run_jobs()
    out = sent(OWNER)
    assert len(out) == 1 and out[0]["template"] == "cold_first_contact_v1"
    assert out[0]["text"].startswith("Hola Marta, soy el asistente virtual de Laura (RE/MAX Confort). He visto tu piso en Delicias.")
    assert "responde BAJA" in out[0]["text"]
    lead = get_lead(lead_id)
    assert lead.status == "contacted" and lead.introduced
    with db.session_scope() as s:  # one message only: no follow-ups to someone who never asked
        assert not s.execute(select(Job).where(Job.lead_id == lead_id, Job.type == "followup")).scalars().all()


def test_rejected_template_never_sends(account, clock):
    configure(account)
    submit(account)
    with db.session_scope() as s:
        a = s.get(Account, account)
        engine.set_prospecting(a, cold_template_status="rejected")
    add_cold_lead(account)
    clock.advance(seconds=31)
    run_jobs()
    assert sent(OWNER) == []


def test_other_leads_keep_the_normal_first_message(account, clock):
    approved(account)
    with db.session_scope() as s:
        engine.ingest_lead(s, s.get(Account, account), {"name": "Ana", "phone": "+34600200009", "source": "lystos", "zone": "Actur"})
    clock.advance(seconds=31)
    run_jobs()
    out = sent("+34600200009")
    assert len(out) == 1 and out[0]["template"] == T.LEAD_FIRST_CONTACT.name


def test_no_name_neutral_greeting_and_type_in_the_lead_language(account, clock):
    approved(account, lang="en", cold_body=coldmsg.default_body("en"))
    add_cold_lead(account, name="", ptype="piso")
    clock.advance(seconds=31)
    run_jobs()
    text = sent(OWNER)[0]["text"]
    assert text.startswith("Hi there, I'm the virtual assistant of Laura") and "listing for a flat in Delicias" in text
    assert "reply STOP" in text


def test_the_footer_introduces_the_assistant_even_if_the_agents_text_does_not(account, clock):  # G13
    lead_id = contacted(account, clock, cold_body="Hola {nombre}, te escribo por tu {tipo} en {zona}. ¿Sigues vendiéndolo?")
    assert "asistente virtual de Laura" in sent(OWNER)[0]["text"]
    assert get_lead(lead_id).introduced


def test_preview_is_the_sample_with_the_footer(account):
    configure(account)
    with db.session_scope() as s:
        text = engine.preview_cold_message(s.get(Account, account))
    assert text.startswith("Hola Marta, soy el asistente virtual de Laura (RE/MAX Confort). He visto tu piso en Zaragoza.")
    assert "responde BAJA" in text and "/privacidad" in text


# ------------------------------------------------------------------ the cold prompt

def test_cold_prompt_in_three_languages_keeps_g13_and_hides_the_source(account):
    lead_id = add_cold_lead(account)
    marker = {"es": "asistente de inteligencia artificial de Laura", "fr": "assistant IA de Laura", "en": "AI assistant of Laura"}
    header = {"es": "CONTACTADO EN FRÍO", "fr": "CONTACTÉ À FROID", "en": "CONTACTED COLD"}
    for lang in ("es", "fr", "en"):
        with db.session_scope() as s:
            a = s.get(Account, account)
            a.settings = {**(a.settings or {}), "lang": lang}
            lead = s.get(Lead, lead_id)
            lead.facts = {**lead.facts, "reason": "traslado"}
            p = build_system_prompt(a, lead, must_introduce=True)
        cold = p[p.index(header[lang]):]
        assert marker[lang] in cold and "[privacy]" in cold and "[threat]" in cold
        assert "psl_secret_42" not in p and "_source" not in p and "casafari" not in p.lower() and "Idealista" not in p
        assert "traslado" in p
        assert p.rstrip().endswith(("explicaciones).", "explications).", "explanations)."))


def test_normal_leads_get_no_cold_block(account):
    with db.session_scope() as s:
        lead, _ = engine.ingest_lead(s, s.get(Account, account), {"name": "Ana", "phone": "+34600200009", "source": "lystos"})
        assert "CONTACTADO EN FRÍO" not in build_system_prompt(s.get(Account, account), lead, must_introduce=True)


# ------------------------------------------------------------------ soft no: one pitch, then the end

def test_soft_no_gets_exactly_one_pitch_then_the_second_no_ends_it(account, clock):
    contacted(account, clock, success_pitch=PITCH)
    assert reply(account, "No gracias") == "soft_no_pitch"
    pitch = sent(OWNER)[-1]["text"]
    assert pitch == f"Entendido, gracias por contestarme. {PITCH}"
    # second no in other words: the model can only call stop; the code ends it, the model's text is never sent
    reply(account, "Que no, que lo vendo yo")
    FakeBackend.push({"text": "Vendemos el 98% de las casas en 10 días"}, {"tool": "stop", "input": {"reason": "not_interested"}})
    clock.advance(seconds=61)
    run_jobs()
    out = sent(OWNER)
    assert out[-1]["text"] == "Entendido, no te escribiremos más. Un saludo." and not any("98%" in m["text"] for m in out)
    n = len(out)
    assert reply(account, "Hola?") == "stored_only"
    clock.advance(minutes=5)
    run_jobs()
    assert len(sent(OWNER)) == n and len(FakeBackend.calls) == 1


def test_soft_no_through_the_model_uses_our_text_not_the_models(account, clock):
    contacted(account, clock)  # no success_pitch
    reply(account, "No quiero agencias, gracias")
    FakeBackend.push({"text": "¡Somos los número 1 con un 95% de éxito!"}, {"tool": "stop", "input": {"reason": "not_interested"}})
    clock.advance(seconds=61)
    run_jobs()
    text = sent(OWNER)[-1]["text"]
    assert text.startswith("Entendido, gracias por contestarme. Si al final por tu cuenta no sale como esperas, Laura")
    assert not re.search(r"\d", text)  # nothing invented: no numbers, no rates
    assert get_lead_status(account) == "not_interested"
    assert reply(account, "no gracias") == "opted_out"
    assert sent(OWNER)[-1]["text"] == "Entendido, no te escribiremos más. Un saludo."


def test_the_model_cannot_send_the_pitch_twice(account, clock):
    contacted(account, clock, success_pitch=PITCH)
    reply(account, "no gracias")
    n = len(sent(OWNER))
    reply(account, "Bueno, ¿y qué más?")
    FakeBackend.push({"text": f"Te cuento: {PITCH} ¿Te animas?"})
    clock.advance(seconds=61)
    run_jobs()
    assert len(sent(OWNER)) == n and get_lead_status(account) == "handoff"


def get_lead_status(account_id):
    with db.session_scope() as s:
        return s.execute(select(Lead.status).where(Lead.account_id == account_id, Lead.phone == OWNER)).scalar_one()


# ------------------------------------------------------------------ hard stops: immediate and final

def test_hard_stop_words_end_at_once_without_the_model(account, clock):
    contacted(account, clock, success_pitch=PITCH)
    assert reply(account, "No me escribas más, por favor") == "opted_out"
    assert sent(OWNER)[-1]["text"] == "Entendido, no te escribiremos más. Un saludo."
    assert not any(PITCH in m["text"] for m in sent(OWNER)) and FakeBackend.calls == []
    with db.session_scope() as s:
        assert s.execute(select(Lead.do_not_contact).where(Lead.phone == OWNER)).scalar_one()


def test_baja_still_opts_out(account, clock):
    contacted(account, clock)
    assert reply(account, "BAJA") == "opted_out"
    assert len(sent(OWNER)) == 2 and FakeBackend.calls == []


def test_data_request_hands_off_and_stops(account, clock):
    contacted(account, clock)
    assert reply(account, "Borra mis datos ahora mismo") == "handoff_data"
    out = sent(OWNER)
    assert out[-1]["text"].startswith("Entendido, no te escribiremos más. He pasado tu petición a Laura")
    alert = sent("+34611111111")[-1]["text"]
    assert "borrar sus datos" in alert and "un mes" in alert
    with db.session_scope() as s:
        lead = s.execute(select(Lead).where(Lead.phone == OWNER)).scalar_one()
        assert lead.do_not_contact and lead.status == "handoff" and lead.human_owned
    reply(account, "¿Me has oído?")
    clock.advance(minutes=5)
    run_jobs()
    assert len(sent(OWNER)) == len(out) and FakeBackend.calls == []


def test_threat_hands_off(account, clock):
    contacted(account, clock)
    assert reply(account, "Como me volváis a escribir os denuncio a la AEPD") == "handoff_threat"
    assert reply(account, "otra vez") == "stored_only"


def test_threat_words_without_data(account, clock):
    contacted(account, clock)
    assert reply(account, "Voy a llamar a mi abogado") == "handoff_threat"
    assert "denunciar" in sent("+34611111111")[-1]["text"]


def test_model_tagged_privacy_handoff_is_a_hard_stop(account, clock):
    contacted(account, clock)
    reply(account, "Quiero saber qué información tenéis de mí")
    FakeBackend.push({"tool": "handoff", "input": {"reason": "[privacy] pide acceso a sus datos"}})
    clock.advance(seconds=61)
    run_jobs()
    assert sent(OWNER)[-1]["text"].startswith("Entendido, no te escribiremos más. He pasado tu petición")
    with db.session_scope() as s:
        assert s.execute(select(Lead.do_not_contact).where(Lead.phone == OWNER)).scalar_one()
    assert events(account, "handoff")
