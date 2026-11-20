"""Prospecting: private-seller ("particular") listings from CASAFARI become ordinary leads.

Opt-in twice: the global kill switch PROSPECTING_ENABLED and, per account, settings["prospecting"]["enabled"]. Spain only.
A run fetches at most a few pages (each costs CASAFARI tokens), keeps listings with a valid Spanish phone that match the
agent's filters, removes duplicates (same phone, same URL, same flat on several portals, already a lead, opted out
anywhere), stops at the daily cap, and hands each one to `engine.ingest_lead`. Nothing here messages anybody: the
normal first_contact job, `can_contact` and `send_to_lead` do that, with all their guards.

Cost: every pull (one CASAFARI page, up to 25 listings) costs 10 CASAFARI tokens = EUR 0.40 (casafari.TOKENS_PER_SEARCH,
EUR_PER_TOKEN). With PROSPECTING_MAX_PAGES_PER_RUN=1 and a pull every PROSPECTING_POLL_HOURS (default 3) inside the allowed
hours (09:00-21:00, about 4 pulls a day) one enabled account costs about EUR 1.60 a day at most. A run that finds the daily
cap already reached pulls nothing.

Safety: before every pull the account is checked. If the WhatsApp number quality is not healthy, or more than 15% of the
last 50 prospecting leads opted out, prospecting is switched off for that account ("paused_reason") and the operator (and
the agent) are alerted. A CASAFARI 402 (no tokens) pauses it too, operator alert only. Nothing resumes by itself: only the
operator ("Reanudar" in /admin/prospecting). The agent's own cold first message must have an APPROVED Meta template
("cold_template_status") before anything is pulled. New accounts ramp up: at most 3 leads a day for the first 3 days after
the agent confirmed, 5 a day until day 7, then their own cap.

Each lead keeps where it came from in `facts["_source"]` (keys starting with "_" never reach the AI), including the
date CASAFARI's terms make us delete it (`expires`). The daily `source_cleanup` job does that deletion.
"""
import hashlib
import logging
from collections import Counter
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select, update

from . import casafari, jobs
from . import lang as L
from .config import get_settings
from .db import utcnow
from .guards import in_allowed_hours, next_allowed_time, normalize_phone, tidy_name, to_local, to_utc_naive
from .models import Account, Event, Integration, Job, Lead, LlmTurn, Meeting, Message

log = logging.getLogger("sparrow.prospecting")

SOURCE = "casafari"
RAMP = ((3, 3), (7, 5))  # (days since confirmed_at below which, daily cap): 3 a day for 3 days, 5 a day until day 7
OPT_OUT_WINDOW = 50  # the last N prospecting leads
OPT_OUT_MAX_RATE = 0.15  # more than this share opted out -> pause
OPT_OUT_MIN_SAMPLE = 10  # fewer leads than this say nothing yet (one "no" out of two is not a trend)
QUALITY_OK = {"", "GREEN", "UNKNOWN"}  # Meta quality_rating of the agent's number; YELLOW, RED, FLAGGED... are not healthy
MAX_PAGES_HARD = 4  # the Free plan's page depth (100 results); never more, whatever the settings say
MAX_DAILY_CAP = 50
MAX_LOOKBACK_DAYS = 89  # the API accepts at most 90 days back

DEFAULTS = {
    "enabled": False,
    "location_id": None,
    "zone_name": "",
    "property_types": ["apartment", "house"],
    "min_price": None,
    "max_price": None,
    "daily_cap": 10,
    "operation": "sale",
    "since_days": 7,  # first run looks this far back; later runs start from the previous run's day
}

PROPERTY_TYPE_ES = {"apartment": "piso", "house": "casa", "studio": "estudio", "duplex": "dúplex", "penthouse": "ático"}
GENERIC_NAMES = {"particular", "anunciante", "propietario", "propietaria", "dueño", "dueña", "vendedor", "contacto", "privado", "n/a", "-"}


# ---------------------------------------------------------------- pure helpers (also used by scripts/casafari_yield.py)

def config_for(account: Account) -> dict:
    cfg = dict(DEFAULTS)
    cfg.update((account.settings or {}).get("prospecting") or {})
    return cfg


