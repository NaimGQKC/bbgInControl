"""Fixes found by the pre-demo review: a plain 'no' to prospecting sends nothing to Meta, the step is hidden while the feature
is off, fake listings never reach real WhatsApps, go-live switches prospecting on after the agent accepted, injected HTML
cannot break out of the page state, and the admin refuses the public default password."""
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import casafari, db, prospecting as P
from app.casafari import FakeCasafari
from app.config import get_settings
from app.main import app
from app.models import Account
from app.whatsapp import FakeWhatsApp
from tests.test_prospecting_setup import GOOD_BODY, ZGZ, offer_zone, pros, save, token


@pytest.fixture(autouse=True)
def env(monkeypatch):
    FakeCasafari.reset()
    monkeypatch.setenv("PROSPECTING_ENABLED", "true")
    monkeypatch.setenv("CASAFARI_MODE", "fake")
    get_settings.cache_clear()
    yield
    FakeCasafari.reset()
    get_settings.cache_clear()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_a_plain_no_stores_no_message_and_sends_nothing_to_meta(account, client):
    r = client.post(f"/api/o/{token(account)}/prospecting", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    p = pros(account)
    assert "cold_body" not in p and p.get("decided_at") and p.get("cold_template_status") in (None, "")
    assert not [t for t in FakeWhatsApp.created_templates if t.get("name", "").startswith("cold_first_contact")]


def test_the_step_is_hidden_while_the_feature_is_off(account, client, monkeypatch):
    assert client.get(f"/api/o/{token(account)}").json()["prospecting"] is not None
    monkeypatch.setenv("PROSPECTING_ENABLED", "false")
    get_settings.cache_clear()
    assert client.get(f"/api/o/{token(account)}").json()["prospecting"] is None


def test_the_step_is_hidden_for_non_spanish_owners(account, client):
    with db.session_scope() as s:
        a = s.get(Account, account)
        a.settings = {**(a.settings or {}), "lang": "fr"}
    assert client.get(f"/api/o/{token(account)}").json()["prospecting"] is None


def test_fixture_listings_never_reach_live_whatsapp(account, monkeypatch):
    monkeypatch.setenv("WHATSAPP_MODE", "live")
    get_settings.cache_clear()
    with db.session_scope() as s:
        a = s.get(Account, account)
        cfg = {"enabled": True, "location_id": ZGZ, "cold_body": GOOD_BODY, "cold_template_status": "approved"}
        assert P.why_not(a, cfg) == "fake_source_with_live_whatsapp"
    monkeypatch.setenv("CASAFARI_MODE", "live")
    get_settings.cache_clear()
    with db.session_scope() as s:
        assert P.why_not(s.get(Account, account), cfg) is None


def test_going_live_switches_prospecting_on_when_the_agent_asked_and_accepted(account, client):
    with db.session_scope() as s:
        s.get(Account, account).status = "onboarding"
    offer_zone(client, account)
    r = save(client, account).json()
    assert r == {"saved": True, "enabled": False, "reason": "not_live"}
    p = pros(account)
    assert p["enable_when_live"] is True and p["ack_at"] and p["enabled"] is False
    with db.session_scope() as s:
        a = s.get(Account, account)
        a.status = "live"
        assert P.activate_if_requested(s, a) is True
    p = pros(account)
    assert p["enabled"] is True and p["enable_when_live"] is False and p["confirmed_at"]


def test_nothing_is_switched_on_without_the_recorded_acceptance(account):
    with db.session_scope() as s:
        a = s.get(Account, account)
        a.settings = {**(a.settings or {}), "prospecting": {"enable_when_live": True, "location_id": ZGZ, "cold_body": GOOD_BODY}}
        assert P.activate_if_requested(s, a) is False


def test_injected_html_cannot_break_out_of_the_page_state(account, client):
    t = token(account)
    r = client.post(f"/api/o/{t}/answers", json={"agent_name": "Eve</script><script>alert(1)</script>", "agency": "<img src=x onerror=alert(1)>"})
    assert r.status_code == 200
    with db.session_scope() as s:
        a = s.get(Account, account)
        assert "<" not in a.agent_name and ">" not in a.agent_name and "<" not in a.agency
    html = client.get(f"/o/{t}").text
    assert "</script><script>alert(1)" not in html
    page_state = html.split("let state = ", 1)[1].split(";\n", 1)[0]
    assert "<" not in page_state and json.loads(page_state)  # still valid JSON


def test_admin_refuses_the_public_default_password_when_live(client, monkeypatch):
    monkeypatch.setenv("WHATSAPP_MODE", "live")
    get_settings.cache_clear()
    assert client.get("/admin", auth=("admin", "change-me")).status_code == 503
    monkeypatch.setenv("ADMIN_PASSWORD", "a-real-secret")
    get_settings.cache_clear()
    assert client.get("/admin", auth=("admin", "a-real-secret")).status_code != 503
    assert client.get("/admin", auth=("admin", "change-me")).status_code == 401


def test_meta_na_rating_is_not_a_reason_to_pause():
    assert "NA" in P.QUALITY_OK and "RED" not in P.QUALITY_OK


def test_a_failed_pull_is_not_retried_by_the_queue(account, monkeypatch):
    from app import jobs

    def boom(s, a):
        raise RuntimeError("after the paid search")

    monkeypatch.setattr(P, "run_for_account", boom)
    with db.session_scope() as s:
        jobs.schedule(s, "prospect", db.utcnow(), account_id=account, dedupe_key="t:boom")
    jobs.run_until_idle()
    with db.session_scope() as s:
        from app.models import Event, Job
        j = s.execute(select(Job).where(Job.dedupe_key == "t:boom")).scalar_one()
        assert j.status == "done"
        assert s.execute(select(Event).where(Event.name == "prospect_error", Event.account_id == account)).scalars().all()
