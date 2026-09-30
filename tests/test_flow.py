"""End-to-end behavior and guardrails G1–G14 (design doc section 11), all with fakes."""
import hashlib
import hmac
import json
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import db, engine
from app.gcal import FakeCalendar
from app.llm import FakeBackend
from app.models import Account, Event, Job, Lead, Meeting, Message
from tests.conftest import run_jobs, sent

LEAD = {"name": "Marta Gómez", "phone": "600 000 001", "zone": "Delicias", "property_type": "piso", "source": "lystos"}
M = "+34600000001"


def ingest(account_id, data=LEAD, **kw):
    with db.session_scope() as s:
        lead, res = engine.ingest_lead(s, s.get(Account, account_id), dict(data), **kw)
        return (lead.id if lead else None), res


def inbound(account_id, text, mid, phone=M, type_="text"):
    with db.session_scope() as s:
        return engine.handle_inbound(s, s.get(Account, account_id), phone.lstrip("+"), {"id": mid, "type": type_, "text": text})


def echo(account_id, text, mid, phone=M):
    with db.session_scope() as s:
        return engine.handle_echo(s, s.get(Account, account_id), phone.lstrip("+"), {"id": mid, "type": "text", "text": text})


def lead_of(lead_id):
    with db.session_scope() as s:
        return s.get(Lead, lead_id)


def start_conversation(clock, account):
    lid, _ = ingest(account)
    clock.advance(seconds=31)
    run_jobs()
    return lid


# ------------------------------------------------------------------ J2 first message

def test_first_message_after_30_seconds(clock, account):
    lid, res = ingest(account)
    assert res == "queued"
    run_jobs()
    assert sent() == []  # not before 30s
    clock.advance(seconds=31)
    run_jobs()
    out = sent(M)
    assert len(out) == 1 and out[0]["template"] == "lead_first_contact_v1"
    assert "asistente virtual de Laura García" in out[0]["text"]  # G13
    assert "Hola Marta" in out[0]["text"]
    lead = lead_of(lid)
    assert lead.status == "contacted" and lead.introduced


def test_outside_hours_waits_for_morning(clock, account):  # G9
    clock.set(clock.now.replace(hour=20, minute=30))  # 22:30 Madrid
    ingest(account)
    clock.advance(minutes=1)
    run_jobs()
    assert sent() == []
    clock.set(clock.now.replace(hour=7, minute=1) + timedelta(days=1))  # 09:01 Madrid next day
    run_jobs()
    assert len(sent(M)) == 1


def test_duplicate_lead_gets_one_message(clock, account):  # G4
    ingest(account)
    _, res = ingest(account, {**LEAD, "phone": "+34 600-000-001", "source": "google_ads"})
    assert res == "duplicate"
    clock.advance(seconds=31)
    run_jobs()
    assert len(sent(M)) == 1


def test_skips_buyers_hot_stages_and_no_ia(clock, account):  # G14, G8
    assert ingest(account, {**LEAD, "phone": "600000002", "lead_type": "buyer"})[1] == "skipped:buyer"
    assert ingest(account, {**LEAD, "phone": "600000003", "stage": "Exclusiva"})[1] == "skipped:hot_stage"
    assert ingest(account, {**LEAD, "phone": "600000004", "tags": ["No IA"]})[1] == "skipped:no_ia_tag"
    assert ingest(account, {**LEAD, "phone": "123"})[1] == "invalid_phone"
    clock.advance(minutes=1)
    run_jobs()
    assert sent() == []


def test_paused_account_sends_nothing(clock, account):  # G11
    with db.session_scope() as s:
        s.get(Account, account).status = "paused"
    ingest(account)
    clock.advance(minutes=1)
    run_jobs()
    assert sent() == []


# ------------------------------------------------------------------ J4 conversation + booking