def spanish_phone(raw) -> str | None:
    """E.164 if it is a valid Spanish mobile (6, 7) or landline (8, 9); None for anything else, foreign numbers included."""
    p = normalize_phone(raw, "34")
    if not p or not p.startswith("+34") or len(p) != 12 or p[3] not in "6789":
        return None
    return p


def clean_name(raw) -> str:
    n = tidy_name(raw)
    return "" if n.strip().lower() in GENERIC_NAMES else n


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def cross_portal_key(price, area, location_id):
    """The same flat on two portals has the same price, area and place. Without price or area we can't tell: no key."""
    p, a = _num(price), _num(area)
    if not p or not a or not location_id:
        return None
    return (round(p), round(a), int(location_id))


def mask_phone(p: str | None) -> str:
    """+34600100001 -> +34 6** *** *01 (for logs and the yield script: never print a full number)."""
    if not p:
        return "-"
    return f"{p[:3]} {p[3]}** *** *{p[-2:]}" if len(p) >= 6 else "***"


def filter_listings(listings: list[dict], cfg: dict, since: date, *, seen: dict | None = None) -> tuple[list[dict], Counter]:
    """Returns (kept, skipped_by_reason). `kept` items are {"listing", "phone", "xkey"}. Filters first, then duplicates,
    so a copy without a phone never hides the copy that has one. `seen` (phones, urls, refs, xkeys) adds what we
    already have; it is updated in place."""
    seen = seen if seen is not None else {}
    for k in ("phones", "urls", "refs", "xkeys"):
        seen.setdefault(k, set())
    types = {t.lower() for t in (cfg.get("property_types") or [])}
    op = cfg.get("operation")
    lo, hi = _num(cfg.get("min_price")), _num(cfg.get("max_price"))
    kept, skipped = [], Counter()
    for x in listings:
        raw = (x.get("contact") or {}).get("phone")
        if not raw:
            skipped["no_phone"] += 1
            continue
        phone = spanish_phone(raw)
        if not phone:
            skipped["foreign_phone" if normalize_phone(raw, "34") else "invalid_phone"] += 1
            continue
        if op and x.get("operation") and x["operation"] != op:
            skipped["operation"] += 1
            continue
        if types and str(x.get("property_type") or "").lower() not in types:
            skipped["property_type"] += 1
            continue
        price = _num(x.get("price"))
        if (lo is not None or hi is not None) and price is not None and ((lo is not None and price < lo) or (hi is not None and price > hi)):
            skipped["price"] += 1
            continue
        if x.get("first_seen") and str(x["first_seen"])[:10] < since.isoformat():
            skipped["too_old"] += 1
            continue
        url, ref = x.get("url") or "", x.get("id") or ""
        xkey = cross_portal_key(x.get("price"), x.get("area"), x.get("location_id"))
        if phone in seen["phones"]:
            skipped["dup_phone"] += 1
            continue
        if (url and url in seen["urls"]) or (ref and ref in seen["refs"]):
            skipped["dup_url"] += 1
            continue
        if xkey and xkey in seen["xkeys"]:
            skipped["dup_cross_portal"] += 1
            continue
        seen["phones"].add(phone)
        if url:
            seen["urls"].add(url)
        if ref:
            seen["refs"].add(ref)
        if xkey:
            seen["xkeys"].add(xkey)
        kept.append({"listing": x, "phone": phone, "xkey": xkey})
    return kept, skipped


def expiry_date(today: date) -> date:
    """When a CASAFARI lead must be gone: the retention period, or 30 days after our CASAFARI account closed."""
    st = get_settings()
    exp = today + timedelta(days=st.prospecting_retention_days)
    if st.casafari_closed_on:
        exp = min(exp, date.fromisoformat(st.casafari_closed_on) + timedelta(days=30))
    return exp


def to_lead(item: dict, cfg: dict, today: date) -> dict:
    """The standard lead dict for engine.ingest_lead, plus the `_source` facts (only what the lead needs)."""
    x = item["listing"]
    ptype = str(x.get("property_type") or "")
    return {
        "name": clean_name((x.get("contact") or {}).get("name")),
        "phone": item["phone"],
        "lead_type": "seller",
        "source": SOURCE,
        "source_ref": str(x.get("id") or ""),
        "listing_url": str(x.get("url") or ""),
        "zone": cfg.get("zone_name") or "",
        "price": _num(x.get("price")),
        "property_type": PROPERTY_TYPE_ES.get(ptype, ptype),
        "_source": {
            "provider": SOURCE,
            "listing_id": str(x.get("id") or ""),
            "portal": str(x.get("portal") or ""),
            "area": _num(x.get("area")),
            "location_id": x.get("location_id"),
            "first_seen": str(x.get("first_seen") or "")[:10],
            "expires": expiry_date(today).isoformat(),
        },
    }


