import os
from datetime import datetime, timedelta

import pytest

os.environ.update(
    WHATSAPP_MODE="fake",
    CALENDAR_MODE="fake",
    LLM_MODE="fake",
    META_APP_SECRET="",
    SPARROW_PHONE_NUMBER_ID="sparrow",
    RUN_WORKER_IN_WEB="false",
    FIRST_MESSAGE_DELAY="30",
    REPLY_DELAY="30",
    REPLY_MAX_DELAY="60",
)

from app import db, engine, jobs, onboarding  # noqa: E402,F401
from app.config import get_settings  # noqa: E402
from app.crypto import encrypt  # noqa: E402
from app.gcal import FakeCalendar  # noqa: E402
from app.llm import FakeBackend  # noqa: E402
from app.models import Account, Integration  # noqa: E402
from app.whatsapp import FakeWhatsApp  # noqa: E402

# Tuesday 29 Sep 2026, 10:00 Madrid (08:00 UTC)
T0 = datetime(2026, 9, 29, 8, 0, 0)


class Clock:
    def set(self, dt):
        db.FROZEN_NOW = dt

    def advance(self, **kw):
        db.FROZEN_NOW = db.FROZEN_NOW + timedelta(**kw)

    @property
    def now(self):
        return db.FROZEN_NOW


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    pg = os.environ.get("TEST_DATABASE_URL")  # run the suite on Postgres too: TEST_DATABASE_URL=postgresql://...
    monkeypatch.setenv("DATABASE_URL", pg or f"sqlite:///{tmp_path}/test.db")
    get_settings.cache_clear()
    db.reset_engine()
    if pg:
        from app import models  # noqa: F401

        db.Base.metadata.drop_all(db.get_engine())
    db.init_db()
    FakeWhatsApp.reset()
    FakeCalendar.reset()
    FakeBackend.reset()
    engine._booking_locks.clear()
    db.FROZEN_NOW = T0
    yield
    db.FROZEN_NOW = None
    db.reset_engine()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def account():
    with db.session_scope() as s:
        a = Account(agent_name="Laura García", agency="RE/MAX Confort", city="Zaragoza", zones="Delicias, Actur",
                    personal_whatsapp="+34611111111", status="live", onboarding_step="done")
        s.add(a)
        s.flush()
        s.add(Integration(account_id=a.id, provider="meta", external={"waba_id": "w1", "phone_number_id": "pn1"}, token_enc=encrypt("t")))
        s.add(Integration(account_id=a.id, provider="google", external={"calendar_id": "primary"}, token_enc=encrypt("g")))
        return a.id


def run_jobs():
    jobs.run_until_idle()


def sent(to=None):
    return [m for m in FakeWhatsApp.outbox if to is None or m["to"] == to.lstrip("+")]