def test_full_conversation_books_meeting(clock, account):
    lid = start_conversation(clock, account)
    FakeBackend.push({"tool": "save_facts", "input": {"condition": "reformado"}}, {"tool": "get_free_slots"})
    FakeBackend.push({"text": "Genial. Laura puede pasar el miércoles o el jueves. ¿Cuál te viene mejor?"})
    assert inbound(account, "Está reformado", "in1") == "scheduled"
    run_jobs()
    assert len(sent(M)) == 1  # AI waits ~30s (handover window)
    clock.advance(seconds=31)
    run_jobs()
    assert sent(M)[-1]["text"].startswith("Genial.")
    lead = lead_of(lid)
    assert lead.status == "offering_slots" and lead.offered_slots and lead.facts["condition"] == "reformado"

    slot = lead.offered_slots[-1]  # > 24h away, so both reminders apply
    FakeBackend.push({"tool": "book_slot", "input": {"slot_id": slot, "meeting_type": "visit", "address": "Calle Delicias 12"}})
    FakeBackend.push({"text": "Hecho ✅ Te esperamos en Calle Delicias 12."})
    inbound(account, "El primero", "in2")
    clock.advance(seconds=31)
    run_jobs()
    lead = lead_of(lid)
    assert lead.status == "booked"  # G6
    with db.session_scope() as s:
        m = s.execute(select(Meeting).where(Meeting.lead_id == lid)).scalar_one()
        assert m.google_event_id in FakeCalendar.events
        ev = FakeCalendar.events[m.google_event_id]
        assert ev["summary"].startswith("Valoración · Marta G.")
        assert "wa.me/34600000001" in ev["description"] and "reformado" in ev["description"]
        reminders = s.execute(select(Job).where(Job.lead_id == lid, Job.type == "reminder")).scalars().all()
        assert len(reminders) == 2
        assert s.execute(select(Event).where(Event.name == "meeting_booked")).first()
    assert sent(M)[-1]["text"].startswith("Hecho")


def test_cannot_book_slot_that_was_not_offered(clock, account):  # G12
    lid = start_conversation(clock, account)
    inbound(account, "hola", "in1")
    with db.session_scope() as s:
        res = engine.book_slot(s, s.get(Account, account), s.get(Lead, lid), "2026-10-01T18:00", "visit")
    assert res["ok"] is False and res["error"] == "slot_not_offered"


def test_two_leads_same_slot_one_event(clock, account):  # G5
    a = start_conversation(clock, account)
    b, _ = ingest(account, {**LEAD, "name": "Pedro", "phone": "600000009"})
    with db.session_scope() as s:
        acc = s.get(Account, account)
        slots = engine.compute_free_slots(s, acc)
        slot = slots[0]["slot_id"]
        for lid in (a, b):
            s.get(Lead, lid).offered_slots = [slot]
    with db.session_scope() as s:
        acc = s.get(Account, account)
        r1 = engine.book_slot(s, acc, s.get(Lead, a), slot, "visit")
        r2 = engine.book_slot(s, acc, s.get(Lead, b), slot, "visit")
    assert r1["ok"] and not r2["ok"]
    assert len(FakeCalendar.events) == 1


def test_busy_calendar_is_respected(clock, account):
    with db.session_scope() as s:
        acc = s.get(Account, account)
        all_slots = engine.compute_free_slots(s, acc)
        from app.engine import _slot_to_utc

        first = _slot_to_utc(all_slots[0]["slot_id"], acc.timezone)
        FakeCalendar.busy_blocks = [(first - timedelta(minutes=10), first + timedelta(hours=1))]
        again = engine.compute_free_slots(s, acc)
    assert all_slots[0]["slot_id"] not in [x["slot_id"] for x in again]


# ------------------------------------------------------------------ opt-out, window, errors

def test_baja_opts_out_forever(clock, account):  # G2
    lid = start_conversation(clock, account)
    assert inbound(account, "BAJA", "in1") == "opted_out"
    assert sent(M)[-1]["text"].startswith("Entendido, no te escribiremos")
    n = len(sent(M))
    clock.advance(days=4)
    run_jobs()
    ingest(account)  # re-import from the CRM
    clock.advance(minutes=1)
    run_jobs()
    assert len(sent(M)) == n
    assert lead_of(lid).do_not_contact