def suppress_hash(phone: str) -> str:
    return hashlib.sha256(f"sparrow-suppress:{phone}".encode()).hexdigest()


# ---------------------------------------------------------------- DB side

def _local_today(account: Account) -> date:
    return to_local(utcnow(), account.timezone).date()


def _local_midnight_utc(account: Account, d: date) -> datetime:
    return to_utc_naive(datetime.combine(d, datetime.min.time(), tzinfo=ZoneInfo(account.timezone)))


def created_today(s, account: Account) -> int:
    start = _local_midnight_utc(account, _local_today(account))
    return s.execute(select(func.count(Lead.id)).where(Lead.account_id == account.id, Lead.source == SOURCE, Lead.created_at >= start)).scalar() or 0


def _since(s, account: Account, cfg: dict, today: date) -> date:
    """From the day of the previous run (listings published later that day are not missed), else since_days back."""
    floor = today - timedelta(days=MAX_LOOKBACK_DAYS)
    last = s.execute(select(Event).where(Event.account_id == account.id, Event.name == "prospect_run")
                     .order_by(Event.at.desc(), Event.id.desc()).limit(1)).scalar_one_or_none()
    back = today - timedelta(days=max(0, int(cfg.get("since_days") or 7)))
    d = (last.data or {}) if last else {}
    if d.get("complete") and d.get("until"):
        start = date.fromisoformat(d["until"])
    elif d.get("since"):  # cap reached, page limit or error: listings were left behind, so look again (not too far back)
        start = max(date.fromisoformat(d["since"]), back)
    else:
        start = back
    return max(start, floor)


def _seen_for(s, account: Account) -> dict:
    """What this account already has from CASAFARI (URLs, listing ids, flats), so a re-run adds nothing twice."""
    seen = {"phones": set(), "urls": set(), "refs": set(), "xkeys": set()}
    rows = s.execute(select(Lead.listing_url, Lead.source_ref, Lead.price, Lead.facts).where(Lead.account_id == account.id, Lead.source == SOURCE))
    for url, ref, price, facts in rows:
        if url:
            seen["urls"].add(url)
        if ref:
            seen["refs"].add(ref)
        src = (facts or {}).get("_source") or {}
        k = cross_portal_key(price, src.get("area"), src.get("location_id"))
        if k:
            seen["xkeys"].add(k)
    return seen


def _blocked_phones(s, account: Account, phones: list[str]) -> tuple[set, set]:
    """(already a lead of this account, opted out anywhere or on the suppression list)."""
    if not phones:
        return set(), set()
    existing = set(s.execute(select(Lead.phone).where(Lead.account_id == account.id, Lead.phone.in_(phones))).scalars())
    dnc = set(s.execute(select(Lead.phone).where(Lead.phone.in_(phones), Lead.do_not_contact.is_(True))).scalars())
    hashes = {(d or {}).get("sha256") for d in s.execute(select(Event.data).where(Event.name == "phone_suppressed")).scalars()}
    dnc |= {p for p in phones if suppress_hash(p) in hashes}
    return existing, dnc


def why_not(account: Account, cfg: dict) -> str | None:
    if not get_settings().prospecting_enabled:
        return "kill_switch"
    if not cfg.get("enabled"):
        return "disabled"
    if not cfg.get("location_id"):
        return "no_location"
    if account.status != "live":
        return "account_not_live"
    if L.lead_lang(account) != "es":
        return "not_spain"
    if not cfg.get("cold_body"):
        return "no_message"  # the agent writes the first message; nothing is pulled until there is one
    if cfg.get("cold_template_status") != "approved":
        return "template_pending"  # the agent's own first message waits for Meta's approval
    return None


