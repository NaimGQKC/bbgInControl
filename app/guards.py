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


_WARM_THREAT = re.compile(
    r"\b(os|te|le|les|nos)\s+(voy\s+a\s+)?denunci\w*|\bdenunciar(os|te|nos|les)\b|\bdenunciaré\b|\baepd\b|\bcnil\b|"
    r"\bvous\s+(signaler|dénoncer)\b|\bje\s+(vais\s+)?porter\s+plainte\b|\breport\s+(you|us)\b|\bsue\s+(you|us)\b", re.I)


def warm_hard_stop(text: str | None) -> str | None:
    """'data' | 'threat' | None for an owner who is already in a normal chat. Narrower than cold_hard_stop: words like
    'abogada' or 'policía' come up in ordinary sales (inheritance, divorce), so only a data request or a clear threat to report counts."""
    t = (text or "").strip()
    if _COLD_HARD_STOPS["data"].search(t):
        return "data"
    return "threat" if _WARM_THREAT.search(t) else None


def cold_hard_stop(text: str | None) -> str | None:
    """'data' | 'threat' | 'stop' | None for a cold owner's reply (checked in this order: the strongest wins)."""
    t = (text or "").strip()
    for kind in ("data", "threat", "stop"):
        if _COLD_HARD_STOPS[kind].search(t):
            return kind
    return None

DEFAULT_SETTINGS = {
    # Allowed hours for first contact / follow-ups (local time). Days: 0=Monday ... 6=Sunday
    "contact_days": [0, 1, 2, 3, 4, 5, 6],
    "contact_start": "08:00",
    "contact_end": "22:00",
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


_OPT_PHRASES = re.compile(
    r"\b(no me (escrib|contact|llam|moleste)\w*( mas)?|dejad? de (escribir|contactar|molestar)\w*|dejen de (escribir|contactar)\w*|"
    r"ne m'?ecri\w* plus|ne me contact\w* plus|arretez?\b|stop (writing|messaging|contacting)|do not (contact|message) me|don'?t (contact|message|text) me)", re.I)
_OPT_BLOCK = {"precio", "precios", "prix", "price", "prices"}  # "baja el precio" is about the price, not an opt-out


# A message about WHEN to contact them ("no me llames por la mañana, prefiero por la tarde") is a scheduling preference, not an opt-out.
# Only clock words count: a preference about the channel or a plain "no me escribas" stays an opt-out (WhatsApp is our only channel).
_OPT_PREFERENCE = re.compile(
    r"\b(por la (manana|tarde|noche)|en horario|horario|antes de|despues de|hasta las|a partir de|fines? de semana|mientras (trabajo|estoy)|durante)\b")
_OPT_STRONG = re.compile(r"\b(mas|nunca|jamas|dejad?|dejen|stop|baja|arretez?|plus)\b")
_OPT_GIVE_BAJA = re.compile(r"\b(dame|dadme|dadnos|danos|deme|me (das|dais|deis)|nos (das|dais))\b.*\bbaja\b")  # "dadme de baja": theirs
_OPT_LISTING_BAJA = re.compile(r"\b(di|dado|dio|dimos|quite|retire|retirado|retirada)\b.*\bde baja\b|\bde baja\b.*\b(anuncio|portal|piso|casa|vivienda)\b")


def _plain(text: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", (text or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9' ]+", " ", s)


def is_opt_out_text(text: str) -> bool:
    """A short message that is an opt-out however it is written: 'BAJA', '¡BAJA!', 'baja por favor', 'STOP 🙏', 'Arrêtez',
    'no me escribas más'. Long messages are left to the model (an owner can say 'baja' while talking about the price)."""
    plain = _plain(text)
    words = plain.split()
    if not words or len(words) > 60:
        return False
    plain_opt = {_plain(w).strip() for w in OPT_OUT_WORDS}
    if _OPT_GIVE_BAJA.search(plain):
        return True
    # "lo di de baja" is about their listing, unless there is also a bare BAJA/STOP or an explicit "no me escribas": then it is an opt-out
    bare_baja = re.search(r"(?<!de )\bbaja\b", plain)
    if _OPT_LISTING_BAJA.search(plain) and not bare_baja and not ((plain_opt - {"baja"}) & set(words)) and not _OPT_PHRASES.search(plain):
        return False
    if _OPT_PHRASES.search(plain):
        # a preference about WHEN to contact them is not an opt-out unless they also say "no more" / stop in some form
        return not (_OPT_PREFERENCE.search(plain) and not _OPT_STRONG.search(plain))
    if _OPT_BLOCK & set(words):
        return False
    if {"stop", "arret", "arretez", "unsubscribe"} & set(words) and len(words) <= 10:
        return True  # unambiguous words: "Ya lo di de baja, STOP"
    if words[-1] == "baja" and not plain.endswith("de baja") and len(words) <= 8:
        return True  # a closing bare BAJA: "Ya lo quité del portal. BAJA"
    return len(words) <= 5 and bool(plain_opt & set(words))


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


# Hard limits by kind of contact. The agent's settings can narrow the window, never widen it.
WARM_LIMITS = (time(8, 0), time(22, 0), {0, 1, 2, 3, 4, 5, 6})  # the owner asked us (form, portal request): speed is the product
COLD_LIMITS = (time(9, 0), time(21, 0), {0, 1, 2, 3, 4, 5})  # we wrote first (prospecting): Mon-Sat 09-21 only


def _window(account: Account, cold: bool) -> tuple[time, time, set]:
    s = settings_for(account)
    lo, hi, days = COLD_LIMITS if cold else WARM_LIMITS
    return max(parse_hm(s["contact_start"]), lo), min(parse_hm(s["contact_end"]), hi), set(s["contact_days"]) & days


def in_allowed_hours(account: Account, when_utc: datetime | None = None, *, cold: bool = False) -> bool:
    start_t, end_t, days = _window(account, cold)
    local = to_local(when_utc or utcnow(), account.timezone)
    return local.weekday() in days and start_t <= local.time() < end_t


def next_allowed_time(account: Account, after_utc: datetime, *, cold: bool = False) -> datetime:
    """First moment >= after_utc inside the allowed hours (naive UTC)."""
    start_t, end_t, days = _window(account, cold)
    tz = ZoneInfo(account.timezone)
    local = to_local(after_utc, account.timezone)
    for _ in range(15):
        day_start = datetime.combine(local.date(), start_t, tzinfo=tz)
        day_end = datetime.combine(local.date(), end_t, tzinfo=tz)
        if local.weekday() in days:
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
    if kind in ("first", "followup") and not in_allowed_hours(account, cold=is_cold(lead)) and not (lead.is_test and (lead.facts or {}).get("_any_hour")):
        return False, "outside_hours"  # G9 (the one exception: a TEST lead the operator created with "any hour")
    return True, "ok"