def test_llm_opt_out(clock, account):
    lid = start_conversation(clock, account)
    FakeBackend.push({"tool": "stop", "input": {"reason": "opted_out"}})
    inbound(account, "no me escribáis más por favor", "in1")
    clock.advance(seconds=31)
    run_jobs()
    assert lead_of(lid).do_not_contact and lead_of(lid).status == "opted_out"


def test_no_free_text_outside_24h_window(clock, account):  # G7
    lid = start_conversation(clock, account)
    inbound(account, "hola", "in1")
    clock.advance(hours=25)
    with db.session_scope() as s:
        res = engine.run_ai_turn(s, s.get(Account, account), s.get(Lead, lid))
    assert res == "window_closed"


def test_llm_failure_hands_off(clock, account):  # G10
    lid = start_conversation(clock, account)
    FakeBackend.raise_error = RuntimeError("boom")
    inbound(account, "¿cuánto cobráis?", "in1")
    clock.advance(seconds=31)
    run_jobs()
    lead = lead_of(lid)
    assert lead.status == "handoff" and lead.human_owned
    alerts = [m for m in sent() if m["from"] == "sparrow"]
    assert alerts and alerts[-1]["template"] == "agent_needs_you_v1" and alerts[-1]["to"] == "34611111111"
    assert "se lo paso a Laura" in sent(M)[-1]["text"]


def test_handoff_tool_alerts_agent(clock, account):  # J7
    lid = start_conversation(clock, account)
    FakeBackend.push({"tool": "handoff", "input": {"reason": "pregunta por la comisión"}})
    inbound(account, "¿Qué comisión cobráis?", "in1")
    clock.advance(seconds=31)
    run_jobs()
    alert = [m for m in sent() if m["from"] == "sparrow"][-1]
    assert "pregunta por la comisión" in alert["text"] and "Marta" in alert["text"]
    assert lead_of(lid).status == "handoff"


# ------------------------------------------------------------------ J3 / J5 handover

def test_agent_writes_after_reply_pauses_ai(clock, account):  # G8
    lid = start_conversation(clock, account)
    inbound(account, "hola, sí", "in1")
    assert echo(account, "Hola Marta, soy Laura, te llamo ahora", "echo1") == "human_owned"
    clock.advance(seconds=31)
    run_jobs()
    assert len(sent(M)) == 1  # only the first template, no AI reply
    inbound(account, "vale!", "in2")
    clock.advance(seconds=31)
    run_jobs()
    assert len(sent(M)) == 1 and lead_of(lid).human_owned


def test_agent_first_opening_then_ai_introduces_itself(clock, account):  # J3, G13
    lid, _ = ingest(account)
    assert echo(account, "Hola Marta, vi tu anuncio", "echo1") == "agent_opening"
    clock.advance(seconds=31)
    run_jobs()
    assert sent(M) == []  # no template: the agent already opened
    FakeBackend.push({"text": "¿Te busco una hora para veros?"})
    inbound(account, "Hola! sí, dime", "in1")
    clock.advance(seconds=31)
    run_jobs()
    assert sent(M)[-1]["text"].startswith("Soy el asistente virtual de Laura")
    assert not lead_of(lid).human_owned


def test_resume_via_sparrow_number(clock, account):
    lid = start_conversation(clock, account)
    inbound(account, "hola", "in1")
    echo(account, "te escribo luego", "echo1")
    assert lead_of(lid).human_owned
    inbound(account, "¿entonces cuándo venís?", "in2")
    with db.session_scope() as s:
        assert engine.handle_sparrow_inbound(s, "34611111111", {"text": "Sigue con Marta"}) == "resumed"
    FakeBackend.push({"text": "Te propongo el jueves a las 18:00."})
    run_jobs()
    assert sent(M)[-1]["text"] == "Te propongo el jueves a las 18:00."
    assert "Hecho, sigo yo con Marta" in [m for m in sent() if m["from"] == "sparrow"][-1]["text"]


