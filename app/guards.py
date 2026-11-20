"""Hard rules enforced in code, whatever the LLM decides (design doc sections 1.5 and 11)."""
import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from .db import utcnow
from .models import Account, Lead

HOT_STAGES = {"exclusiva", "cerrado", "cerrada", "vendido", "vendida", "firmado", "en exclusiva", "reservado"}
NO_AI_TAGS = {"no ia", "sin ia", "no_ia", "noia"}
OPT_OUT_WORDS = {"stop", "baja", "arret", "arrêt", "desabonner", "désabonner", "unsubscribe"}  # es / fr / en  # compared after lower-casing and stripping trailing . !

COLD_SOURCES = {"casafari"}  # leads we found ourselves (a private owner's own public listing): they never asked to be contacted


def is_cold(lead: Lead) -> bool:
    """A private owner found through prospecting (CASAFARI), not someone who asked the agent for anything."""
    src = (lead.facts or {}).get("_source")
    return (isinstance(src, dict) and src.get("provider") in COLD_SOURCES) or (lead.source or "").lower() in COLD_SOURCES


# Cold owners: replies that end the conversation at once, in code, before (and whatever) the model says.
# "data" = a data-protection request (also handed to the agent, who must answer it); "threat" = reporting / legal
# threats (handed to the agent too); "stop" = "don't write to me again" in words (a plain opt-out).
_COLD_HARD_STOPS = {
    "data": re.compile(
        r"(borr|elimin|suprim|cancel)\w*\s+(todos\s+)?(mis|los|de mis)\s+datos|protecci[oó]n de datos|\brgpd\b|\bgdpr\b|\blopd\b|"
        r"derecho (de|al) (supresi[oó]n|olvido|acceso)|"
        r"(supprim|effac)\w*\s+(toutes\s+)?mes donn[ée]es|protection des donn[ée]es|droit (à l'oubli|d'acc[eè]s|à l'effacement)|"
        r"(delete|erase|remove)\s+(all\s+)?(of\s+)?my\s+(personal\s+)?(data|details|information)|data protection|right to be forgotten",
        re.I),
    "threat": re.compile(
        r"\bdenunci\w*|\baepd\b|\babogad[oa]\b|\bpolic[ií]a\b|\bjuzgado\b|"
        r"\bporter plainte\b|\bplainte\b|\bcnil\b|\bavocat\b|\bvous signaler\b|"
        r"\breport (you|this)\b|\blawyer\b|\bsue\b|\bpolice\b",
        re.I),
    "stop": re.compile(
        r"no me (escrib|mand|molest|contact|llam)\w*( m[aá]s)?|dej(a|ad|e) de (escribir|molestar|mandar)|no (quiero|deseo) (que me escrib|recibir)\w*|"
        r"ne m'(écrivez|ecrivez|envoyez|contactez) plus|arr[êe]tez de m'|ne me (contactez|dérangez|derangez) plus|"
        r"(don't|do not|stop) (message|messaging|text|texting|write|writing|contact|contacting|bother|bothering) (to )?me",
        re.I),
}


def cold_hard_stop(text: str | None) -> str | None:
    """'data' | 'threat' | 'stop' | None for a cold owner's reply (checked in this order: the strongest wins)."""
    t = (text or "").strip()
    for kind in ("data", "threat", "stop"):
        if _COLD_HARD_STOPS[kind].search(t):
            return kind
    return None

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
        if len(digits) == 9:  # national number (Spanish 9 digits; French 10 with the leading 0, removed above)
            digits = "+" + default_cc + digits
        else:
            digits = "+" + digits
    num = digits[1:]
    if not num.isdigit() or not (8 <= len(num) <= 15):
        return None
    if (num.startswith("34") or num.startswith("33")) and len(num) != 11:  # Spain and France: country code + 9 digits
        return None
    return "+" + num


def wa_id(phone_e164: str) -> str:
    """WhatsApp uses the number without '+'."""
    return phone_e164.lstrip("+")


# ---------- time ----------

DAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]  # Spanish (kept: used by the Spanish prompt)


_PARTICLES = {"de", "del", "la", "las", "los", "y", "van", "von", "da", "di"}


def tidy_name(name: str | None) -> str:
    """Google (or typing) can give 'NADIA ROUCHDI ROUCHDI' or 'nadia': show it like a person writes it."""
    n = " ".join((name or "").split())
    if not n or not (n.isupper() or n.islower()):
        return n
    return " ".join(w.lower() if i and w.lower() in _PARTICLES else w[:1].upper() + w[1:].lower() for i, w in enumerate(n.split()))


def first_name(name: str | None) -> str:
    parts = (name or "").split()
    return parts[0] if parts else ""


def parse_hm(s: str) -> time:
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
    return parse_hm(s["contact_start"]) <= local.time() < parse_hm(s["contact_end"])


def next_allowed_time(account: Account, after_utc: datetime) -> datetime:
    """First moment >= after_utc inside allowed hours (naive UTC). Capped to 09:00-21:00 whatever the settings."""
    s = settings_for(account)
    tz = ZoneInfo(account.timezone)
    start_t = max(parse_hm(s["contact_start"]), time(9, 0))
    end_t = min(parse_hm(s["contact_end"]), time(21, 0))
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
