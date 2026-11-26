"""Pre-release fixes after the real-model rehearsal: data/threat messages alert the agent for every lead, a booked owner who
cancels in chat gets no reminders, phone-call reminders read right in Spanish, one allowed-hours window, and an undeliverable
first message is never lost silently."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app import db, guards, jobs
from app.gcal import FakeCalendar
from app.llm import FakeBackend
from app.models import Account, Event, Job, Lead, Meeting, Message
from app.whatsapp import FakeWhatsApp, WhatsAppError
from tests.conftest import run_jobs, sent
from tests.test_flow import M, _book, ingest, inbound, lead_of, start_conversation


@pytest.mark.parametrize("text", ["Dejad de molestar o os denuncio a la AEPD, borrad mis datos", "Quiero que borréis mis datos",
                                  "Os voy a denunciar por esto"])
def test_data_or_threat_message_from_a_normal_lead_alerts_the_agent(clock, account, text):
    lid = start_conversation(clock, account)
    out = inbound(account, text, "in1")
    assert out in ("handoff_data", "handoff_threat")
    lead = lead_of(lid)
    assert lead.do_not_contact and lead.status == "handoff"
    alert = [m for m in sent() if m["from"] == "sparrow"][-1]
    assert alert["template"] == "agent_needs_you_v1" and alert["to"] == "34611111111"


def test_plain_baja_is_still_a_silent_opt_out(clock, account):
    lid = start_conversation(clock, account)
    assert inbound(account, "BAJA", "in1") == "opted_out"
    assert not [m for m in sent() if m["from"] == "sparrow" and m["template"] == "agent_needs_you_v1"]


def test_cancel_in_chat_stops_the_reminders(clock, account):
    lid, m = _book(clock, account)
    FakeBackend.push({"tool": "handoff", "input": {"reason": "quiere cancelar la cita"}})
    inbound(account, "Lo siento, tengo que cancelar", "in3")
    clock.advance(seconds=31)
    run_jobs()
    with db.session_scope() as s:
        meeting = s.execute(select(Meeting).where(Meeting.lead_id == lid)).scalar_one()
        assert meeting.status == "booked"  # still counts; only the automatic reminders stop
        pending = s.execute(select(Job).where(Job.lead_id == lid, Job.type == "reminder", Job.status == "pending")).scalars().all()
        assert pending == []
    n = len(sent(M))
    clock.set(m.start - timedelta(hours=24))
    run_jobs()
    assert len(sent(M)) == n


@pytest.mark.parametrize("reason", ["pregunta por cancelar la hipoteca", "quiere cambiar condiciones de comisión", "needs to exchange contracts"])
def test_handoff_words_that_only_look_like_a_cancellation_keep_the_reminders(clock, account, reason):
    lid, m = _book(clock, account)
    FakeBackend.push({"tool": "handoff", "input": {"reason": reason}})
    inbound(account, "tengo una duda", "in3")
    clock.advance(seconds=31)
    run_jobs()
    with db.session_scope() as s:
        assert s.execute(select(Job).where(Job.lead_id == lid, Job.type == "reminder", Job.status == "pending")).scalars().all()


def test_a_lawyer_or_police_mention_in_a_normal_chat_is_not_a_threat(clock, account):
    lid = start_conversation(clock, account)
    FakeBackend.push({"text": "Entiendo, ¿qué día os viene bien?"})
    assert inbound(account, "Mi abogada lleva la herencia, ¿qué día podéis venir?", "in1") not in ("handoff_threat", "opted_out")
    assert not lead_of(lid).do_not_contact


def test_handoff_for_another_reason_keeps_the_reminders(clock, account):
    lid, m = _book(clock, account)
    FakeBackend.push({"tool": "handoff", "input": {"reason": "pregunta por la hipoteca"}})
    inbound(account, "¿Y si pido una hipoteca?", "in3")
    clock.advance(seconds=31)
    run_jobs()
    with db.session_scope() as s:
        assert s.execute(select(Meeting).where(Meeting.lead_id == lid)).scalar_one().status == "booked"
        assert s.execute(select(Job).where(Job.lead_id == lid, Job.type == "reminder", Job.status == "pending")).scalars().all()


def test_phone_call_reminder_reads_right_in_spanish(clock, account):
    lid, m = _book(clock, account)
    with db.session_scope() as s:
        meeting = s.execute(select(Meeting).where(Meeting.lead_id == lid)).scalar_one()
        meeting.type = "call"  # the lead's address stays on the row: it must not turn the call into a visit
    clock.set(m.start - timedelta(hours=24))
    run_jobs()
    text = sent(M)[-1]["text"]
    assert "por por" not in text and "te llamará" in text


def test_hours_08_to_22_every_day_for_everyone_and_the_agent_can_only_narrow_them(clock, account):
    with db.session_scope() as s:
        a = s.get(Account, account)
        # Madrid is UTC+1 in December. Tuesday 1 Dec: 07:30, 08:30, 21:30, 22:30 local
        for cold in (False, True):  # owners who asked and prospecting share the same outer window (messages, not calls)
            assert not guards.in_allowed_hours(a, datetime(2026, 12, 1, 6, 30), cold=cold)
            assert guards.in_allowed_hours(a, datetime(2026, 12, 1, 7, 30), cold=cold)
            assert guards.in_allowed_hours(a, datetime(2026, 12, 1, 20, 30), cold=cold)
            assert not guards.in_allowed_hours(a, datetime(2026, 12, 1, 21, 30), cold=cold)
            assert guards.in_allowed_hours(a, datetime(2026, 12, 6, 10, 0), cold=cold)  # Sunday 11:00 local
        # the agent may narrow the window, never widen it
        a.settings = {"contact_start": "06:00", "contact_end": "23:30", "contact_days": [0, 1, 2, 3, 4, 5, 6]}
        s.flush()
        assert not guards.in_allowed_hours(a, datetime(2026, 12, 1, 6, 30))
        assert not guards.in_allowed_hours(a, datetime(2026, 12, 1, 21, 30))
        a.settings = {"contact_start": "10:00", "contact_end": "18:00", "contact_days": [0, 1, 2, 3, 4]}
        s.flush()
        assert not guards.in_allowed_hours(a, datetime(2026, 12, 1, 8, 30))  # 09:30 local, before the agent's own 10:00
        assert not guards.in_allowed_hours(a, datetime(2026, 12, 5, 10, 0))  # Saturday, agent excluded it
        assert guards.in_allowed_hours(a, datetime(2026, 12, 1, 12, 0))
        nxt = guards.next_allowed_time(a, datetime(2026, 12, 4, 17, 30))  # Friday 18:30 local -> Monday 10:00 local
        assert nxt == datetime(2026, 12, 7, 9, 0)


def test_failed_first_message_alerts_the_agent(clock, account, monkeypatch):
    orig = FakeWhatsApp.send_template

    def boom(self, to, *a, **k):  # only the owner's number is undeliverable; the alert to the agent must still go out
        if str(to).lstrip("+") != "34611111111":
            raise WhatsAppError("(#131026) Message undeliverable")
        return orig(self, to, *a, **k)
    monkeypatch.setattr(FakeWhatsApp, "send_template", boom)
    lid, _ = ingest(account)
    clock.advance(seconds=31)
    for _ in range(8):  # the job retries, then fails for good
        run_jobs()
        clock.advance(seconds=900)
    assert lead_of(lid).status == "failed"
    alert = [m for m in sent() if m["from"] == "sparrow"]
    assert alert and alert[-1]["template"] == "agent_needs_you_v1" and "primer mensaje" in alert[-1]["text"]


@pytest.mark.parametrize("reason,said", [("quiere cancelar", "no puedo ir"), ("wants to cancel", "I can't make it"), ("veut modifier son rdv", "je dois décaler"),
                                         ("pide otro día", "mejor otro día"), ("no puede venir mañana", "mañana no puedo")])
def test_short_cancel_reasons_stop_the_reminders(clock, account, reason, said):
    lid, m = _book(clock, account)
    FakeBackend.push({"tool": "handoff", "input": {"reason": reason}})
    inbound(account, said, "in3")
    clock.advance(seconds=31)
    run_jobs()
    with db.session_scope() as s:
        assert s.execute(select(Job).where(Job.lead_id == lid, Job.type == "reminder", Job.status == "pending")).scalars().all() == []


@pytest.mark.parametrize("text", ["Hay okupas, ya puse una denuncia", "Tengo que denunciar a mi vecino por obras", "Can you report this to Marta?"])
def test_ordinary_sales_talk_about_complaints_is_not_a_threat(text):
    assert guards.warm_hard_stop(text) is None


@pytest.mark.parametrize("text", ["Os voy a denunciar", "Os denuncio a la AEPD", "I will sue you", "Je vais porter plainte"])
def test_a_threat_aimed_at_us_is_one(text):
    assert guards.warm_hard_stop(text) == "threat"


def test_two_hour_call_reminder_reads_right(clock, account):
    lid, m = _book(clock, account)
    with db.session_scope() as s:
        s.execute(select(Meeting).where(Meeting.lead_id == lid)).scalar_one().type = "call"
    clock.set(m.start - timedelta(hours=2))
    run_jobs()
    text = sent(M)[-1]["text"]
    assert "estará en contacto por teléfono" in text and "por por" not in text


def test_data_request_after_opt_out_still_alerts_the_agent(clock, account):
    lid = start_conversation(clock, account)
    assert inbound(account, "BAJA", "in1") == "opted_out"
    n = len(sent(M))
    assert inbound(account, "Y borrad mis datos por favor", "in2") == "stored_only"
    assert len(sent(M)) == n  # nothing is sent to an opted-out number
    alert = [m for m in sent() if m["from"] == "sparrow"][-1]
    assert alert["template"] == "agent_needs_you_v1" and alert["to"] == "34611111111"


def test_failed_first_message_to_a_cold_lead_does_not_alert(clock, account):
    from app import engine
    lid, _ = ingest(account)
    with db.session_scope() as s:
        lead = s.get(Lead, lid)
        lead.facts = {**(lead.facts or {}), "_source": {"provider": "casafari"}}
    n = len([m for m in sent() if m["from"] == "sparrow"])
    with db.session_scope() as s:
        job = s.execute(select(Job).where(Job.lead_id == lid, Job.type == "first_contact")).scalars().first()
        job.last_error = "WhatsAppError('(#131026) Message undeliverable')"
        engine.job_first_contact_failed(s, job)
    assert len([m for m in sent() if m["from"] == "sparrow"]) == n


def test_any_hour_applies_only_to_a_test_lead_created_with_it(clock, account):
    clock.set(datetime(2026, 12, 1, 22, 0))  # Tuesday 23:00 Madrid: outside the window
    with db.session_scope() as s:
        acc = s.get(Account, account)
        a, _ = engine_ingest(s, acc, "+34600111001", is_test=True, any_hour=True)
        b, _ = engine_ingest(s, acc, "+34600111002", is_test=True, any_hour=False)
        c, _ = engine_ingest(s, acc, "+34600111003", is_test=False, any_hour=True)  # a real owner can never skip the window
        assert guards.can_contact(acc, a, kind="first") == (True, "ok")
        assert guards.can_contact(acc, b, kind="first") == (False, "outside_hours")
        assert guards.can_contact(acc, c, kind="first") == (False, "outside_hours")
        assert "_any_hour" not in (c.facts or {})
    run_jobs()
    clock.advance(seconds=31)
    run_jobs()
    out = [m for m in sent() if m["from"] != "sparrow"]
    assert len(out) == 1 and out[0]["to"] == "34600111001"  # only the any-hour test lead got a message


def engine_ingest(s, acc, phone, **kw):
    from app import engine
    return engine.ingest_lead(s, acc, {"name": "Test", "phone": phone, "zone": "Delicias", "source": "admin"}, **kw)


def _fail_last_out(error):
    from app import engine
    with db.session_scope() as s:
        wid = s.execute(select(Message.wa_message_id).where(Message.direction == "out", Message.sender == "ai")
                        .order_by(Message.id.desc())).scalars().first()
        engine.handle_status(s, wid, "failed", error)


def test_late_delivery_failure_of_the_first_message_alerts_the_agent_once(clock, account):
    lid = start_conversation(clock, account)
    assert lead_of(lid).status == "contacted"
    _fail_last_out("131042: Business eligibility payment issue")
    lead = lead_of(lid)
    assert lead.status == "failed"
    alerts = [m for m in sent() if m["from"] == "sparrow" and m["template"] == "agent_needs_you_v1"]
    assert len(alerts) == 1 and alerts[0]["to"] == "34611111111" and "pago" in alerts[0]["text"] and "Marta" in alerts[0]["text"]
    with db.session_scope() as s:  # no pointless follow-ups to a number we cannot reach
        assert s.execute(select(Job).where(Job.lead_id == lid, Job.type == "followup", Job.status == "pending")).scalars().all() == []
    _fail_last_out("131042: again")  # the same lead is not alerted twice
    assert len([m for m in sent() if m["from"] == "sparrow" and m["template"] == "agent_needs_you_v1"]) == 1


def test_undeliverable_number_says_so(clock, account):
    start_conversation(clock, account)
    _fail_last_out("(#131026) Message undeliverable")
    alert = [m for m in sent() if m["from"] == "sparrow"][-1]
    assert "no tiene WhatsApp" in alert["text"]


def test_a_reply_that_never_arrived_hands_the_chat_to_the_agent(clock, account):
    lid = start_conversation(clock, account)
    FakeBackend.push({"text": "Perfecto, gracias. ¿Cuándo te viene bien?"})
    inbound(account, "Sí, vendo", "in1")
    clock.advance(seconds=31)
    run_jobs()
    _fail_last_out("131047: Re-engagement message")
    lead = lead_of(lid)
    assert lead.status == "handoff" and lead.human_owned
    alert = [m for m in sent() if m["from"] == "sparrow"][-1]
    assert "Sí, vendo" in alert["text"] and "24 h" in alert["text"]


def test_agency_and_zones_are_one_clean_line_for_the_whatsapp_template():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        tok = c.post("/start", follow_redirects=False).headers["location"].split("/")[-1]
        r = c.post(f"/api/o/{tok}/answers", json={"agency": "RE/MAX\nConfort\tSur   " + "x" * 300, "zones": "Delicias\nActur"})
        assert r.status_code == 200
        assert c.post(f"/api/o/{tok}/answers", content=b"{not json", headers={"content-type": "application/json"}).status_code == 400
    with db.session_scope() as s:
        acc = s.execute(select(Account).order_by(Account.id.desc())).scalars().first()
        assert "\n" not in acc.agency and "\t" not in acc.agency and len(acc.agency) <= 120
        assert acc.zones == "Delicias Actur"


def test_temporary_ai_error_is_retried_and_the_owner_still_gets_an_answer(clock, account):
    import httpx
    lid = start_conversation(clock, account)
    FakeBackend.raise_error = httpx.ReadTimeout("slow")
    inbound(account, "Sí, vendo", "in1")
    clock.advance(seconds=31)
    run_jobs()
    assert lead_of(lid).status != "handoff"  # not handed over after the first failure
    FakeBackend.raise_error = None  # the provider is back
    FakeBackend.push({"text": "Perfecto, gracias. ¿Cuándo te viene bien?"})
    clock.advance(seconds=60)
    run_jobs()
    assert "¿Cuándo te viene bien?" in sent(M)[-1]["text"]
    assert lead_of(lid).human_owned is False


def test_ai_that_stays_down_tells_the_agent_briefly_and_the_owner_that_a_person_will_write(clock, account):
    import httpx
    lid = start_conversation(clock, account)
    FakeBackend.raise_error = httpx.ReadTimeout("slow")
    inbound(account, "Sí, vendo", "in1")
    for _ in range(6):
        clock.advance(seconds=90)
        run_jobs()
    lead = lead_of(lid)
    assert lead.status == "handoff" and lead.human_owned
    alert = [m for m in sent() if m["from"] == "sparrow"][-1]
    assert alert["to"] == "34611111111" and "no pudo responder" in alert["text"] and "escríbele" in alert["text"]
    assert "se lo paso" in sent(M)[-1]["text"].lower()  # the owner is told a person will write


def test_a_permanent_ai_error_hands_over_at_once(clock, account):
    lid = start_conversation(clock, account)
    FakeBackend.raise_error = ValueError("content filter")
    inbound(account, "Sí, vendo", "in1")
    clock.advance(seconds=31)
    run_jobs()
    assert lead_of(lid).status == "handoff"


def test_a_read_timeout_after_meta_may_have_accepted_never_sends_the_first_message_twice(clock, account, monkeypatch):
    from app.whatsapp import AmbiguousSend
    calls = []

    def slow(self, to, *a, **k):
        calls.append(to)
        raise AmbiguousSend("no answer from WhatsApp (ReadTimeout)")
    monkeypatch.setattr(FakeWhatsApp, "send_template", slow)
    lid, _ = ingest(account)
    for _ in range(5):
        clock.advance(seconds=90)
        run_jobs()
    assert len(calls) == 1  # one attempt, no retry
    assert lead_of(lid).status == "contacted"
    with db.session_scope() as s:
        row = s.execute(select(Message).where(Message.lead_id == lid, Message.direction == "out")).scalars().one()
        assert row.status == "unknown"


def test_a_job_re_run_after_a_crash_does_not_resend_the_first_message(clock, account):
    from app import engine
    lid = start_conversation(clock, account)
    n = len(sent(M))
    with db.session_scope() as s:  # simulate the crash between the send and the status update
        s.get(Lead, lid).status = "queued"
        job = s.execute(select(Job).where(Job.lead_id == lid, Job.type == "first_contact")).scalars().first()
        engine.job_first_contact(s, job)
    assert len(sent(M)) == n and lead_of(lid).status == "contacted"


def test_health_checks_the_database_and_the_queue(clock, account):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        assert c.get("/health").json() == {"ok": True}
        with db.session_scope() as s:
            s.add(Job(type="followup", run_at=db.utcnow() - timedelta(minutes=30), status="pending"))
        r = c.get("/health")
        assert r.status_code == 503 and r.json()["error"] == "queue"


def test_watchdog_tells_the_operator_once_while_a_problem_lasts(clock, account, monkeypatch):
    from app import engine
    from app.config import get_settings
    monkeypatch.setenv("OPS_WHATSAPP", "699 000 111")
    get_settings.cache_clear()
    with db.session_scope() as s:
        s.add(Job(type="followup", run_at=db.utcnow() - timedelta(minutes=1), status="failed", last_error="boom"))
    for _ in range(3):  # three runs, same problem: one message
        with db.session_scope() as s:
            engine.job_watchdog(s, Job(type="watchdog", run_at=db.utcnow()))
        clock.advance(seconds=300)
    alerts = [m for m in sent("+34699000111") if m["from"] == "sparrow"]
    assert len(alerts) == 1 and "jobs_failed_24h=1" in alerts[0]["text"]


def test_a_healthy_system_stays_quiet(clock, account, monkeypatch):
    from app import engine
    from app.config import get_settings
    monkeypatch.setenv("OPS_WHATSAPP", "699 000 111")
    get_settings.cache_clear()
    with db.session_scope() as s:
        engine.job_watchdog(s, Job(type="watchdog", run_at=db.utcnow()))
    assert sent("+34699000111") == []


def test_an_errored_webhook_event_is_retried_then_reported(clock, account, monkeypatch):
    from app import webhooks
    from app.models import WebhookEvent
    monkeypatch.setattr(webhooks, "_process", lambda s, payload: (_ for _ in ()).throw(RuntimeError("db hiccup")))
    with db.session_scope() as s:
        ev = WebhookEvent(provider="meta", external_id="evt-1", payload={}, received_at=db.utcnow() - timedelta(minutes=5))
        s.add(ev)
        s.flush()
        eid = ev.id
    for expected in ("[1]", "[2]", "[3]"):
        webhooks.sweep_unprocessed() if expected != "[1]" else webhooks.process_event(eid)
        with db.session_scope() as s:
            assert s.get(WebhookEvent, eid).error.startswith(expected)
    assert webhooks.sweep_unprocessed() == 0  # after three tries it is left for the watchdog to report
    monkeypatch.setattr(webhooks, "_process", lambda s, payload: None)


@pytest.mark.parametrize("text", ["no me llames por la mañana, prefiero por la tarde", "No me molestéis en horario de trabajo, escribidme por la tarde",
                                  "no me llames mientras trabajo, mejor por whatsapp", "Lo di de baja ayer", "ya he dado de baja el anuncio"])
def test_scheduling_preferences_and_listing_talk_are_not_opt_outs(text):
    assert not guards.is_opt_out_text(text)


@pytest.mark.parametrize("text", ["no me escribas más", "No me llames más por favor", "baja por favor", "dame de baja", "no me llames, no me escribas más",
                                  "STOP", "no me molestéis nunca más", "No me escribáis, prefiero vender por mi cuenta", "No me contactéis por WhatsApp",
                                  "No me escribas por WhatsApp, gracias", "Ya lo quité del portal. BAJA", "Ya lo di de baja, STOP", "Lo he retirado, dadme de baja",
                                  "no me llames por la tarde, nunca más", "do not contact me"])
def test_real_opt_outs_still_work(text):
    assert guards.is_opt_out_text(text)


def test_billing_counts_a_meeting_when_it_is_scheduled_even_if_the_agent_deletes_the_event(clock, account, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setenv("ADMIN_PASSWORD", "pw")
    from app.config import get_settings
    get_settings.cache_clear()
    lid, m = _book(clock, account)
    booked_month = f"{db.utcnow():%Y-%m}"
    with db.session_scope() as s:
        s.execute(select(Meeting).where(Meeting.lead_id == lid)).scalar_one().is_test = False  # test meetings are left out of billing
    FakeCalendar.events[m.google_event_id]["deleted"] = True  # the agent deletes it to dodge the fee
    clock.set(m.end + timedelta(minutes=11))
    run_jobs()
    with TestClient(app) as c:
        r = c.get(f"/admin/billing?month={booked_month}", auth=("admin", "pw"))
    assert r.status_code == 200 and "<b>1</b>" in r.text and "30 €" in r.text  # still billed: it was scheduled through our chat
    assert "Total: 30 €" in r.text


def test_meeting_check_marks_a_deleted_event_as_cancelled(clock, account):
    lid, m = _book(clock, account)
    FakeCalendar.events[m.google_event_id]["deleted"] = True
    clock.set(m.end + timedelta(minutes=11))
    run_jobs()
    with db.session_scope() as s:
        assert s.execute(select(Meeting).where(Meeting.lead_id == lid)).scalar_one().status == "cancelled"
        assert not s.execute(select(Event).where(Event.name == "meeting_verified", Event.lead_id == lid)).first()


def test_slots_can_be_asked_for_a_given_day_part_of_day_or_hour(clock, account):
    from app import engine
    from datetime import date
    clock.set(datetime(2026, 12, 1, 9, 0))  # Tuesday 10:00 Madrid
    with db.session_scope() as s:
        acc = s.get(Account, account)
        default = engine.compute_free_slots(s, acc)
        assert 1 <= len(default) <= 6
        sat = engine.compute_free_slots(s, acc, on_date=date(2026, 12, 5))  # Saturday, 4 days away
        assert sat and all(x["slot_id"].startswith("2026-12-05") for x in sat)
        morning = engine.compute_free_slots(s, acc, on_date=date(2026, 12, 5), part_of_day="morning")
        assert morning and all(int(x["slot_id"][11:13]) < 14 for x in morning)
        eleven = engine.compute_free_slots(s, acc, on_date=date(2026, 12, 5), near_time="11:00")
        assert any(x["slot_id"].endswith("T11:00") for x in eleven)  # 11:00 is free even though it is off the 3 h grid
        assert engine.compute_free_slots(s, acc, on_date=date(2026, 12, 6)) == []  # Sunday is not a slot day: empty, so "no hay hueco" is true


def test_missing_meta_payment_method_is_reported_and_rechecked(clock, monkeypatch):
    from fastapi.testclient import TestClient
    from app.config import get_settings
    from app.main import app
    monkeypatch.setenv("OPS_WHATSAPP", "699 000 111")
    get_settings.cache_clear()
    FakeWhatsApp.payment_state = "missing"
    try:
        with TestClient(app) as c:
            tok = c.post("/start", follow_redirects=False).headers["location"].split("/")[-1]
            c.post(f"/api/o/{tok}/answers", json={"agent_name": "Laura", "agency": "RE/MAX", "zones": "Delicias", "email": "l@x.es", "personal_whatsapp": "611 111 111"})
            with db.session_scope() as s:
                from app import onboarding
                acc = s.execute(select(Account).order_by(Account.id.desc())).scalars().first()
                onboarding.upsert_integration(s, acc.id, "meta", external={"waba_id": "w9", "phone_number_id": "p9"}, token_enc="x")
            st = c.post(f"/api/o/{tok}/meta_payment/recheck").json()
            assert st["meta_payment"] == "missing"
            ops = [m for m in sent("+34699000111") if m["from"] == "sparrow"]
            assert len(ops) == 1 and "método de pago" in ops[0]["text"]
            c.post(f"/api/o/{tok}/meta_payment/recheck")  # still missing: the operator is not told twice
            assert len([m for m in sent("+34699000111") if m["from"] == "sparrow"]) == 1
            FakeWhatsApp.payment_state = "ok"  # the agent added the card
            assert c.post(f"/api/o/{tok}/meta_payment/recheck").json()["meta_payment"] == "ok"
    finally:
        FakeWhatsApp.payment_state = "ok"


def test_a_temporary_model_error_after_a_booking_still_confirms_and_keeps_the_reminders(clock, account):
    import httpx
    lid = start_conversation(clock, account)
    FakeBackend.push({"tool": "get_free_slots"})
    FakeBackend.push({"text": "¿Te va bien alguno?"})
    inbound(account, "sí", "in1")
    clock.advance(seconds=31)
    run_jobs()
    slot = lead_of(lid).offered_slots[-1]
    FakeBackend.push({"tool": "book_slot", "input": {"slot_id": slot, "meeting_type": "visit", "address": "Calle Mayor 1"}})
    orig = FakeBackend.complete
    calls = {"n": 0}

    def flaky(self, system, messages, tools):
        calls["n"] += 1
        if calls["n"] == 2:  # the model call that writes the reply after the booking times out
            raise httpx.ReadTimeout("slow")
        return orig(self, system, messages, tools)
    FakeBackend.complete = flaky
    try:
        inbound(account, "ese", "in2")
        clock.advance(seconds=31)
        run_jobs()
    finally:
        FakeBackend.complete = orig
    assert lead_of(lid).status == "booked"
    assert "confirmada" in sent(M)[-1]["text"].lower()  # confirmed by code, not "se lo paso al agente"
    with db.session_scope() as s:
        pending = s.execute(select(Job).where(Job.lead_id == lid, Job.type.in_(["reminder", "meeting_check"]), Job.status == "pending")).scalars().all()
        assert {j.type for j in pending} == {"reminder", "meeting_check"}
        assert s.execute(select(Event).where(Event.name == "meeting_booked", Event.lead_id == lid)).first()


def test_the_watchdog_does_not_alert_about_its_own_alerts_and_stays_quiet_when_nothing_changes(clock, account, monkeypatch):
    from app import engine
    from app.config import get_settings
    monkeypatch.setenv("OPS_WHATSAPP", "")  # no operator number: the alert cannot be sent (alert_not_sent)
    get_settings.cache_clear()
    with db.session_scope() as s:
        s.add(Job(type="followup", run_at=db.utcnow() - timedelta(minutes=1), status="failed", last_error="boom"))
    for _ in range(4):
        with db.session_scope() as s:
            engine.job_watchdog(s, Job(type="watchdog", run_at=db.utcnow()))
        clock.advance(seconds=60)
    with db.session_scope() as s:
        assert len(s.execute(select(Event).where(Event.name == "watchdog_alert")).scalars().all()) == 1


def test_the_first_message_guard_still_schedules_the_follow_ups(clock, account):
    from app import engine
    lid = start_conversation(clock, account)
    with db.session_scope() as s:
        for j in s.execute(select(Job).where(Job.lead_id == lid, Job.type == "followup")).scalars():
            s.delete(j)
        s.get(Lead, lid).status = "queued"
        job = s.execute(select(Job).where(Job.lead_id == lid, Job.type == "first_contact")).scalars().first()
        engine.job_first_contact(s, job)
        s.flush()
        assert len(s.execute(select(Job).where(Job.lead_id == lid, Job.type == "followup")).scalars().all()) == 2


def _approve_v3(account):
    from app.models import Integration
    with db.session_scope() as s:
        integ = s.execute(select(Integration).where(Integration.account_id == account, Integration.provider == "meta")).scalar_one()
        integ.external = {**integ.external, "tpl_status": {"lead_first_contact_v3:es": "APPROVED", "lead_first_contact_v2:es": "APPROVED"}}


def test_new_first_message_says_it_is_an_ai_in_a_one_line_footer(clock, account):
    _approve_v3(account)
    lid, _ = ingest(account)
    clock.advance(seconds=31)
    run_jobs()
    first = sent(M)[0]
    assert first["template"] == "lead_first_contact_v3"
    assert first["text"].startswith("Hola Marta, te escribo de parte de Laura (RE/MAX Confort)")
    assert "asistente" not in first["text"].lower()  # no "asistente virtual" in the message itself
    assert first["text"].rstrip().splitlines()[-1].startswith("Soy IA. ") and "STOP" in first["text"]  # the disclosure and the opt-out, in the footer
    assert lead_of(lid).introduced is True  # already disclosed: the AI does not repeat a long introduction
    FakeBackend.push({"text": "Perfecto, gracias. ¿Lo tienes reformado o para actualizar?"})
    inbound(account, "Sí, sigo pensando en venderlo", "in1")
    clock.advance(seconds=31)
    run_jobs()
    assert sent(M)[-1]["text"] == "Perfecto, gracias. ¿Lo tienes reformado o para actualizar?"


def test_until_meta_approves_v3_the_previous_first_message_is_used(clock, account):
    from app.models import Integration
    with db.session_scope() as s:
        integ = s.execute(select(Integration).where(Integration.account_id == account, Integration.provider == "meta")).scalar_one()
        integ.external = {**integ.external, "tpl_status": {"lead_first_contact_v3:es": "PENDING", "lead_first_contact_v2:es": "APPROVED"}}
    lid, _ = ingest(account)
    clock.advance(seconds=31)
    run_jobs()
    assert sent(M)[0]["template"] == "lead_first_contact_v2"  # a template Meta has not approved is never sent
    assert lead_of(lid).introduced is True  # v2 says "asistente virtual" itself
