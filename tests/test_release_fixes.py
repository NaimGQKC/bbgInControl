"""Pre-release fixes after the real-model rehearsal: data/threat messages alert the agent for every lead, a booked owner who
cancels in chat gets no reminders, phone-call reminders read right in Spanish, one allowed-hours window, and an undeliverable
first message is never lost silently."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app import db, guards, jobs
from app.llm import FakeBackend
from app.models import Account, Job, Lead, Meeting
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


def test_phone_call_reminder_reads_right_in_spanish(clock, account):
    lid, m = _book(clock, account)
    with db.session_scope() as s:
        meeting = s.execute(select(Meeting).where(Meeting.lead_id == lid)).scalar_one()
        meeting.type = "call"  # the lead's address stays on the row: it must not turn the call into a visit
    clock.set(m.start - timedelta(hours=24))
    run_jobs()
    text = sent(M)[-1]["text"]
    assert "por por" not in text and "te llamará" in text


def test_allowed_hours_never_wider_than_9_to_21(clock, account):
    with db.session_scope() as s:
        a = s.get(Account, account)
        a.settings = {"contact_start": "07:00", "contact_end": "23:00"}
        s.flush()
        # Tuesday 2026-12-01 07:30 and 21:30 Madrid (UTC+1) are outside the capped window
        assert not guards.in_allowed_hours(a, datetime(2026, 12, 1, 6, 30))
        assert not guards.in_allowed_hours(a, datetime(2026, 12, 1, 20, 30))
        assert guards.in_allowed_hours(a, datetime(2026, 12, 1, 9, 30))


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