def effective_cap(cfg: dict, now: datetime | None = None) -> int:
    """The agent's daily cap, held down while the number is new to this: 3 a day for the first 3 days after the agent
    confirmed (cfg["confirmed_at"]), 5 a day until day 7. Without confirmed_at (set by hand) there is no ramp."""
    cap = max(0, min(int(cfg.get("daily_cap") or 0), MAX_DAILY_CAP))
    raw = cfg.get("confirmed_at")
    if raw:
        try:
            days = ((now or utcnow()) - datetime.fromisoformat(str(raw)).replace(tzinfo=None)).days
        except ValueError:
            days = 0
        for limit_days, ramp_cap in RAMP:
            if days < limit_days:
                return min(cap, ramp_cap)
    return cap


# ---------------------------------------------------------------- auto-pause (never resumes by itself)

PAUSE_TEXT_AGENT = {  # what the agent reads after "necesita que revises algo:" (agent alerts use the agent's language)
    "quality": {
        "es": "tu prospección automática está en pausa porque WhatsApp marca la calidad de tu número como baja. Revisa tu número y escríbenos",
        "fr": "votre prospection automatique est en pause car WhatsApp juge la qualité de votre numéro trop basse. Vérifiez votre numéro et écrivez-nous",
        "en": "your automatic prospecting is paused because WhatsApp rates your number's quality as low. Check your number and write to us",
    },
    "opt_outs": {
        "es": "tu prospección automática está en pausa porque muchos propietarios han pedido no recibir más mensajes. Revisa el texto del primer mensaje y escríbenos",
        "fr": "votre prospection automatique est en pause car beaucoup de propriétaires ont demandé à ne plus recevoir de messages. Revoyez le premier message et écrivez-nous",
        "en": "your automatic prospecting is paused because many owners asked not to receive more messages. Review your first message and write to us",
    },
}
PAUSE_TEXT_OPS = {
    "quality": "Prospecting pausado: la calidad del número de WhatsApp no es buena",
    "opt_outs": "Prospecting pausado: más del 15% de los últimos propietarios pidió la baja",
    "source_credit": "Prospecting pausado: CASAFARI sin tokens (402). Recarga y reanuda a mano",
}


def wa_quality(s, account: Account) -> str:
    """The Meta quality rating of the agent's number as stored on the meta integration (`external["quality_rating"]`:
    GREEN / YELLOW / RED / FLAGGED ...). Read only: this module never calls Meta. Empty = unknown = healthy."""
    integ = s.execute(select(Integration).where(Integration.account_id == account.id, Integration.provider == "meta")).scalar_one_or_none()
    return str(((integ.external or {}) if integ else {}).get("quality_rating") or "").strip().upper()


def opt_out_rate(s, account: Account) -> tuple[int, int]:
    """(opted out, total) among the last OPT_OUT_WINDOW prospecting leads of the account (test leads excluded)."""
    rows = s.execute(select(Lead.do_not_contact).where(Lead.account_id == account.id, Lead.source == SOURCE, Lead.is_test.is_(False))
                     .order_by(Lead.id.desc()).limit(OPT_OUT_WINDOW)).scalars().all()
    return sum(1 for r in rows if r), len(rows)


def health_problem(s, account: Account) -> str | None:
    if wa_quality(s, account) not in QUALITY_OK:
        return "quality"
    out, total = opt_out_rate(s, account)
    if total >= OPT_OUT_MIN_SAMPLE and out / total > OPT_OUT_MAX_RATE:
        return "opt_outs"
    return None


def pause(s, account: Account, reason: str, *, tell_agent: bool) -> None:
    """Switch prospecting off for this account and say why ("quality" | "opt_outs" | "source_credit" | "operator").
    The operator gets an alert; the agent too when it is their number or text that caused it. Never resumes by itself."""
    from . import engine
    from . import templates as T

    cfg = dict((account.settings or {}).get("prospecting") or {})
    cfg.update({"enabled": False, "paused_reason": reason, "paused_at": utcnow().isoformat()})
    account.settings = {**(account.settings or {}), "prospecting": cfg}
    engine.log_event(s, "prospect_paused", account.id, reason=reason)
    log.warning("prospecting for account %s paused: %s", account.id, reason)
    if reason in PAUSE_TEXT_OPS:
        engine.alert_ops(s, account, T.OPS_NEEDS_HELP, [account.agent_name or "Un agente", "prospecting", account.personal_whatsapp or account.email or "-", PAUSE_TEXT_OPS[reason]])
    if tell_agent and reason in PAUSE_TEXT_AGENT:
        lang = L.lead_lang(account)
        engine.alert_agent(s, account, T.AGENT_ACTION_REQUIRED, [L.agent_hi(account), PAUSE_TEXT_AGENT[reason][lang]])