def test_burst_grouped_into_one_turn(clock, account):
    start_conversation(clock, account)
    FakeBackend.push({"text": "Entendido, gracias."})
    inbound(account, "hola", "in1")
    clock.advance(seconds=10)
    inbound(account, "es un piso", "in2")
    clock.advance(seconds=10)
    inbound(account, "de 3 habitaciones", "in3")
    clock.advance(seconds=31)
    run_jobs()
    assert len(FakeBackend.calls) == 1
    assert "de 3 habitaciones" in json.dumps(FakeBackend.calls[0]["messages"], ensure_ascii=False)
    assert len(sent(M)) == 2


# ------------------------------------------------------------------ G1, G3 via the real webhook endpoint

def _wh(value, field="messages"):
    return {"object": "whatsapp_business_account", "entry": [{"id": "w1", "changes": [{"field": field, "value": {"metadata": {"phone_number_id": "pn1"}, **value}}]}]}


def test_webhook_duplicates_and_non_leads(clock, account):  # G1, G3
    from app.main import app

    lid = start_conversation(clock, account)
    body = _wh({"messages": [{"from": "34600000001", "id": "wamid.X1", "type": "text", "text": {"body": "hola"}}]})
    with TestClient(app) as c:
        assert c.post("/webhooks/meta", json=body).json()["ok"]
        assert c.post("/webhooks/meta", json=body).json().get("duplicate")
        body2 = _wh({"messages": [{"from": "34600000001", "id": "wamid.X1", "type": "text", "text": {"body": "hola"}}], "extra": 1})
        c.post("/webhooks/meta", json=body2)  # same message id, different delivery
        stranger = _wh({"messages": [{"from": "34699999999", "id": "wamid.S1", "type": "text", "text": {"body": "¿quedamos para cenar?"}}]})
        c.post("/webhooks/meta", json=stranger)
        c.post("/webhooks/meta", json=_wh({"message_echoes": [{"from": "x", "to": "34699999999", "id": "e1", "type": "text", "text": {"body": "sí"}}]}, "smb_message_echoes"))
    with db.session_scope() as s:
        ins = s.execute(select(Message).where(Message.direction == "in")).scalars().all()
        assert len(ins) == 1  # stranger never stored (G1), duplicate stored once (G3)
        assert s.execute(select(Message).where(Message.sender == "agent")).first() is None
    clock.advance(seconds=31)
    run_jobs()
    assert len(sent(M)) == 2  # template + exactly one AI reply
    assert sent("+34699999999") == []


def test_webhook_signature_required_when_secret_set(monkeypatch, account):
    from app.config import get_settings
    from app.main import app

    monkeypatch.setenv("META_APP_SECRET", "s3cret")
    get_settings.cache_clear()
    body = json.dumps(_wh({"statuses": []})).encode()
    with TestClient(app) as c:
        assert c.post("/webhooks/meta", content=body, headers={"x-hub-signature-256": "sha256=bad"}).status_code == 401
        sig = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        assert c.post("/webhooks/meta", content=body, headers={"x-hub-signature-256": f"sha256={sig}", "content-type": "application/json"}).status_code == 200
        assert c.get("/webhooks/meta", params={"hub.mode": "subscribe", "hub.verify_token": "sparrow-verify", "hub.challenge": "42"}).text == "42"


# ------------------------------------------------------------------ follow-ups and reminders

def test_followups_then_no_reply(clock, account):
    lid = start_conversation(clock, account)
    clock.advance(days=1, hours=2)
    run_jobs()
    assert sent(M)[-1]["template"] == "lead_followup_1_v1"
    clock.advance(days=2)
    run_jobs()
    assert sent(M)[-1]["template"] == "lead_followup_2_v1"
    clock.advance(days=3)
    run_jobs()
    assert lead_of(lid).status == "no_reply"
    assert len(sent(M)) == 3


