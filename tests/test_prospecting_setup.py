"""Prospecting set up by the agent in the onboarding chat (web endpoints), and the safety rules around the run:
template approval, cadence, ramp-up, auto-pause, admin line. Fake CASAFARI and WhatsApp, frozen clock."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import casafari, coldmsg, db, jobs, prospecting as P
from app.casafari import FakeCasafari
from app.config import get_settings
from app.main import app
from app.models import Account, Event, Integration, Job, Lead
from tests.conftest import run_jobs, sent

ZGZ = 312416
GOOD_BODY = "Hola {nombre}, soy el asistente virtual de {agente} ({agencia}). Vimos tu anuncio de {tipo} en {zona}. ¿Sigues vendiendo tú?"
ADMIN = ("admin", "change-me")


@pytest.fixture(autouse=True)
def env(monkeypatch):
    FakeCasafari.reset()
    monkeypatch.setenv("PROSPECTING_ENABLED", "true")
    monkeypatch.setenv("CASAFARI_MODE", "fake")
    monkeypatch.setenv("OPS_WHATSAPP", "+34622222222")
    get_settings.cache_clear()
    yield
    FakeCasafari.reset()
    get_settings.cache_clear()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def token(account_id):
    with db.session_scope() as s:
        return s.get(Account, account_id).public_token


def pros(account_id) -> dict:
    with db.session_scope() as s:
        return dict((s.get(Account, account_id).settings or {}).get("prospecting") or {})


def configure(account_id, **kw):
    with db.session_scope() as s:
        a = s.get(Account, account_id)
        a.settings = {**(a.settings or {}), "prospecting": {"enabled": True, "location_id": ZGZ, "zone_name": "Zaragoza", "daily_cap": 10, **kw}}


def run(account_id):
    with db.session_scope() as s:
        return P.run_for_account(s, s.get(Account, account_id))


def n_leads(account_id):
    with db.session_scope() as s:
        return s.execute(select(func.count(Lead.id)).where(Lead.account_id == account_id)).scalar()


def places(c, account, q):
    return c.get(f"/api/o/{token(account)}/places", params={"q": q})


def save(c, account, **kw):
    body = {"enabled": True, "location_id": ZGZ, "zone_name": "Zaragoza", "property_types": ["apartment"], "min_price": 100000,
            "max_price": 400000, "daily_cap": 5, "cold_body": GOOD_BODY, "success_pitch": "", "ack_risk": True}
    body.update(kw)
    return c.post(f"/api/o/{token(account)}/prospecting", json=body)


def offer_zone(c, account):
    assert places(c, account, "Zaragoza").json()["places"]


# ------------------------------------------------------------------ places

def test_places_spain_only_and_min_length(account, client, monkeypatch):
    monkeypatch.setattr(casafari, "FAKE_LOCATIONS", casafari.FAKE_LOCATIONS + [
        {"id": 999, "name": "Zaragoza Mexico", "kind": "city", "country_code": "MX", "breadcrumbs": ["Mexico"]},
        {"id": 998, "name": "Zaragoza region", "kind": "country", "country_code": "ES", "breadcrumbs": ["España"]}])
    assert places(client, account, "za").json() == {"places": []}  # under 3 characters: nothing, no call counted
    r = places(client, account, "Zaragoza").json()["places"]
    assert {p["id"] for p in r} == {312416, 312400}
    assert set(r[0]) == {"id", "name", "detail"} and r[0]["detail"]
    assert places(client, account, "xyzxyz").json() == {"places": []}


def test_places_rate_limit_per_account_per_hour(account, client, clock):
    for _ in range(20):
        assert places(client, account, "Madrid").status_code == 200
    r = places(client, account, "Madrid")
    assert r.status_code == 429 and r.json()["places"] == []
    clock.advance(hours=1)
    assert places(client, account, "Madrid").status_code == 200


def test_unknown_token_is_404(client):
    assert client.get("/api/o/nope/places", params={"q": "Madrid"}).status_code == 404


# ------------------------------------------------------------------ save: validation

def test_save_enables_and_records_the_acceptance(account, client, clock):
    offer_zone(client, account)
    r = save(client, account)
    assert r.json() == {"saved": True, "enabled": True}
    p = pros(account)
    assert p["enabled"] and p["location_id"] == ZGZ and p["property_types"] == ["apartment"] and p["daily_cap"] == 5
    assert p["confirmed_at"] == p["ack_at"] == clock.now.isoformat() and p["ack_text_version"] == 1
    assert p["cold_version"] == 1 and p["cold_template_status"] == "pending"
    state = client.get(f"/api/o/{token(account)}").json()["prospecting"]
    assert state["enabled"] and state["zone_name"] == "Zaragoza" and state["cold_body"] == GOOD_BODY
    assert state["risks"] == coldmsg.RISKS["es"] and state["cold_template_status"] == "pending" and state["done"] is True
    assert set(state) == {"enabled", "zone_name", "location_id", "property_types", "min_price", "max_price", "daily_cap", "cold_body",
                          "success_pitch", "risks", "paused_reason", "cold_template_status", "done"}


def test_state_before_setup_offers_the_default_body(account, client):
    st = client.get(f"/api/o/{token(account)}").json()["prospecting"]
    assert st["enabled"] is False and st["done"] is False and st["cold_body"] == coldmsg.default_body("es") and st["daily_cap"] == 5


def test_save_keeps_other_settings_and_bumps_version_only_on_change(account, client):
    with db.session_scope() as s:
        a = s.get(Account, account)
        a.settings = {**(a.settings or {}), "lang": "es", "slot_days": [0, 1]}
    offer_zone(client, account)
    save(client, account)
    save(client, account, daily_cap=7)  # same text: same version
    assert pros(account)["cold_version"] == 1
    with db.session_scope() as s:
        s.get(Account, account).settings = {**s.get(Account, account).settings, "prospecting": {**pros(account), "cold_template_status": "approved"}}
    save(client, account, cold_body=GOOD_BODY + " Gracias.", daily_cap=7)
    p = pros(account)
    assert p["cold_version"] == 2 and p["cold_template_status"] == "pending" and p["daily_cap"] == 7
    with db.session_scope() as s:
        assert s.get(Account, account).settings["slot_days"] == [0, 1]


def test_ack_is_required_to_enable(account, client):
    offer_zone(client, account)
    for bad in (False, None, "true", 1):
        r = save(client, account, ack_risk=bad)
        assert r.status_code == 400 and r.json()["field"] == "ack_risk"
    assert pros(account).get("enabled") is not True
    assert save(client, account, enabled=False, ack_risk=False).json() == {"saved": True, "enabled": False}  # switching off needs no ack


@pytest.mark.parametrize("kw,field", [
    ({"location_id": 424242}, "location_id"),  # not from the ES lookup
    ({"location_id": "abc"}, "location_id"),
    ({"location_id": None}, "location_id"),
    ({"property_types": ["plot"]}, "property_types"),
    ({"property_types": []}, "property_types"),
    ({"property_types": "apartment"}, "property_types"),
    ({"min_price": -1}, "min_price"),
    ({"max_price": 20_000_001}, "max_price"),
    ({"min_price": 300000, "max_price": 200000}, "min_price"),
    ({"min_price": "abc"}, "min_price"),
    ({"success_pitch": "x" * 401}, "success_pitch"),
    ({"cold_body": "Hola"}, "cold_body"),  # too short
    ({"cold_body": GOOD_BODY + " https://example.com"}, "cold_body"),  # link
    ({"cold_body": "{nombre} " + "a" * 30 + " {zona} {precio}"}, "cold_body"),  # unknown field
    ({"cold_body": GOOD_BODY + "x" * 600}, "cold_body"),  # too long
])
def test_validation_rules(account, client, kw, field):
    offer_zone(client, account)
    r = save(client, account, **kw)
    assert r.status_code == 400, kw
    assert r.json()["ok"] is False and r.json()["field"] == field
    assert pros(account).get("enabled") is not True


def test_cold_body_problems_are_listed(account, client):
    offer_zone(client, account)
    r = save(client, account, cold_body="Hola {foo} que tal estas hoy amigo, mira www.x.es")
    assert r.status_code == 400 and "link_not_allowed" in r.json()["problems"] and "unknown_field:foo" in r.json()["problems"]


def test_edge_values_are_accepted_and_cap_is_clamped(account, client):
    offer_zone(client, account)
    assert save(client, account, min_price=0, max_price=20_000_000, success_pitch="x" * 400, daily_cap=99).json()["saved"]
    assert pros(account)["daily_cap"] == 20
    assert save(client, account, min_price=None, max_price=None, daily_cap=0).json()["saved"]
    p = pros(account)
    assert p["daily_cap"] == 1 and p["min_price"] is None and p["max_price"] is None
    assert save(client, account, daily_cap="oops").json()["saved"] and pros(account)["daily_cap"] == 5
    assert save(client, account, property_types=["house", "apartment"]).json()["saved"] and pros(account)["property_types"] == ["apartment", "house"]


def test_not_live_saves_but_stays_off(account, client):
    with db.session_scope() as s:
        s.get(Account, account).status = "onboarding"
    offer_zone(client, account)
    assert save(client, account).json() == {"saved": True, "enabled": False, "reason": "not_live"}
    p = pros(account)
    assert p["enabled"] is False and p["cold_body"] == GOOD_BODY and "confirmed_at" not in p


def test_a_paused_account_cannot_be_turned_on_by_the_agent(account, client):
    offer_zone(client, account)
    save(client, account)
    with db.session_scope() as s:
        P.pause(s, s.get(Account, account), "opt_outs", tell_agent=False)
    r = save(client, account).json()
    assert r["enabled"] is False and r["reason"] == "paused" and r["paused_reason"] == "opt_outs"
    assert pros(account)["paused_reason"] == "opt_outs"


# ------------------------------------------------------------------ preview

def test_preview_renders_with_sample_values_and_the_footer(account, client):
    r = client.post(f"/api/o/{token(account)}/prospecting/preview", json={"cold_body": GOOD_BODY}).json()
    assert set(r) == {"text", "problems", "warnings"} and r["problems"] == [] and r["warnings"] == []
    assert "Hola Marta, soy el asistente virtual de Laura (Inmobiliaria Sur)" in r["text"]
    assert "BAJA" in r["text"] and r["text"].endswith("/privacidad") and "{" not in r["text"]


def test_preview_reports_problems_and_warnings(account, client):
    r = client.post(f"/api/o/{token(account)}/prospecting/preview", json={"cold_body": "Hola {nombre} te lo vendo GRATIS y garantizado en {zona}!"}).json()
    assert "promise_words" in r["warnings"]
    r = client.post(f"/api/o/{token(account)}/prospecting/preview", json={"cold_body": "mira https://x.es"}).json()
    assert "link_not_allowed" in r["problems"] and "too_short" in r["problems"]


def test_preview_uses_the_leads_language(account, client):
    with db.session_scope() as s:
        s.get(Account, account).settings = {"lang": "en"}
    r = client.post(f"/api/o/{token(account)}/prospecting/preview", json={"cold_body": coldmsg.default_body("en")}).json()
    assert "Hi Emma" in r["text"] and "reply STOP" in r["text"]


# ------------------------------------------------------------------ the run: template, ramp

def test_run_waits_for_the_approved_template(account):
    configure(account, cold_body=GOOD_BODY, cold_template_status="pending")
    assert run(account) == {"ran": False, "reason": "template_pending"}
    assert FakeCasafari.calls == [] and n_leads(account) == 0
    configure(account, cold_body=GOOD_BODY, cold_template_status="rejected")
    assert run(account)["reason"] == "template_pending"
    configure(account, cold_body=GOOD_BODY, cold_template_status="approved")
    assert run(account)["created"] == 10 and len(FakeCasafari.calls) == 1


def test_ramp_up_after_confirmation(account, clock):
    def cap_after(days):
        return P.effective_cap({"daily_cap": 10, "confirmed_at": (clock.now - timedelta(days=days, hours=1)).isoformat()}, clock.now)
    assert [cap_after(d) for d in (0, 2, 3, 6, 7, 30)] == [3, 3, 5, 5, 10, 10]
    assert P.effective_cap({"daily_cap": 2, "confirmed_at": clock.now.isoformat()}, clock.now) == 2  # the agent's own cap is lower
    assert P.effective_cap({"daily_cap": 10}, clock.now) == 10  # set by hand: no ramp


def test_ramp_limits_what_a_run_creates(account, clock):
    configure(account, cold_body=GOOD_BODY, cold_template_status="approved", confirmed_at=(clock.now - timedelta(days=1)).isoformat())
    assert run(account)["created"] == 3
    assert run(account)["created"] == 0  # same day: the cap is used up
    clock.advance(days=3)
    assert run(account)["created"] == 5  # day 4: up to 5 a day


# ------------------------------------------------------------------ cadence

def test_pulls_every_poll_interval_inside_allowed_hours(account, clock, monkeypatch):
    configure(account)
    with db.session_scope() as s:
        assert P.schedule_prospecting(s) == 1
        assert P.schedule_prospecting(s) == 0  # one pending job per account
    run_jobs()
    with db.session_scope() as s:
        assert s.execute(select(func.count(Event.id)).where(Event.name == "prospect_run")).scalar() == 1
        clock.advance(hours=1)
        assert P.schedule_prospecting(s) == 0  # ran 1 h ago, poll is 3 h
        clock.advance(hours=2)
        assert P.schedule_prospecting(s) == 1
    monkeypatch.setenv("PROSPECTING_POLL_HOURS", "1")
    get_settings.cache_clear()
    run_jobs()
    clock.advance(hours=1)
    with db.session_scope() as s:
        assert P.schedule_prospecting(s) == 1


def test_scan_repeats_every_poll_interval(account, clock):
    with db.session_scope() as s:
        jobs.schedule(s, "prospect_scan", db.utcnow(), dedupe_key="prospect_scan:first")
    run_jobs()
    with db.session_scope() as s:
        nxt = s.execute(select(Job).where(Job.type == "prospect_scan", Job.status == "pending")).scalar_one()
        assert nxt.run_at == clock.now + timedelta(hours=3)


def test_no_pull_outside_allowed_hours(account, clock):
    configure(account)
    clock.set(clock.now.replace(hour=21, minute=30))  # 23:30 Madrid
    with db.session_scope() as s:
        assert P.schedule_prospecting(s) == 1
        job = s.execute(select(Job).where(Job.type == "prospect")).scalar_one()
        assert job.run_at == clock.now.replace(hour=7, minute=0) + timedelta(days=1)  # 09:00 Madrid
    run_jobs()
    assert FakeCasafari.calls == []


# ------------------------------------------------------------------ auto-pause

def set_quality(account, rating):
    with db.session_scope() as s:
        integ = s.execute(select(Integration).where(Integration.account_id == account, Integration.provider == "meta")).scalar_one()
        integ.external = {**integ.external, "quality_rating": rating}


def test_green_or_unknown_quality_is_healthy(account):
    configure(account)
    set_quality(account, "GREEN")
    assert run(account)["ran"] is True


@pytest.mark.parametrize("rating", ["RED", "YELLOW", "FLAGGED"])
def test_bad_quality_pauses_before_any_pull(account, rating):
    configure(account)
    set_quality(account, rating)
    r = run(account)
    assert r == {"ran": False, "reason": "paused", "paused_reason": "quality"}
    assert FakeCasafari.calls == [] and n_leads(account) == 0
    p = pros(account)
    assert p["enabled"] is False and p["paused_reason"] == "quality"
    assert [m for m in sent("+34622222222") if "Prospecting pausado" in m["text"]]  # operator
    assert [m for m in sent("+34611111111") if "prospección automática" in m["text"]]  # agent, in Spanish
    assert run(account)["reason"] == "disabled"  # nothing resumes by itself
    with db.session_scope() as s:
        assert s.execute(select(func.count(Event.id)).where(Event.name == "prospect_paused")).scalar() == 1


def add_prospect_leads(account, total, opted_out):
    with db.session_scope() as s:
        for i in range(total):
            s.add(Lead(account_id=account, phone=f"+346009{i:05d}", source="casafari", status="opted_out" if i < opted_out else "contacted",
                       do_not_contact=i < opted_out))


def test_opt_out_rate_over_15_percent_pauses(account):
    configure(account)
    add_prospect_leads(account, 20, 4)  # 20%
    r = run(account)
    assert r["reason"] == "paused" and r["paused_reason"] == "opt_outs" and FakeCasafari.calls == []
    assert pros(account)["paused_reason"] == "opt_outs"
    assert sent("+34611111111") and sent("+34622222222")


def test_opt_out_rate_at_or_under_15_percent_keeps_running(account):
    configure(account)
    add_prospect_leads(account, 20, 3)  # exactly 15%: not over
    assert run(account)["ran"] is True


def test_opt_out_rate_needs_a_minimum_sample(account):
    configure(account)
    add_prospect_leads(account, 4, 2)  # 50% but only 4 leads: no verdict yet
    assert run(account)["ran"] is True
    assert pros(account)["enabled"] is True


def test_opt_out_rate_only_counts_the_last_50(account):
    configure(account)
    with db.session_scope() as s:
        for i in range(5):  # old opt-outs, before 50 newer clean leads
            s.add(Lead(account_id=account, phone=f"+346008{i:05d}", source="casafari", do_not_contact=True))
        s.flush()
        for i in range(50):
            s.add(Lead(account_id=account, phone=f"+346007{i:05d}", source="casafari"))
    with db.session_scope() as s:
        assert P.opt_out_rate(s, s.get(Account, account)) == (0, 50)
        assert P.health_problem(s, s.get(Account, account)) is None


def test_402_pauses_and_only_the_operator_hears(account):
    configure(account)
    FakeCasafari.fail_with = casafari.CasafariOutOfTokens("no tokens", "https://example.test/upgrade")
    r = run(account)
    assert r["error"] == "CasafariOutOfTokens" and r["created"] == 0
    p = pros(account)
    assert p["enabled"] is False and p["paused_reason"] == "source_credit"
    assert [m for m in sent("+34622222222") if "CASAFARI" in m["text"]]
    assert sent("+34611111111") == []
    assert run(account)["reason"] == "disabled"


def test_429_does_not_pause(account):
    configure(account)
    FakeCasafari.fail_with = casafari.CasafariRateLimited("slow", 30)
    assert run(account)["error"] == "CasafariRateLimited"
    assert pros(account)["enabled"] is True


# ------------------------------------------------------------------ admin

def test_admin_line_pause_and_resume(account, client, clock):
    configure(account, cold_body=GOOD_BODY, cold_template_status="approved", daily_cap=4)
    run(account)
    assert client.get("/admin/prospecting").status_code == 401
    page = client.get("/admin/prospecting", auth=ADMIN)
    assert page.status_code == 200 and "Zaragoza" in page.text and "ON" in page.text and "approved" in page.text and "4/4" in page.text
    with db.session_scope() as s:
        from app.admin import prospecting_line
        ln = prospecting_line(s, s.get(Account, account))
    assert ln["enabled"] and ln["zone"] == "Zaragoza" and ln["leads_today"] == 4 and ln["last_run"]["created"] == 4
    assert ln["paused_reason"] == "" and ln["cold_template_status"] == "approved"

    r = client.post(f"/admin/accounts/{account}/prospecting", data={"action": "pause"}, auth=ADMIN, follow_redirects=False)
    assert r.status_code == 303
    assert pros(account)["enabled"] is False and pros(account)["paused_reason"] == "operator"
    page = client.get("/admin/prospecting", auth=ADMIN)
    assert "operator" in page.text and "Reanudar" in page.text

    clock.advance(days=10)
    client.post(f"/admin/accounts/{account}/prospecting", data={"action": "resume"}, auth=ADMIN)
    p = pros(account)
    assert p["enabled"] is True and p["paused_reason"] == "" and p["confirmed_at"] == clock.now.isoformat()  # ramp starts again
    with db.session_scope() as s:
        assert P.effective_cap(P.config_for(s.get(Account, account)), clock.now) == 3
    assert client.post(f"/admin/accounts/{account}/prospecting", data={"action": "pause"}).status_code == 401


def test_admin_account_page_still_opens(account, client):
    assert client.get(f"/admin/accounts/{account}", auth=ADMIN).status_code == 200