def run_for_account(s, account: Account, *, client=None) -> dict:
    """One prospecting run. Returns the counts it also logs as a `prospect_run` event (no phones or names in it)."""
    from . import engine  # engine imports a lot; keep this module importable on its own

    cfg = config_for(account)
    off = why_not(account, cfg)
    if off:
        return {"ran": False, "reason": off}
    problem = health_problem(s, account)  # before spending a token
    if problem:
        pause(s, account, problem, tell_agent=True)
        return {"ran": False, "reason": "paused", "paused_reason": problem}
    st = get_settings()
    today = _local_today(account)
    since = _since(s, account, cfg, today)
    cap = effective_cap(cfg)
    room = cap - created_today(s, account)
    counts = {"ran": True, "fetched": 0, "created": 0, "searches": 0, "skipped": Counter(), "since": since.isoformat(), "until": today.isoformat()}
    if room <= 0:
        counts["skipped"]["daily_cap_reached_before_fetch"] += 1
        return _finish(s, account, counts)

    cl = client or casafari.client()
    max_pages = max(0, min(int(st.prospecting_max_pages_per_run), MAX_PAGES_HARD))
    seen = _seen_for(s, account)
    cursor = None
    for _ in range(max_pages):
        try:
            page = cl.search(cfg["location_id"], since, until=today, operation=cfg.get("operation") or None, cursor=cursor)
        except casafari.CasafariError as e:  # never retried by the queue: a retry would spend tokens again
            counts["error"] = type(e).__name__
            counts["error_status"] = e.status
            log.warning("prospecting for account %s stopped: %s", account.id, e)
            if e.status == 402:  # out of CASAFARI tokens: stop until the operator tops up and resumes
                pause(s, account, "source_credit", tell_agent=False)
            break
        counts["searches"] += 1
        listings = page.get("listings") or []
        counts["fetched"] += len(listings)
        kept, skipped = filter_listings(listings, cfg, since, seen=seen)
        counts["skipped"].update(skipped)
        existing, dnc = _blocked_phones(s, account, [k["phone"] for k in kept])
        for item in kept:
            if item["phone"] in dnc:
                counts["skipped"]["do_not_contact"] += 1
                continue
            if item["phone"] in existing:
                counts["skipped"]["already_lead"] += 1
                continue
            if room <= 0:
                counts["skipped"]["daily_cap"] += 1
                continue
            data = to_lead(item, cfg, today)
            src = data.pop("_source")
            lead, res = engine.ingest_lead(s, account, data)
            if lead is None or res == "duplicate":
                counts["skipped"]["already_lead"] += 1
                continue
            lead.facts = {**(lead.facts or {}), "_source": src}
            counts["created"] += 1
            room -= 1
        cursor = page.get("next")
        if not cursor or room <= 0:
            break
    # complete = we saw everything published in the window, so the next run can start from today
    counts["complete"] = "error" not in counts and not cursor and not counts["skipped"].get("daily_cap")
    return _finish(s, account, counts)


def _finish(s, account: Account, counts: dict) -> dict:
    counts["skipped"] = dict(counts["skipped"])
    counts["tokens"] = counts.get("searches", 0) * casafari.TOKENS_PER_SEARCH
    s.add(Event(name="prospect_run", account_id=account.id, data=counts))
    log.info("prospecting account %s: %s", account.id, counts)
    return counts


# ---------------------------------------------------------------- jobs

def enabled_accounts(s) -> list[Account]:
    return [a for a in s.execute(select(Account).where(Account.status == "live")).scalars() if why_not(a, config_for(a)) is None]


def poll_interval() -> timedelta:
    return timedelta(hours=max(1, int(get_settings().prospecting_poll_hours or 3)))