def test_reply_cancels_followups(clock, account):
    start_conversation(clock, account)
    inbound(account, "hola", "in1")
    clock.advance(days=5)
    run_jobs()
    assert not any(m.get("template", "").startswith("lead_followup") for m in sent(M))


def _book(clock, account):
    lid = start_conversation(clock, account)
    FakeBackend.push({"tool": "get_free_slots"})
    FakeBackend.push({"text": "¿Te va bien alguno?"})
    inbound(account, "sí", "in1")
    clock.advance(seconds=31)
    run_jobs()
    slot = lead_of(lid).offered_slots[-1]
    FakeBackend.push({"tool": "book_slot", "input": {"slot_id": slot, "meeting_type": "visit", "address": "Calle Mayor 1"}})
    FakeBackend.push({"text": "Hecho ✅"})
    inbound(account, "ese", "in2")
    clock.advance(seconds=31)
    run_jobs()
    with db.session_scope() as s:
        return lid, s.execute(select(Meeting).where(Meeting.lead_id == lid)).scalar_one()


def test_reminders_sent_and_skipped_if_event_deleted(clock, account):
    lid, m = _book(clock, account)
    clock.set(m.start - timedelta(hours=24))
    run_jobs()
    assert "mañana a las" in sent(M)[-1]["text"]
    FakeCalendar.events[m.google_event_id]["deleted"] = True
    n = len(sent(M))
    clock.set(m.start - timedelta(hours=2))
    run_jobs()
    assert len(sent(M)) == n


def test_booked_counts_even_if_agent_cancels(clock, account):
    lid, m = _book(clock, account)
    FakeCalendar.events[m.google_event_id]["deleted"] = True
    with db.session_scope() as s:
        assert s.execute(select(Event).where(Event.name == "meeting_booked", Event.lead_id == lid)).first()


# ------------------------------------------------------------------ intake + onboarding

def test_intake_endpoint_spanish_fields(clock, account):
    from app.main import app

    with db.session_scope() as s:
        key = s.get(Account, account).intake_key
    with TestClient(app) as c:
        r = c.post(f"/intake/{key}?source=lystos", json={"nombre": "Ana Ruiz", "telefono": "+34 600 111 222", "zona": "Actur", "precio": "185.000", "etiquetas": "vip"})
        assert r.json()["results"][0]["result"] == "queued"
        assert c.post("/intake/nope", json={}).status_code == 404
    with db.session_scope() as s:
        lead = s.execute(select(Lead).where(Lead.phone == "+34600111222")).scalar_one()
        assert lead.price == 185000 and lead.source == "lystos" and lead.tags == ["vip"]


def test_onboarding_flow_goes_live(clock):
    from app.main import app

    with TestClient(app) as c:
        token = c.post("/start", follow_redirects=False).headers["location"].split("/")[-1]
        r = c.post(f"/api/o/{token}/answers", json={"agent_name": "Laura García", "agency": "RE/MAX", "zones": "Delicias"})
        assert r.json()["step"] == "questions"
        assert c.post(f"/api/o/{token}/answers", json={"personal_whatsapp": "12"}).status_code == 400
        assert c.post(f"/api/o/{token}/answers", json={"personal_whatsapp": "611 111 111"}).json()["step"] == "google"
        with db.session_scope() as s:
            from app import onboarding

            acc = s.execute(select(Account).where(Account.public_token == token)).scalar_one()
            onboarding.save_google(s, acc, {"access_token": "a", "refresh_token": "r", "expires_in": 3600})
        assert c.post(f"/api/o/{token}/step", json={"done": "lystos"}).json()["step"] == "wa_check"
        assert c.post(f"/api/o/{token}/step", json={"done": "wa_check"}).json()["step"] == "whatsapp"
        r = c.post(f"/api/o/{token}/meta", json={"code": "c", "waba_id": "w9", "phone_number_id": "pn9"}).json()
        assert r["step"] == "waiting_templates" and len(r["templates"]) == 6
    clock.advance(minutes=2)
    run_jobs()
    with db.session_scope() as s:
        acc = s.execute(select(Account).where(Account.public_token == token)).scalar_one()
        assert acc.status == "live" and acc.onboarding_step == "done"


