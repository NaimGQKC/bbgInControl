"""Prospecting from CASAFARI private-seller listings (app/prospecting.py), all with the fake client and the frozen clock."""
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from app import casafari, db, engine, jobs, prospecting as P
from app.casafari import FakeCasafari, _l
from app.config import get_settings
from app.models import Account, Event, Job, Lead, Message
from app.prompt import build_system_prompt
from tests.conftest import run_jobs, sent

ZGZ = 312416
ON = {"enabled": True, "location_id": ZGZ, "zone_name": "Zaragoza", "daily_cap": 10}


@pytest.fixture(autouse=True)
def fake_casafari(monkeypatch):
    FakeCasafari.reset()
    monkeypatch.setenv("PROSPECTING_ENABLED", "true")
    monkeypatch.setenv("CASAFARI_MODE", "fake")
    get_settings.cache_clear()
    yield
    FakeCasafari.reset()
    get_settings.cache_clear()


def configure(account_id, **kw):
    with db.session_scope() as s:
        a = s.get(Account, account_id)
        a.settings = {**(a.settings or {}), "prospecting": {**ON, **kw}}


def run(account_id):
    with db.session_scope() as s:
        return P.run_for_account(s, s.get(Account, account_id))


def leads(account_id):
    with db.session_scope() as s:
        return s.execute(select(Lead).where(Lead.account_id == account_id).order_by(Lead.id)).scalars().all()


def by_ref(account_id):
    return {lead.source_ref: lead for lead in leads(account_id)}


# ------------------------------------------------------------------ off by default

def test_disabled_by_default(account):
    r = run(account)
    assert r == {"ran": False, "reason": "disabled"}
    assert FakeCasafari.calls == [] and leads(account) == []


def test_kill_switch_beats_account_setting(account, monkeypatch):
    configure(account)
    monkeypatch.setenv("PROSPECTING_ENABLED", "false")
    get_settings.cache_clear()
    assert run(account)["reason"] == "kill_switch"
    with db.session_scope() as s:
        assert P.schedule_prospecting(s) == 0
    assert FakeCasafari.calls == [] and leads(account) == []


def test_not_live_or_not_spain_does_not_fetch(account):
    configure(account)
    with db.session_scope() as s:
        s.get(Account, account).status = "onboarding"
    assert run(account)["reason"] == "account_not_live"
    with db.session_scope() as s:
        a = s.get(Account, account)
        a.status = "live"
        a.settings = {**a.settings, "lang": "fr"}
    assert run(account)["reason"] == "not_spain"
    assert FakeCasafari.calls == []


# ------------------------------------------------------------------ the run

def test_run_filters_dedupes_and_caps(account):
    configure(account, daily_cap=50)
    r = run(account)
    assert r["searches"] == 1 and r["tokens"] == 10
    refs = by_ref(account)
    assert "psl_fake001" in refs and "psl_fake002" not in refs  # same flat on Fotocasa: cross-portal duplicate
    assert "psl_fake014" in refs and "psl_fake015" not in refs
    assert "psl_fake004" not in refs  # no phone
    assert "psl_fake005" not in refs and "psl_fake006" not in refs  # plot, investment
    assert "psl_fake007" not in refs  # foreign number
    assert "psl_fake008" not in refs  # same phone as 001
    assert "psl_fake017" not in refs  # invalid phone
    assert "psl_fake012" in refs and refs["psl_fake012"].phone == "+34934000012"  # landline is fine
    assert "psl_fake016" in refs  # no price/area: kept (can't be a cross-portal duplicate)
    sk = r["skipped"]
    assert sk["dup_cross_portal"] == 2 and sk["dup_phone"] == 1 and sk["no_phone"] == 1
    assert sk["property_type"] == 2 and sk["foreign_phone"] == 1 and sk["invalid_phone"] == 1
    assert r["created"] == len(refs) == 15
    lead = refs["psl_fake001"]
    assert lead.source == "casafari" and lead.lead_type == "seller" and lead.status == "queued"
    assert lead.name == "Maria Lopez" and lead.zone == "Zaragoza" and lead.property_type == "piso"
    assert lead.price == 185000 and lead.listing_url.endswith("/idealista/1")
    assert refs["psl_fake009"].name == ""  # "Particular" is not a name
    with db.session_scope() as s:
        ev = s.execute(select(Event).where(Event.name == "prospect_run")).scalar_one()
        assert ev.data["fetched"] == 23 and ev.data["created"] == 15
        assert "+346" not in str(ev.data) and "Maria" not in str(ev.data)  # counts only, no personal data


def test_daily_cap(account, clock):
    configure(account, daily_cap=3)
    r = run(account)
    assert r["created"] == 3 and r["skipped"]["daily_cap"] > 0
    r = run(account)  # same day: nothing more, and no tokens spent
    assert r["created"] == 0 and r["searches"] == 0 and len(FakeCasafari.calls) == 1
    clock.advance(days=1)
    r = run(account)  # the cap left listings behind, so the next day looks at the same window again
    assert FakeCasafari.calls[-1]["since"] == "2026-09-23"  # not today: 7 days back again, not further
    assert r["created"] == 3 and len(leads(account)) == 6


