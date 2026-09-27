"""Hard rules enforced in code, whatever the LLM decides (design doc sections 1.5 and 11)."""
import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from .db import utcnow
from .models import Account, Lead

HOT_STAGES = {"exclusiva", "cerrado", "cerrada", "vendido", "vendida", "firmado", "en exclusiva", "reservado"}
NO_AI_TAGS = {"no ia", "sin ia", "no_ia", "noia"}
OPT_OUT_WORDS = {"stop", "baja", "stop.", "baja."}

DEFAULT_SETTINGS = {
    # Allowed hours for first contact / follow-ups (local time). Days: 0=Monday ... 6=Sunday
    "contact_days": [0, 1, 2, 3, 4, 5],
    "contact_start": "09:00",
    "contact_end": "21:00",
    # Valuation slots
    "slot_windows": [["10:00", "13:30"], ["16:30", "20:00"]],
    "slot_days": [0, 1, 2, 3, 4, 5],
    "meeting_minutes": 45,
    "min_notice_hours": 2,
    "days_ahead": 6,
    "buffer_minutes": 15,
}


def settings_for(account: Account) -> dict:
    s = dict(DEFAULT_SETTINGS)
    s.update(account.settings or {})
    return s


# ---------- phones ----------

def normalize_phone(raw: str | None, default_cc: str = "34") -> str | None:
    """Return E.164 (+34600000001) or None if it doesn't look valid."""
    if not raw:
        return None
    digits = re.sub(r"[^\d+]", "", str(raw))
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if not digits.startswith("+"):
        digits = digits.lstrip("0")
        if len(digits) == 9:  # Spanish national number
            digits = "+" + default_cc + digits
        else:
            digits = "+" + digits
    num = digits[1:]
    if not num.isdigit() or not (8 <= len(num) <= 15):
        return None
    if num.startswith("34") and len(num) != 11:
        return None
    return "+" + num


def wa_id(phone_e164: str) -> str:
    """WhatsApp uses the number without '+'."""
    return phone_e164.lstrip("+")


# ---------- time ----------

def _hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def to_local(dt_utc: datetime, tz: str) -> datetime:
    return dt_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(tz))


def to_utc_naive(dt_local: datetime) -> datetime:
    return dt_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def in_allowed_hours(account: Account, when_utc: datetime | None = None) -> bool:
    s = settings_for(account)
    local = to_local(when_utc or utcnow(), account.timezone)
    if local.weekday() not in s["contact_days"]:
        return False
    return _hm(s["contact_start"]) <= local.time() < _hm(s["contact_end"])


def next_allowed_time(account: Account, after_utc: datetime) -> datetime:
    """First moment >= after_utc inside allowed hours (naive UTC). Capped to 09:00-21:00 whatever the settings."""
    s = settings_for(account)
    tz = ZoneInfo(account.timezone)
    start_t = max(_hm(s["contact_start"]), time(9, 0))
    end_t = min(_hm(s["contact_end"]), time(21, 0))
    local = to_local(after_utc, account.timezone)
    for _ in range(15):
        day_start = datetime.combine(local.date(), start_t, tzinfo=tz)
        day_end = datetime.combine(local.date(), end_t, tzinfo=tz)
        if local.weekday() in s["contact_days"]:
            if local < day_start:
                return to_utc_naive(day_start)
            if local < day_end:
                return to_utc_naive(local)
        local = datetime.combine(local.date() + timedelta(days=1), time(0, 0), tzinfo=tz)
    return after_utc


def window_open(lead: Lead, now: datetime | None = None) -> bool:
    """G7: free-form messages only within 24h of the lead's last message."""
    if not lead.last_inbound_at:
        return False
    return (now or utcnow()) < lead.last_inbound_at + timedelta(hours=24)


# ---------- the single gate ----------

def can_contact(account: Account, lead: Lead, *, kind: str) -> tuple[bool, str]:
    """kind: first|followup|ai_reply|reminder|confirmation|opt_out_confirmation|handoff_notice

    Every outgoing message to a lead goes through here.
    """
    if kind != "opt_out_confirmation" and lead.do_not_contact:
        return False, "do_not_contact"  # G2
    if account.status == "paused":
        return False, "account_paused"  # G11
    if account.status != "live" and not lead.is_test:
        return False, "account_not_live"
    if any(t.strip().lower() in NO_AI_TAGS for t in (lead.tags or [])):
        return False, "no_ia_tag"  # G8
    if kind in ("first", "followup", "ai_reply") and lead.human_owned:
        return False, "human_owned"  # G8
    if kind in ("first", "followup") and (lead.stage or "").strip().lower() in HOT_STAGES:
        return False, "hot_stage"
    if kind in ("first", "followup") and lead.lead_type == "buyer":
        return False, "buyer"  # G14
    if kind in ("first", "followup") and not in_allowed_hours(account):
        return False, "outside_hours"  # G9
    return True, "ok"
