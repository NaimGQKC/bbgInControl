"""WhatsApp number quality (Meta's GREEN / YELLOW / RED) for each agent's number.

Prospecting reads `Integration(meta).external["quality_rating"]` and pauses itself when it is not healthy
(see prospecting.QUALITY_OK). This module is the only thing that writes it: it asks Meta for the rating of
the agent's number (refresh before every prospecting scan, and straight away when Meta sends a
`phone_number_quality_update` webhook). Fake WhatsApp mode never calls Meta.
"""
import logging

from sqlalchemy import select

from .config import get_settings
from .crypto import decrypt
from .models import Account, Event, Integration
from .whatsapp import WhatsAppError, get_phone_number

log = logging.getLogger("sparrow.quality")


def refresh(s, account: Account) -> str | None:
    """Store and return the current quality rating of the account's number (None if it could not be read)."""
    if get_settings().whatsapp_mode == "fake":
        return None
    integ = s.execute(select(Integration).where(Integration.account_id == account.id, Integration.provider == "meta")).scalar_one_or_none()
    if integ is None or not integ.token_enc or not (integ.external or {}).get("phone_number_id"):
        return None
    try:
        info = get_phone_number(integ.external["phone_number_id"], decrypt(integ.token_enc))
    except WhatsAppError as e:
        log.warning("quality refresh failed for account %s: %s", account.id, e)
        return None
    rating = str(info.get("quality_rating") or "").upper()
    if rating and rating != (integ.external or {}).get("quality_rating"):
        integ.external = {**integ.external, "quality_rating": rating}
        s.add(Event(name="quality_rating", account_id=account.id, data={"rating": rating}))
    return rating or None


def refresh_prospecting_accounts(s, accounts) -> None:
    for a in accounts:
        refresh(s, a)
