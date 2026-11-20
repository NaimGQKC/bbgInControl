"""The agent's WhatsApp number quality is read from Meta and stored, so prospecting's auto-pause can see it."""
import pytest
from sqlalchemy import select

from app import db, quality
from app.config import get_settings
from app.models import Account, Event, Integration


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setenv("WHATSAPP_MODE", "live")
    get_settings.cache_clear()
    yield
    monkeypatch.setenv("WHATSAPP_MODE", "fake")
    get_settings.cache_clear()


def rating_of(account_id):
    with db.session_scope() as s:
        integ = s.execute(select(Integration).where(Integration.account_id == account_id, Integration.provider == "meta")).scalar_one()
        return (integ.external or {}).get("quality_rating")


def test_fake_mode_never_calls_meta(account, monkeypatch):
    monkeypatch.setattr(quality, "get_phone_number", lambda *a: pytest.fail("must not call Meta in fake mode"))
    with db.session_scope() as s:
        assert quality.refresh(s, s.get(Account, account)) is None


def test_refresh_stores_the_rating_and_logs_a_change(account, live, monkeypatch):
    monkeypatch.setattr(quality, "get_phone_number", lambda pnid, token: {"quality_rating": "yellow"})
    with db.session_scope() as s:
        assert quality.refresh(s, s.get(Account, account)) == "YELLOW"
    assert rating_of(account) == "YELLOW"
    with db.session_scope() as s:
        assert s.execute(select(Event).where(Event.name == "quality_rating", Event.account_id == account)).scalars().all()


def test_meta_error_keeps_the_old_rating(account, live, monkeypatch):
    from app.whatsapp import WhatsAppError

    def boom(*a):
        raise WhatsAppError("down")

    monkeypatch.setattr(quality, "get_phone_number", boom)
    with db.session_scope() as s:
        assert quality.refresh(s, s.get(Account, account)) is None
    assert rating_of(account) is None