def test_complete_run_moves_the_window_forward(account, clock):
    configure(account, daily_cap=50)
    run(account)
    clock.advance(days=1)
    run(account)
    assert FakeCasafari.calls[0]["since"] == "2026-09-22" and FakeCasafari.calls[1]["since"] == "2026-09-29"


def _many(n, every=1):
    return [_l(100 + i, "Idealista", "apartment" if i % every == 0 else "plot", 100000 + i * 1000, 50 + i, f"+346002{i:05d}")
            for i in range(n)]


def test_hard_caps_on_pages_and_daily_cap(account, monkeypatch):
    monkeypatch.setenv("PROSPECTING_MAX_PAGES_PER_RUN", "99")
    get_settings.cache_clear()
    configure(account, daily_cap=1000)
    FakeCasafari.listings = _many(200, every=10)  # few usable ones: it keeps paging, but never past the hard limit
    r = run(account)
    assert r["searches"] == P.MAX_PAGES_HARD and r["created"] == 10 and r["complete"] is False
    FakeCasafari.listings = _many(200)
    r = run(account)
    assert r["created"] == P.MAX_DAILY_CAP - 10 and r["searches"] == 2  # stops paging once the cap is reached


def test_one_page_per_run_by_default(account):
    configure(account, daily_cap=50)
    FakeCasafari.listings = _many(60)
    r = run(account)
    assert r["searches"] == 1 and r["created"] == 25 and len(FakeCasafari.calls) == 1


def test_price_range_and_property_types(account):
    configure(account, min_price=150000, max_price=300000, property_types=["house"], daily_cap=50)
    run(account)
    refs = by_ref(account)
    assert set(refs) == {"psl_fake012", "psl_fake020"}  # houses inside the range (003 is 320k)


def test_idempotent_rerun_creates_no_duplicates(account, clock):
    configure(account, daily_cap=50)
    FakeCasafari.listings = [dict(x, first_seen="2026-09-29") for x in casafari.FAKE_LISTINGS]  # still in the next window
    run(account)
    n = len(leads(account))
    clock.advance(hours=3)
    r = run(account)
    assert r["created"] == 0 and len(leads(account)) == n
    assert r["skipped"]["dup_url"] == n  # known listings are recognised before anything else


def test_cross_portal_duplicate_of_an_existing_lead(account, clock):
    configure(account, daily_cap=50)
    FakeCasafari.listings = [_l(1, "Idealista", "apartment", 185000, 90, "+34600100001")]
    run(account)
    clock.advance(days=1)
    FakeCasafari.listings = [_l(2, "Fotocasa for Sale", "apartment", 185000, 90, "+34600100002", first_seen="2026-09-30")]
    r = run(account)
    assert r["created"] == 0 and r["skipped"]["dup_cross_portal"] == 1


def test_existing_lead_from_another_source_is_left_alone(account):
    configure(account, daily_cap=50)
    with db.session_scope() as s:
        engine.ingest_lead(s, s.get(Account, account), {"name": "Marta", "phone": "+34600100003", "source": "lystos"})
    r = run(account)
    assert r["skipped"]["already_lead"] == 1
    lead = [x for x in leads(account) if x.phone == "+34600100003"][0]
    assert lead.source == "lystos" and lead.listing_url == ""


def test_do_not_contact_respected_across_accounts(account):
    configure(account, daily_cap=50)
    with db.session_scope() as s:
        other = Account(agent_name="Otro", status="live")
        s.add(other)
        s.flush()
        s.add(Lead(account_id=other.id, phone="+34600100001", do_not_contact=True, status="opted_out"))
    r = run(account)
    assert r["skipped"]["do_not_contact"] == 1
    assert "psl_fake001" not in by_ref(account)


def test_api_errors_stop_the_run_without_retry(account):
    configure(account)
    FakeCasafari.fail_with = casafari.CasafariOutOfTokens("no tokens", "https://example.test/upgrade")
    r = run(account)
    assert r["error"] == "CasafariOutOfTokens" and r["created"] == 0
    FakeCasafari.fail_with = casafari.CasafariRateLimited("slow down", 30)
    assert run(account)["error"] == "CasafariRateLimited"


# ------------------------------------------------------------------ the normal pipeline still decides

def test_first_message_goes_through_the_normal_guards(account, clock):
    configure(account, daily_cap=1)
    run(account)
    clock.advance(seconds=31)
    run_jobs()
    out = sent("+34600100001")
    assert len(out) == 1 and out[0]["template"] == "lead_first_contact_v2"
    assert "asistente virtual de Laura" in out[0]["text"] and "Vi tu anuncio en Zaragoza" in out[0]["text"]


def test_outside_allowed_hours_nothing_is_sent(account, clock):
    configure(account, daily_cap=2)
    clock.set(clock.now.replace(hour=20, minute=30))  # 22:30 Madrid: ingest now, but can_contact holds the message
    run(account)
    assert len(leads(account)) == 2
    clock.advance(minutes=5)
    run_jobs()
    assert sent() == []
    clock.set(clock.now.replace(hour=7, minute=1) + timedelta(days=1))  # 09:01 Madrid
    run_jobs()
    assert len(sent()) == 2