# ------------------------------------------------------------------ fixes from code review

def test_no_second_opt_out_confirmation(clock, account):
    start_conversation(clock, account)
    inbound(account, "BAJA", "in1")
    inbound(account, "BAJA", "in2")
    assert sum(1 for m in sent(M) if m["text"].startswith("Entendido, no te escribiremos")) == 1


def test_no_after_followup_2_is_opt_out(clock, account):
    lid = start_conversation(clock, account)
    clock.advance(days=3, hours=3)
    run_jobs()
    assert sent(M)[-1]["template"] == "lead_followup_2_v1"
    assert inbound(account, "No", "in1") == "opted_out"
    assert lead_of(lid).do_not_contact


def test_message_during_ai_turn_gets_answered(clock, account):
    from app import jobs as jobmod

    lid = start_conversation(clock, account)
    inbound(account, "hola", "in1")
    clock.advance(seconds=31)
    real = engine.run_ai_turn

    def slow_backend_turn(s, acc, lead):
        orig = FakeBackend.complete

        def thinking(self, system, messages, tools):
            clock.advance(seconds=5)  # a new message lands while Claude is thinking
            inbound(account, "¿sigues ahí?", "in2")
            return orig(self, system, messages, tools)

        FakeBackend.complete = thinking
        try:
            return real(s, acc, lead)
        finally:
            FakeBackend.complete = orig

    engine.run_ai_turn = slow_backend_turn
    try:
        FakeBackend.push({"text": "primera respuesta"})
        jobmod.run_due(1)
    finally:
        engine.run_ai_turn = real
    FakeBackend.push({"text": "Sí, aquí estoy."})
    clock.advance(seconds=61)
    run_jobs()
    assert sent(M)[-1]["text"] == "Sí, aquí estoy."
    assert "primera respuesta" not in [m["text"] for m in sent(M)]


def test_stuck_running_jobs_are_recovered(clock, account):
    lid, _ = ingest(account)
    with db.session_scope() as s:
        j = s.execute(select(Job).where(Job.lead_id == lid)).scalar_one()
        j.status, j.claimed_at = "running", clock.now
    clock.advance(minutes=11)
    run_jobs()
    assert len(sent(M)) == 1


def test_send_failure_in_ai_turn_hands_off_without_retry(clock, account):
    from app.whatsapp import FakeWhatsApp, WhatsAppError

    lid = start_conversation(clock, account)
    inbound(account, "hola", "in1")
    FakeBackend.push({"text": "respuesta"})
    FakeWhatsApp.fail_next = WhatsAppError("rate limit", 130429)
    clock.advance(seconds=31)
    run_jobs()
    clock.advance(minutes=5)
    run_jobs()
    assert lead_of(lid).status == "handoff"
    assert len(FakeBackend.calls) == 1  # the LLM was not re-run


def test_reminder_uses_booked_time_if_calendar_fails(clock, account, monkeypatch):
    from app import gcal

    lid, m = _book(clock, account)
    monkeypatch.setattr(gcal.FakeCalendar, "get_event", lambda self, eid: (_ for _ in ()).throw(gcal.CalendarError("down")))
    clock.set(m.start - timedelta(hours=24))
    run_jobs()
    assert "mañana a las" in sent(M)[-1]["text"]


def test_onboarding_test_requires_live_account(clock):
    from app.main import app

    with TestClient(app) as c:
        token = c.post("/start", follow_redirects=False).headers["location"].split("/")[-1]
        assert c.post(f"/api/o/{token}/test", json={"phone": "600000001"}).status_code == 400
    assert sent() == []


