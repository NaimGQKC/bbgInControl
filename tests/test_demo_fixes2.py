"""Second batch from the pre-demo QA: opt-out however it is written, a bare "no" mid-chat, the booking confirmation when the
model says nothing, a repeatable admin test lead, and queued leads released at go-live."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import db, engine, guards
from app.config import get_settings
from app.llm import FakeBackend
from app.main import app
from app.models import Account, Lead, Meeting
from tests.conftest import run_jobs, sent
from tests.test_flow import M, ingest, inbound, lead_of, start_conversation


@pytest.mark.parametrize("text", ["BAJA", "¡BAJA!", "baja por favor", "Baja ya", "STOP 🙏", "stop please", "Arrêtez", "ARRÊT.",
                                  "no me escribas más", "No me escribáis más por favor", "ne m'écrivez plus", "Unsubscribe"])
def test_every_way_of_saying_stop_is_an_opt_out(text):
    assert guards.is_opt_out_text(text)


@pytest.mark.parametrize("text", ["Sí, sigo vendiendo", "¿Cuánto cobráis?", "He bajado el precio a 180.000", "Baja el precio un poco, ¿no?",
                                  "El piso tiene una baja por reforma en la cocina y por eso tardo en contestar, lo siento mucho",
                                  "No, no está ocupado", "Mañana por la tarde"])
def test_normal_messages_are_not_opt_outs(text):
    assert not guards.is_opt_out_text(text)


def test_punctuated_baja_stops_the_chat_in_code(clock, account):
    lid = start_conversation(clock, account)
    assert inbound(account, "¡BAJA!", "in1") == "opted_out"
    assert lead_of(lid).do_not_contact


def test_empty_model_text_after_a_booking_still_confirms_the_meeting(clock, account):
    lid = start_conversation(clock, account)
    FakeBackend.push({"tool": "get_free_slots"})
    FakeBackend.push({"text": "Laura puede pasar el miércoles o el jueves. ¿Cuál te viene mejor?"})
    inbound(account, "Sí, quiero la valoración", "in1")
    clock.advance(seconds=31)
    run_jobs()
    slot = lead_of(lid).offered_slots[-1]
    FakeBackend.push({"tool": "book_slot", "input": {"slot_id": slot, "meeting_type": "visit", "address": "Calle Delicias 12"}})
    FakeBackend.push({"text": ""})  # the model booked and then said nothing
    inbound(account, "El primero", "in2")
    clock.advance(seconds=31)
    run_jobs()
    assert lead_of(lid).status == "booked"
    last = sent(M)[-1]["text"].lower()
    assert "confirmada" in last and "laura" in last  # our own words, not "se lo paso a Laura"
    assert lead_of(lid).human_owned is False and lead_of(lid).status != "handoff"


def test_admin_test_lead_can_be_run_twice_with_the_same_phone(account, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "pw")
    get_settings.cache_clear()
    with TestClient(app) as c:
        for _ in range(2):
            r = c.post(f"/admin/accounts/{account}/test_lead", data={"name": "Demo", "phone": "600 123 456", "test": "on"},
                       auth=("admin", "pw"), follow_redirects=False)
            assert r.status_code in (302, 303)
    with db.session_scope() as s:
        leads = s.execute(select(Lead).where(Lead.account_id == account, Lead.phone == "+34600123456")).scalars().all()
        assert len(leads) == 1 and leads[0].is_test and leads[0].status == "queued"


def test_leads_that_arrived_before_go_live_are_contacted_once_live(clock, account):
    with db.session_scope() as s:
        s.get(Account, account).status = "onboarding"
    lid, _ = ingest(account)
    clock.advance(seconds=31)
    run_jobs()
    assert sent(M) == [] and lead_of(lid).status == "queued"
    with db.session_scope() as s:
        a = s.get(Account, account)
        a.status = "live"
        assert engine.rearm_queued_leads(s, a) == 1
    clock.advance(seconds=31)
    run_jobs()
    assert len(sent(M)) == 1 and lead_of(lid).status == "contacted"


def test_all_caps_names_are_tidied_in_the_first_message(clock, account):
    ingest(account, {"name": "MARIA GÓMEZ", "phone": "600 000 001", "zone": "Delicias", "source": "lystos"})
    clock.advance(seconds=31)
    run_jobs()
    assert "Hola Maria" in sent(M)[0]["text"] or "Hola María" in sent(M)[0]["text"]