def test_prospect_job_waits_for_allowed_hours(account, clock):
    configure(account)
    clock.set(clock.now.replace(hour=21, minute=0))  # 23:00 Madrid
    with db.session_scope() as s:
        assert P.schedule_prospecting(s) == 1
        job = s.execute(select(Job).where(Job.type == "prospect")).scalar_one()
        assert job.run_at == clock.now.replace(hour=7, minute=0) + timedelta(days=1)  # 09:00 Madrid tomorrow
        assert P.schedule_prospecting(s) == 0  # once per account per day
    run_jobs()
    assert FakeCasafari.calls == []
    clock.set(clock.now.replace(hour=7, minute=1) + timedelta(days=1))
    run_jobs()
    assert len(FakeCasafari.calls) == 1 and len(leads(account)) == 10


def test_scan_skips_accounts_without_prospecting(account):
    with db.session_scope() as s:
        jobs.schedule(s, "prospect_scan", db.utcnow(), dedupe_key="prospect_scan:test")
    run_jobs()
    with db.session_scope() as s:
        assert s.execute(select(func.count(Job.id)).where(Job.type == "prospect")).scalar() == 0
        assert s.execute(select(Job).where(Job.type == "prospect_scan", Job.status == "pending")).scalar_one()  # tomorrow's


# ------------------------------------------------------------------ source record and expiry

def test_source_and_expiry_recorded_but_hidden_from_the_ai(account):
    configure(account, daily_cap=1)
    run(account)
    lead = leads(account)[0]
    src = lead.facts["_source"]
    assert src["provider"] == "casafari" and src["listing_id"] == "psl_fake001" and src["portal"] == "Idealista"
    assert src["expires"] == (date(2026, 9, 29) + timedelta(days=365)).isoformat()
    assert "contact" not in src and "coordinates" not in src  # only what the lead needs, not the raw payload
    with db.session_scope() as s:
        prompt = build_system_prompt(s.get(Account, account), lead, must_introduce=True)
    assert "_source" not in prompt and "expires" not in prompt


def test_expiry_follows_casafari_closure(account, monkeypatch):
    monkeypatch.setenv("CASAFARI_CLOSED_ON", "2026-10-15")
    get_settings.cache_clear()
    configure(account, daily_cap=1)
    run(account)
    assert leads(account)[0].facts["_source"]["expires"] == "2026-11-14"


def test_cleanup_deletes_expired_unanswered_leads(account, clock):
    configure(account, daily_cap=3)
    run(account)
    clock.advance(seconds=31)
    run_jobs()  # first messages sent
    ids = [x.id for x in leads(account)]
    with db.session_scope() as s:
        replied = s.get(Lead, ids[0])
        replied.has_replied = True
        opted = s.get(Lead, ids[1])
        opted.do_not_contact = True
        opted_phone = opted.phone
        s.add(Lead(account_id=account, phone="+34600999999", source="lystos"))  # other sources are never touched
    clock.advance(days=364)
    with db.session_scope() as s:
        assert P.cleanup_expired(s) == {"deleted": 0, "stripped": 0}
    clock.advance(days=2)
    with db.session_scope() as s:
        assert P.cleanup_expired(s) == {"deleted": 2, "stripped": 1}
    left = leads(account)
    assert {x.source for x in left} == {"casafari", "lystos"} and len(left) == 2
    kept = [x for x in left if x.source == "casafari"][0]
    assert kept.id == ids[0] and kept.listing_url == "" and kept.source_ref == "" and "_source" not in kept.facts
    with db.session_scope() as s:
        assert s.execute(select(func.count(Message.id)).where(Message.lead_id.in_(ids[1:]))).scalar() == 0
    # the opted-out number is remembered only as a hash, and is never prospected again
    clock.advance(days=1)
    FakeCasafari.listings = [_l(900, "Idealista", "apartment", 111000, 51, opted_phone, first_seen=f"{clock.now:%Y-%m-%d}")]
    r = run(account)
    assert r["skipped"]["do_not_contact"] == 1 and r["created"] == 0


# ------------------------------------------------------------------ pure helpers

def test_spanish_phone_and_masking():
    assert P.spanish_phone("600 100 001") == "+34600100001"
    assert P.spanish_phone("+34 711 100 018") == "+34711100018"
    assert P.spanish_phone("934000012") == "+34934000012"
    assert P.spanish_phone("+447700900123") is None
    assert P.spanish_phone("+33612345678") is None
    assert P.spanish_phone("+34500000000") is None
    assert P.mask_phone("+34600100001") == "+34 6** *** *01"


def test_filter_too_old_and_rent():
    cfg = dict(P.DEFAULTS)
    kept, skipped = P.filter_listings([_l(1, "Idealista", "apartment", 1, 1, "+34600100001", first_seen="2026-01-01"),
                                       _l(2, "Idealista", "apartment", 1, 1, "+34600100002", op="rent")], cfg, date(2026, 9, 1))
    assert kept == [] and skipped == {"too_old": 1, "operation": 1}