def schedule_prospecting(s) -> int:
    """Queue a `prospect` job for each enabled account that is due: none pending, and the last run at least one poll
    interval (PROSPECTING_POLL_HOURS) ago. The job runs at the account's next allowed hour. Returns how many were queued."""
    if not get_settings().prospecting_enabled:
        return 0
    n, now, poll = 0, utcnow(), poll_interval()
    for a in enabled_accounts(s):
        if s.execute(select(Job.id).where(Job.type == "prospect", Job.account_id == a.id, Job.status.in_(("pending", "running")))).first():
            continue
        last = s.execute(select(Event.at).where(Event.account_id == a.id, Event.name == "prospect_run").order_by(Event.at.desc(), Event.id.desc()).limit(1)).scalar_one_or_none()
        if last and now - last < poll - timedelta(minutes=5):  # a little slack: the scan itself drifts by minutes
            continue
        run_at = next_allowed_time(a, now)
        key = f"prospect:{a.id}:{run_at:%Y-%m-%dT%H}"
        if s.execute(select(Job.id).where(Job.dedupe_key == key)).first() is None:
            jobs.schedule(s, "prospect", run_at, account_id=a.id, dedupe_key=key)
            n += 1
    return n


@jobs.handler("prospect_scan")
def job_prospect_scan(s, job):
    nxt = utcnow() + poll_interval()
    jobs.schedule(s, "prospect_scan", nxt, dedupe_key=f"prospect_scan:{nxt:%Y-%m-%dT%H}")
    if get_settings().prospecting_enabled:
        from . import quality  # fresh Meta quality rating of each agent's number, so the auto-pause sees it
        quality.refresh_prospecting_accounts(s, enabled_accounts(s))
    schedule_prospecting(s)


@jobs.handler("prospect")
def job_prospect(s, job):
    account = s.get(Account, job.account_id)
    if account is None:
        return
    if not in_allowed_hours(account):  # fetch only when the first message can follow right away
        job.status = "pending"
        job.run_at = next_allowed_time(account, utcnow())
        return
    run_for_account(s, account)


# ---------------------------------------------------------------- cleanup (CASAFARI storage limit)

def _expires(lead: Lead) -> date:
    src = (lead.facts or {}).get("_source") or {}
    try:
        exp = date.fromisoformat(src["expires"])
    except (KeyError, TypeError, ValueError):
        exp = lead.created_at.date() + timedelta(days=get_settings().prospecting_retention_days)
    closed = get_settings().casafari_closed_on
    if closed:
        exp = min(exp, date.fromisoformat(closed) + timedelta(days=30))
    return exp


def cleanup_expired(s) -> dict:
    """CASAFARI leads past their date: deleted if they never answered (an opted-out number stays only as a hash, so
    nobody contacts it again). If the owner answered, the conversation is now the agent's own: we keep the lead but
    drop what came from CASAFARI (listing URL, listing id, source facts)."""
    today = utcnow().date()
    out = {"deleted": 0, "stripped": 0}
    for lead in s.execute(select(Lead).where(Lead.source == SOURCE)).scalars().all():
        if _expires(lead) >= today:
            continue
        booked = s.execute(select(Meeting.id).where(Meeting.lead_id == lead.id)).first() is not None
        if lead.has_replied or booked:
            if lead.listing_url or lead.source_ref or "_source" in (lead.facts or {}):
                lead.listing_url = ""
                lead.source_ref = ""
                lead.facts = {k: v for k, v in (lead.facts or {}).items() if k != "_source"}
                out["stripped"] += 1
            continue
        if lead.do_not_contact:
            s.add(Event(name="phone_suppressed", data={"sha256": suppress_hash(lead.phone)}))
        for model in (Message, Job, LlmTurn, Meeting):
            s.execute(delete(model).where(model.lead_id == lead.id))
        s.execute(update(Event).where(Event.lead_id == lead.id).values(lead_id=None))
        s.delete(lead)
        out["deleted"] += 1
    if out["deleted"] or out["stripped"]:
        s.add(Event(name="source_cleanup", data=out))
    return out


@jobs.handler("source_cleanup")
def job_source_cleanup(s, job):
    nxt = utcnow() + timedelta(days=1)
    jobs.schedule(s, "source_cleanup", nxt, dedupe_key=f"source_cleanup:{nxt:%Y-%m-%d}")
    cleanup_expired(s)