def test_openai_message_translation():
    from types import SimpleNamespace

    from app.llm import TOOLS, OpenAIBackend

    msgs = [
        {"role": "user", "content": "hola"},
        {"role": "assistant", "content": [{"type": "text", "text": "miro"}, {"type": "tool_use", "id": "c1", "name": "get_free_slots", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": '{"slots": []}'}]},
    ]
    out, fns = OpenAIBackend.to_openai("sys", msgs, TOOLS)
    assert [m["role"] for m in out] == ["system", "user", "assistant", "tool"]
    assert out[2]["tool_calls"][0]["function"]["name"] == "get_free_slots" and out[3]["tool_call_id"] == "c1"
    assert fns[0]["function"]["parameters"]["type"] == "object"
    call = SimpleNamespace(id="c2", function=SimpleNamespace(name="book_slot", arguments='{"slot_id": "x", "meeting_type": "visit"}'))
    res = OpenAIBackend.from_openai(SimpleNamespace(content=None, tool_calls=[call]))
    assert res["stop_reason"] == "tool_use" and res["content"][0]["input"]["slot_id"] == "x"


# ------------------------------------------------------------------ cleanup pass: rules that were leaking

def test_admin_manual_send_cannot_reach_a_lead(clock, account):  # G2: leads are only messaged through send_to_lead
    from app.main import app

    lid = start_conversation(clock, account)
    engine_out = len(sent(M))
    with db.session_scope() as s:
        lead = s.get(Lead, lid)
        lead.do_not_contact = True
    with TestClient(app) as c:
        r = c.post(f"/admin/accounts/{account}/send", data={"to": M, "kind": "text", "text": "hola otra vez"},
                   auth=("admin", "change-me"), follow_redirects=False)
        assert "es un lead" in r.headers["location"].replace("%20", " ")
        c.post(f"/admin/accounts/{account}/send", data={"to": "+34699999999", "kind": "text", "text": "prueba"},
               auth=("admin", "change-me"), follow_redirects=False)
    assert len(sent(M)) == engine_out
    assert sent("+34699999999")[-1]["text"] == "prueba"  # a non-lead number (App Review demo) still works


def test_admin_test_lead_checkbox_unchecked_is_a_real_lead(clock, account):
    from app.main import app

    with TestClient(app) as c:
        c.post(f"/admin/accounts/{account}/test_lead", data={"name": "Real", "phone": "600222333"},
               auth=("admin", "change-me"), follow_redirects=False)
    with db.session_scope() as s:
        assert s.execute(select(Lead).where(Lead.phone == "+34600222333")).scalar_one().is_test is False


def test_reminder_not_logged_as_sent_when_blocked(clock, account):
    lid, m = _book(clock, account)
    with db.session_scope() as s:
        s.get(Account, account).status = "paused"  # G11
    clock.set(m.start - timedelta(hours=24))
    n = len(sent(M))
    run_jobs()
    assert len(sent(M)) == n
    with db.session_scope() as s:
        assert not s.execute(select(Event).where(Event.name == "reminder_24h_sent", Event.lead_id == lid)).first()


def test_sparrow_number_chat_is_logged_once(clock, account):  # "log every message", G3
    start_conversation(clock, account)
    with db.session_scope() as s:
        assert engine.handle_sparrow_inbound(s, "34611111111", {"id": "sp1", "text": "hola"}) == "help"
    with db.session_scope() as s:
        assert engine.handle_sparrow_inbound(s, "34611111111", {"id": "sp1", "text": "hola"}) == "duplicate"
    with db.session_scope() as s:
        rows = s.execute(select(Message).where(Message.lead_id.is_(None), Message.account_id == account)).scalars().all()
        assert [(r.direction, r.sender) for r in rows] == [("in", "agent"), ("out", "system")]
    assert len([m for m in sent() if m["from"] == "sparrow"]) == 1


def test_intake_ignores_garbage_items(clock, account):
    from app.main import app

    with db.session_scope() as s:
        key = s.get(Account, account).intake_key
    with TestClient(app) as c:
        r = c.post(f"/intake/{key}", json=["texto suelto", {"nombre": "Ana", "telefono": "600333444"}])
        assert [x["result"] for x in r.json()["results"]] == ["queued"]
