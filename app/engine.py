"""The conversation engine: intake → first message → AI chat → booking → reminders, plus handover and alerts."""
import logging
import re
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from . import coldmsg, gcal, jobs, templates as T
from .config import get_settings
from .crypto import decrypt, encrypt
from .db import utcnow
from .gcal import CalendarError, calendar_for_account
from .guards import (
    DAYS,
    HOT_STAGES,
    NO_AI_TAGS,
    OPT_OUT_WORDS,
    is_opt_out_text,
    can_contact,
    cold_hard_stop,
    first_name,
    is_cold,
    next_allowed_time,
    normalize_phone,
    tidy_name,
    parse_hm,
    settings_for,
    to_local,
    to_utc_naive,
    window_open,
)
from .llm import run_tool_loop
from .models import Account, Event, Integration, Job, Lead, LlmTurn, Meeting, Message
from .prompt import build_system_prompt
from .transcribe import transcribe
from . import lang as L
from .whatsapp import FakeWhatsApp, WhatsAppClient, WhatsAppError

log = logging.getLogger("sparrow.engine")
_booking_locks: dict[int, threading.Lock] = {}


# ---------------------------------------------------------------- helpers

def when_label(dt_utc: datetime, tz: str, lang: str = "es") -> str:
    return L.when_label(to_local(dt_utc, tz), lang)


def log_event(s, name: str, account_id=None, lead_id=None, **data):
    s.add(Event(name=name, account_id=account_id, lead_id=lead_id, data=data))


def agent_wa(s, account: Account) -> WhatsAppClient:
    """Client that sends from the agent's own number (Coexistence) or, in dev, the test number."""
    st = get_settings()
    integ = s.execute(select(Integration).where(Integration.account_id == account.id, Integration.provider == "meta")).scalar_one_or_none()
    if st.whatsapp_mode == "fake":
        pid = (integ.external or {}).get("phone_number_id") if integ else None
        return FakeWhatsApp(phone_number_id=pid or f"agent-{account.id}")
    if integ and integ.status == "active" and integ.token_enc:
        ext = integ.external or {}
        return WhatsAppClient(decrypt(integ.token_enc), ext["phone_number_id"], ext.get("waba_id", ""))
    # Dev/pilot: only accounts explicitly linked to the Meta test number use the token from the environment
    if integ and (integ.external or {}).get("test_number") and st.wa_token and st.wa_phone_number_id:
        return WhatsAppClient(st.wa_token, st.wa_phone_number_id, st.wa_waba_id)
    raise WhatsAppError("WhatsApp not connected for this account")


def sparrow_wa() -> WhatsAppClient | None:
    st = get_settings()
    if st.whatsapp_mode == "fake":
        return FakeWhatsApp(phone_number_id="sparrow")
    if st.sparrow_phone_number_id and st.sparrow_wa_token:
        return WhatsAppClient(st.sparrow_wa_token, st.sparrow_phone_number_id)
    return None


def _store_out(s, account, lead, *, sender, type_, text, template_name="", wa_id=None, status="sent", error=""):
    s.add(
        Message(
            account_id=account.id,
            lead_id=lead.id if lead else None,
            direction="out",
            sender=sender,
            type=type_,
            text=text,
            template_name=template_name,
            wa_message_id=wa_id,
            status=status,
            error=error,
        )
    )
    s.flush()


def send_to_lead(s, account: Account, lead: Lead, *, kind: str, text: str | None = None,
                 template: T.Template | None = None, values: list[str] | None = None, sender: str = "ai") -> bool:
    """The only function that sends to a lead. Runs the hard checks right before sending."""
    s.flush()
    s.refresh(lead)  # last-second check (G8): an echo may have arrived meanwhile
    ok, why = can_contact(account, lead, kind=kind)
    if not ok:
        log.info("blocked %s to lead %s: %s", kind, lead.id, why)
        return False
    if text is not None and not window_open(lead):
        log.info("window closed for lead %s, free text not allowed (G7)", lead.id)  # G7
        return False
    wa = agent_wa(s, account)
    lang = L.lead_lang(account)
    try:
        if template is not None:
            wid = wa.send_template(lead.phone, template, values or [], language=lang)
            _store_out(s, account, lead, sender=sender, type_="template", text=template.render(values or [], lang), template_name=template.name, wa_id=wid)
        else:
            wid = wa.send_text(lead.phone, text)
            _store_out(s, account, lead, sender=sender, type_="text", text=text, wa_id=wid)
        s.commit()  # the message is out: never lose its record (and never resend it on a retry)
    except WhatsAppError as e:
        _store_out(s, account, lead, sender=sender, type_="template" if template else "text",
                   text=template.render(values or [], lang) if template else text or "", template_name=template.name if template else "",
                   status="failed", error=str(e))
        log_event(s, "send_failed", account.id, lead.id, error=str(e), code=e.code)
        s.commit()
        raise
    return True


def _sparrow_template(s, account: Account, to: str | None, template: T.Template, values: list[str]) -> None:
    lang = "es" if template is T.OPS_NEEDS_HELP else L.lead_lang(account)  # agents get their alerts in their language; you (the operator) in Spanish
    wa = sparrow_wa()
    if not wa or not to:
        log.warning("cannot send %s for account %s (no Sparrow number or no recipient): %s", template.name, account.id, template.render(values, lang))
        log_event(s, "alert_not_sent", account.id, template=template.name, text=template.render(values, lang))
        return
    try:
        wid = wa.send_template(to, template, values, language=lang)
        s.add(Message(account_id=account.id, direction="out", sender="system", type="template",
                      text=template.render(values, lang), template_name=template.name, wa_message_id=wid, status="sent"))
    except WhatsAppError as e:
        log.error("%s failed: %s", template.name, e)
        log_event(s, "alert_failed", account.id, template=template.name, error=str(e))


def alert_agent(s, account: Account, template: T.Template, values: list[str]) -> None:
    """Messages to the agent come only from the Sparrow number (design J7)."""
    _sparrow_template(s, account, normalize_phone(account.personal_whatsapp), template, values)


def alert_ops(s, account: Account, template: T.Template, values: list[str]) -> None:
    """Same, but to the operator (OPS_WHATSAPP): an agent asked for help."""
    _sparrow_template(s, account, normalize_phone(get_settings().ops_whatsapp), template, values)


def _clip(text: str, n: int = 120) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def lead_summary(lead: Lead) -> str:
    parts = [first_name(lead.name) or lead.phone]
    prop = " ".join(p for p in [lead.property_type, lead.zone] if p)
    if prop:
        parts.append(prop)
    f = lead.facts or {}
    for k in ("reason", "timeline"):
        if f.get(k):
            parts.append(f[k])
    parts.append(lead.phone)
    return _clip(" · ".join(parts), 200)


# ---------------------------------------------------------------- intake

LEAD_FIELDS = ("name", "property_type", "zone", "address", "listing_url", "stage", "source", "source_ref")


def ingest_lead(s, account: Account, data: dict, *, is_test: bool = False) -> tuple[Lead | None, str]:
    """Standard `Lead` in, first message scheduled. Returns (lead, result)."""
    phone = normalize_phone(data.get("phone"), L.cc_for(account))
    if not phone:
        log_event(s, "lead_rejected", account.id, reason="invalid_phone", raw=str(data.get("phone")))
        return None, "invalid_phone"
    lead = s.execute(select(Lead).where(Lead.account_id == account.id, Lead.phone == phone)).scalar_one_or_none()
    if lead:  # G4: duplicates merge, never a second first message
        for f in LEAD_FIELDS:
            if data.get(f) and not getattr(lead, f):
                setattr(lead, f, str(data[f]))
        if data.get("stage"):
            lead.stage = str(data["stage"])
        if data.get("tags"):
            lead.tags = sorted(set((lead.tags or []) + list(data["tags"])))
        return lead, "duplicate"
    lead_type = (data.get("lead_type") or "seller").lower()
    lead = Lead(
        account_id=account.id,
        phone=phone,
        name=str(data.get("name") or ""),
        source=str(data.get("source") or "manual"),
        source_ref=str(data.get("source_ref") or ""),
        lead_type=lead_type if lead_type in ("seller", "buyer", "unknown") else "unknown",
        property_type=str(data.get("property_type") or ""),
        zone=str(data.get("zone") or ""),
        address=str(data.get("address") or ""),
        price=float(data["price"]) if data.get("price") not in (None, "") else None,
        listing_url=str(data.get("listing_url") or ""),
        stage=str(data.get("stage") or ""),
        tags=list(data.get("tags") or []),
        is_test=is_test,
    )
    s.add(lead)
    s.flush()
    log_event(s, "lead_received", account.id, lead.id, source=lead.source)

    skip = None
    if lead.lead_type == "buyer":
        skip = "buyer"
    elif lead.stage.strip().lower() in HOT_STAGES:
        skip = "hot_stage"
    elif any(t.strip().lower() in NO_AI_TAGS for t in lead.tags):
        skip = "no_ia_tag"
    if skip:
        lead.status = "skipped"
        log_event(s, "lead_skipped", account.id, lead.id, reason=skip)
        return lead, f"skipped:{skip}"

    run_at = next_allowed_time(account, utcnow() + timedelta(seconds=get_settings().first_message_delay))
    jobs.schedule(s, "first_contact", run_at, lead_id=lead.id, account_id=account.id, dedupe_key=f"first:{lead.id}")
    lead.status = "queued"
    return lead, "queued"


# ---------------------------------------------------------------- scheduled jobs

def _load(s, job) -> tuple[Account, Lead]:
    lead = s.get(Lead, job.lead_id)
    return s.get(Account, lead.account_id), lead


def _at_local(account: Account, base_utc: datetime, days: int, hhmm: str = "10:00") -> datetime:
    tz = ZoneInfo(account.timezone)
    d = to_local(base_utc, account.timezone).date() + timedelta(days=days)
    return next_allowed_time(account, to_utc_naive(datetime.combine(d, parse_hm(hhmm), tzinfo=tz)))


def first_contact_values(account: Account, lead_name: str, zone: str) -> list[str]:
    lang = L.lead_lang(account)
    return [
        first_name(tidy_name(lead_name)) or L.text("greeting_fallback", lang),
        first_name(tidy_name(account.agent_name)) or L.text("your_agent", lang),
        account.agency or L.text("your_agency", lang),
        zone or (account.zones.split(",")[0].strip() if account.zones else L.text("your_zone", lang)),
    ]


# ---------------------------------------------------------------- cold first contact (prospecting, CASAFARI)
# The agent writes the first message (settings["prospecting"]["cold_body"]); Sparrow adds the footer (coldmsg) and
# submits it to Meta as the agent's own template (onboarding.submit_cold_template). What we send is the snapshot that
# was submitted ("cold_submitted"), never the draft being edited, and only once Meta approved it.

def prospecting_cfg(account: Account) -> dict:
    return dict((account.settings or {}).get("prospecting") or {})


def set_prospecting(account: Account, **kw) -> None:
    """Update settings["prospecting"] (reassigns the JSON so the change is saved)."""
    account.settings = {**(account.settings or {}), "prospecting": {**prospecting_cfg(account), **kw}}


def cold_privacy_url() -> str:
    return get_settings().base_url.rstrip("/") + "/privacidad"


def cold_template_for(account: Account) -> T.Template | None:
    """The cold template as it was submitted to Meta (None if nothing was submitted)."""
    sub = prospecting_cfg(account).get("cold_submitted") or {}
    if not sub.get("body") or not sub.get("version"):
        return None
    return T.cold_first_contact(sub["version"], sub.get("lang") or "es", sub["body"], sub.get("privacy_url") or cold_privacy_url())


def cold_values(account: Account, lead: Lead, body: str) -> list[str]:
    """Values in coldmsg.used_fields(body) order. Every variable needs a value: a missing name is the neutral greeting."""
    lang = L.lead_lang(account)
    vals = {
        "nombre": first_name(tidy_name(lead.name)) or L.text("greeting_fallback", lang),
        "tipo": L.property_word(lead.property_type, lang),
        "zona": lead.zone or (account.zones.split(",")[0].strip() if account.zones else L.text("your_zone", lang)),
        "agente": first_name(tidy_name(account.agent_name)) or L.text("your_agent", lang),
        "agencia": account.agency or L.text("your_agency", lang),
    }
    return [vals[f] for f in coldmsg.used_fields(body)]


def cold_first_message(account: Account, lead: Lead) -> tuple[T.Template | None, list[str] | str]:
    """(template, values) when the agent's cold template is approved in the current lead language,
    else (None, why): the lead then stays queued until release_cold_queue re-arms it."""
    cfg = prospecting_cfg(account)
    sub = cfg.get("cold_submitted") or {}
    if cfg.get("cold_template_status") != "approved":
        return None, f"cold_template_{cfg.get('cold_template_status') or 'missing'}"
    tpl = cold_template_for(account)
    if tpl is None or sub.get("lang") != L.lead_lang(account) or int(sub.get("version") or 0) != int(cfg.get("cold_version") or 0):
        return None, "cold_template_missing"
    return tpl, cold_values(account, lead, sub["body"])


def preview_cold_message(account: Account, body: str | None = None) -> str:
    """What an owner would read (agent text + footer), with the sample owner/listing from coldmsg.SAMPLE and the
    agent's own name and agency when we have them. body: preview a draft before saving it."""
    lang = L.lead_lang(account)
    text = body if body is not None else (prospecting_cfg(account).get("cold_body") or coldmsg.default_body(lang))
    vals = dict(coldmsg.SAMPLE.get(lang, coldmsg.SAMPLE["es"]))
    if first_name(tidy_name(account.agent_name)):
        vals["agente"] = first_name(tidy_name(account.agent_name))
    if account.agency:
        vals["agencia"] = account.agency
    return coldmsg.render(text, lang, cold_privacy_url(), vals)


def release_cold_queue(s, account: Account) -> int:
    """The cold template was just approved: re-arm the first message of cold leads that were waiting for it
    (spaced 2 minutes apart, inside allowed hours; can_contact still decides at send time)."""
    waiting = [l for l in s.execute(select(Lead).where(Lead.account_id == account.id, Lead.status == "queued")).scalars() if is_cold(l)]
    start = next_allowed_time(account, utcnow())
    for i, lead in enumerate(waiting):
        jobs.schedule(s, "first_contact", start + timedelta(minutes=2 * i), lead_id=lead.id, account_id=account.id,
                      dedupe_key=f"first:{lead.id}", reuse=True)
    if waiting:
        log_event(s, "cold_queue_released", account.id, n=len(waiting))
    return len(waiting)


def rearm_queued_leads(s, account: Account) -> int:
    """When an account goes live (or is switched back on), leads that arrived earlier and were left queued get their first
    message now. Cold (prospecting) leads have their own release once their template is approved."""
    n = 0
    for lead in s.execute(select(Lead).where(Lead.account_id == account.id, Lead.status == "queued", Lead.do_not_contact.is_(False))).scalars():
        if is_cold(lead):
            continue
        jobs.schedule(s, "first_contact", utcnow() + timedelta(seconds=get_settings().first_message_delay), lead_id=lead.id,
                      account_id=account.id, dedupe_key=f"first:{lead.id}:live")
        n += 1
    return n


@jobs.handler("first_contact")
def job_first_contact(s, job):
    account, lead = _load(s, job)
    if lead.status not in ("queued", "new"):
        return
    ok, why = can_contact(account, lead, kind="first")
    if why == "outside_hours":
        job.status = "pending"
        job.run_at = next_allowed_time(account, utcnow())
        return
    if not ok:
        if why != "account_not_live":  # a not-yet-live account keeps the lead queued; any other block is final
            lead.status = "skipped"
        log_event(s, "first_contact_blocked", account.id, lead.id, reason=why)
        return
    if is_cold(lead):
        _cold_first_contact(s, account, lead)
        return
    if send_to_lead(s, account, lead, kind="first", template=T.LEAD_FIRST_CONTACT, values=first_contact_values(account, lead.name, lead.zone)):
        lead.status = "contacted"
        lead.introduced = True
        latency = (utcnow() - lead.created_at).total_seconds()
        log_event(s, "first_message_sent", account.id, lead.id, seconds_after_lead=round(latency))
        jobs.schedule(s, "followup", _at_local(account, utcnow(), 1), lead_id=lead.id, account_id=account.id,
                      dedupe_key=f"followup1:{lead.id}", payload={"n": 1})
        jobs.schedule(s, "followup", _at_local(account, utcnow(), 3), lead_id=lead.id, account_id=account.id,
                      dedupe_key=f"followup2:{lead.id}", payload={"n": 2})


def _cold_first_contact(s, account: Account, lead: Lead) -> None:
    tpl, values = cold_first_message(account, lead)
    if tpl is None:  # not approved (yet): the lead stays queued, like a lead of a not-yet-live account
        log_event(s, "first_contact_blocked", account.id, lead.id, reason=values)
        return
    if send_to_lead(s, account, lead, kind="first", template=tpl, values=values):
        lang = L.lead_lang(account)
        lead.status = "contacted"
        # G13 safety net: if the agent's text doesn't say it, the AI introduces itself in its first reply
        lead.introduced = L.text("intro_marker", lang) in tpl.render(values, lang).lower()
        log_event(s, "first_message_sent", account.id, lead.id, seconds_after_lead=round((utcnow() - lead.created_at).total_seconds()),
                  template=tpl.name)
        # The owner never asked us: one message, no follow-ups (they read as pressure and hurt the agent's number).
        jobs.schedule(s, "mark_no_reply", utcnow() + timedelta(days=3), lead_id=lead.id, account_id=account.id, dedupe_key=f"noreply:{lead.id}")


@jobs.handler("followup")
def job_followup(s, job):
    account, lead = _load(s, job)
    if lead.has_replied or lead.status != "contacted":
        return
    ok, why = can_contact(account, lead, kind="followup")
    if why == "outside_hours":
        job.status = "pending"
        job.run_at = next_allowed_time(account, utcnow())
        return
    if not ok:
        return
    n = (job.payload or {}).get("n", 1)
    if n == 1:
        lang = L.lead_lang(account)
        sent = send_to_lead(s, account, lead, kind="followup", template=T.LEAD_FOLLOWUP_1,
                            values=[first_name(lead.name) or L.text("greeting_fallback", lang), lead.zone or L.text("your_zone", lang)])
    else:
        sent = send_to_lead(s, account, lead, kind="followup", template=T.LEAD_FOLLOWUP_2, values=[first_name(lead.name) or L.text("greeting_fallback", L.lead_lang(account))])
        if sent:
            jobs.schedule(s, "mark_no_reply", utcnow() + timedelta(days=2), lead_id=lead.id, account_id=account.id,
                          dedupe_key=f"noreply:{lead.id}")
    if sent:
        log_event(s, f"followup_{n}_sent", account.id, lead.id)


@jobs.handler("mark_no_reply")
def job_mark_no_reply(s, job):
    account, lead = _load(s, job)
    if not lead.has_replied and lead.status == "contacted":
        lead.status = "no_reply"
        log_event(s, "no_reply", account.id, lead.id)


@jobs.handler("reminder")
def job_reminder(s, job):
    account, lead = _load(s, job)
    meeting = s.execute(select(Meeting).where(Meeting.lead_id == lead.id)).scalar_one_or_none()
    if not meeting or meeting.status != "booked" or lead.do_not_contact:
        return
    which = (job.payload or {}).get("which", "24h")
    try:
        ev = calendar_for_account(s, account).get_event(meeting.google_event_id)
    except CalendarError as e:  # calendar unreachable: trust the time we booked
        log.warning("reminder: calendar error %s", e)
        ev = {"start": meeting.start, "end": meeting.end}
    if ev is not None and ev.get("all_day"):
        return
    if ev is None:  # the agent deleted the event: no reminder (J8)
        meeting.status = "cancelled"
        log_event(s, "meeting_cancelled_by_agent", account.id, lead.id)
        return
    if ev["start"] != meeting.start:  # the agent moved it: use the new time
        meeting.start, meeting.end = ev["start"], ev["end"]
        offset = timedelta(hours=24 if which == "24h" else 2)
        if ev["start"] - offset > utcnow() + timedelta(minutes=5):
            job.status = "pending"
            job.run_at = ev["start"] - offset
            return
    if ev["start"] < utcnow():
        return
    local = to_local(ev["start"], account.timezone)
    lang = L.lead_lang(account)
    where = L.where_phrase(meeting.address, meeting.type, lang)
    if lang == "es" and meeting.type == "call" and not meeting.address:  # the approved texts read "pasa por {where}" / "estará en {where}"
        where = "el teléfono (te llamará)" if which == "24h" else "contacto por teléfono"
    agent_first = first_name(account.agent_name) or L.text("the_agent", lang)
    lead_first = first_name(lead.name) or L.text("greeting_fallback", lang)
    at = L.fmt_time(local, lang)
    if which == "24h":
        tpl, values = T.MEETING_REMINDER_24H, [lead_first, at, agent_first, where]
    else:
        tpl, values = T.MEETING_REMINDER_2H, [lead_first, agent_first, where, at]
    if window_open(lead):
        sent = send_to_lead(s, account, lead, kind="reminder", text=tpl.render(values, lang))
    else:
        sent = send_to_lead(s, account, lead, kind="reminder", template=tpl, values=values)
    if sent:
        log_event(s, f"reminder_{which}_sent", account.id, lead.id)


@jobs.handler("ai_turn:failed")
def job_ai_turn_failed(s, job):  # G10: fail closed
    account, lead = _load(s, job)
    if lead.status not in ("handoff", "opted_out"):
        do_handoff(s, account, lead, L.text("why_tech_error", L.lead_lang(account)), notify_lead=False)


@jobs.handler("first_contact:failed")
def job_first_contact_failed(s, job):
    account, lead = _load(s, job)
    lead.status = "failed"
    log_event(s, "first_contact_failed", account.id, lead.id, error=job.last_error[:500])
    # never lose a lead silently: the agent hears about it and can call the owner themselves
    alert_agent(s, account, T.AGENT_NEEDS_YOU, [L.agent_hi(account), lead_summary(lead), L.text("why_first_failed", L.lead_lang(account))])


@jobs.handler("google_check")
def job_google_check(s, job):
    """Daily: refresh every agent's Google token. If an agent removed Sparrow in Google we find out within a day and
    delete their tokens, as the privacy policy promises."""
    nxt = utcnow() + timedelta(days=1)
    jobs.schedule(s, "google_check", nxt, dedupe_key=f"google_check:{nxt:%Y-%m-%d}")
    if get_settings().calendar_mode == "fake":
        return
    for integ in s.execute(select(Integration).where(Integration.provider == "google", Integration.status == "active")).scalars().all():
        if not integ.refresh_token_enc:
            continue
        account = s.get(Account, integ.account_id)
        try:
            data = gcal.refresh(decrypt(integ.refresh_token_enc))
        except CalendarError as e:
            if "invalid_grant" in str(e):
                gcal.mark_revoked(s, account, integ)
            else:
                log.warning("google check failed for account %s: %s", account.id, e)
            continue
        integ.token_enc = encrypt(data["access_token"])
        integ.expires_at = utcnow() + timedelta(seconds=int(data.get("expires_in", 3600)))


@jobs.handler("phone_health")
def job_phone_health(s, job):
    """Coexistence disconnects if the agent's phone app is inactive ~14 days: warn on day 12."""
    nxt = utcnow() + timedelta(days=1)
    jobs.schedule(s, "phone_health", nxt, dedupe_key=f"phone_health:{nxt:%Y-%m-%d}")
    for account in s.execute(select(Account).where(Account.status == "live")).scalars():
        last = account.last_agent_activity_at or account.created_at
        if utcnow() - last > timedelta(days=12):
            if not account.phone_inactive_alerted_at or account.phone_inactive_alerted_at < last:
                alert_agent(s, account, T.AGENT_PHONE_INACTIVE, [L.agent_hi(account)])
                account.phone_inactive_alerted_at = utcnow()


# ---------------------------------------------------------------- inbound (webhooks)

def _find_lead(s, account: Account, phone: str) -> Lead | None:
    p = normalize_phone(phone)
    if not p:
        return None
    return s.execute(select(Lead).where(Lead.account_id == account.id, Lead.phone == p)).scalar_one_or_none()


def _already_have(s, wa_message_id: str | None) -> bool:
    return bool(wa_message_id) and s.execute(select(Message.id).where(Message.wa_message_id == wa_message_id)).first() is not None


NO_THANKS = {"no", "no gracias", "no, gracias", "non", "non merci", "non, merci", "no thanks", "no, thanks", "no thank you"}  # a plain "no" answering follow-up 2


def _last_template(s, lead: Lead) -> str:
    row = s.execute(select(Message.template_name).where(Message.lead_id == lead.id, Message.direction == "out")
                    .order_by(Message.id.desc())).first()
    return row[0] if row else ""


def handle_inbound(s, account: Account, from_phone: str, msg: dict) -> str:
    """A lead wrote to the agent's number. msg: {id, type, text?, audio_id?}"""
    lead = _find_lead(s, account, from_phone)
    if not lead:
        return "ignored_not_a_lead"  # G1: never read or answer non-lead chats
    if _already_have(s, msg.get("id")):
        return "duplicate"  # G3
    text = msg.get("text") or ""
    mtype = msg.get("type", "text")
    if mtype == "audio" and msg.get("audio_id"):
        try:
            audio, mime = agent_wa(s, account).download_media(msg["audio_id"])
            text = transcribe(audio, mime, L.lead_lang(account)) or ""
        except Exception:
            log.exception("audio download failed")
        text = f"[nota de voz] {text}" if text else "[nota de voz sin transcribir]"
    elif mtype not in ("text", "button", "interactive"):
        text = text or f"[{mtype}]"
    s.add(Message(account_id=account.id, lead_id=lead.id, direction="in", sender="lead", type=mtype, text=text,
                  wa_message_id=msg.get("id"), status="received"))
    now = utcnow()
    first_reply = not lead.has_replied
    lead.last_inbound_at = now
    lead.has_replied = True
    if first_reply:
        log_event(s, "replied", account.id, lead.id)
    jobs.cancel(s, lead.id, ["followup", "mark_no_reply", "first_contact"])

    # Exact STOP/BAJA/ARRÊT → opt out in code, no LLM needed. A plain "no" counts only as the answer to follow-up 2.
    word = text.strip().lower().rstrip(".!")
    if word not in OPT_OUT_WORDS and not lead.do_not_contact and not lead.human_owned:
        hard = cold_hard_stop(text)  # data request / threat to report (any lead, not only cold ones): the agent must be told (GDPR: one month to answer)
        if hard in ("data", "threat"):
            return cold_hard_stop_out(s, account, lead, hard)
    if word in OPT_OUT_WORDS or is_opt_out_text(text) or (word in NO_THANKS and _last_template(s, lead) == T.LEAD_FOLLOWUP_2.name):
        opt_out(s, account, lead, source="keyword")
        return "opted_out"

    if lead.do_not_contact or lead.human_owned or lead.status in ("handoff", "opted_out", "skipped"):
        return "stored_only"
    if is_cold(lead):  # an owner we wrote to first: the hard stops and the soft-no ladder are decided here, in code
        hard = cold_hard_stop(text)
        if hard:
            return cold_hard_stop_out(s, account, lead, hard)
        if word in NO_THANKS and (first_reply or word not in ("no", "non", "nope")):  # a bare "no" mid-chat answers a question, it is not a refusal
            return cold_soft_no(s, account, lead)
    if lead.status in ("new", "queued", "contacted", "no_reply", "not_interested"):
        lead.status = "in_conversation"
    if mtype == "audio" and "sin transcribir" in text:
        send_to_lead(s, account, lead, kind="ai_reply", text=L.text("voice_not_supported", L.lead_lang(account)))
        return "asked_to_write"

    # Burst grouping: wait ~30s after the last message, max ~60s after the first one
    st = get_settings()
    pending = s.execute(select(Job).where(Job.dedupe_key == f"ai_turn:{lead.id}", Job.status == "pending")).scalar_one_or_none()
    if not pending:
        lead.burst_started_at = now
    burst_start = lead.burst_started_at or now
    run_at = min(now + timedelta(seconds=st.reply_delay), burst_start + timedelta(seconds=st.reply_max_delay))
    jobs.schedule(s, "ai_turn", run_at, lead_id=lead.id, account_id=account.id, dedupe_key=f"ai_turn:{lead.id}",
                  reuse=True, replace_run_at=True)
    return "scheduled"


def handle_echo(s, account: Account, to_phone: str, msg: dict) -> str:
    """The agent wrote from their WhatsApp Business app (smb_message_echoes)."""
    account.last_agent_activity_at = utcnow()
    lead = _find_lead(s, account, to_phone)
    if not lead:
        return "ignored_not_a_lead"  # G1
    if _already_have(s, msg.get("id")):
        return "duplicate"
    s.add(Message(account_id=account.id, lead_id=lead.id, direction="out", sender="agent", type=msg.get("type", "text"),
                  text=msg.get("text") or f"[{msg.get('type', 'mensaje')}]", wa_message_id=msg.get("id"), status="sent"))
    if lead.has_replied:
        # J5: the agent wrote after the lead replied → the chat is theirs, permanently
        lead.human_owned = True
        jobs.cancel(s, lead.id, ["ai_turn", "followup", "mark_no_reply", "first_contact"])
        log_event(s, "agent_took_over", account.id, lead.id)
        return "human_owned"
    # J3: agent-first opening. No template from us; the AI answers when the lead replies.
    jobs.cancel(s, lead.id, ["first_contact", "followup", "mark_no_reply"])
    if lead.status in ("new", "queued"):
        lead.status = "contacted"
    log_event(s, "agent_opened", account.id, lead.id)
    return "agent_opening"


def handle_status(s, wa_message_id: str, status: str, error: str = "") -> None:
    m = s.execute(select(Message).where(Message.wa_message_id == wa_message_id)).scalar_one_or_none()
    if not m:
        return
    order = ["", "sent", "delivered", "read"]
    if status == "failed":
        m.status, m.error = "failed", error
        log_event(s, "delivery_failed", m.account_id, m.lead_id, error=error)
    elif status in order and order.index(status) > (order.index(m.status) if m.status in order else 0):
        m.status = status


def handle_sparrow_inbound(s, from_phone: str, msg: dict) -> str:
    """An agent wrote to the Sparrow number. v1 command: 'Sigue con Marta' → resume the AI for that lead."""
    p = normalize_phone(from_phone)
    account = s.execute(select(Account).where(Account.personal_whatsapp == p)).scalars().first() if p else None
    if not account:
        return "unknown_sender"
    if _already_have(s, msg.get("id")):
        return "duplicate"  # G3
    text = (msg.get("text") or "").strip()
    s.add(Message(account_id=account.id, direction="in", sender="agent", type=msg.get("type", "text"), text=text,
                  wa_message_id=msg.get("id"), status="received"))
    wa = sparrow_wa()

    def reply(body: str) -> None:
        if not wa:
            return
        wid = wa.send_text(p, body)
        s.add(Message(account_id=account.id, direction="out", sender="system", type="text", text=body, wa_message_id=wid, status="sent"))

    lang = L.lead_lang(account)
    m = re.match(r"^\s*(?:sigue|reprends|reprenez|continue)(?:\s+(?:con|avec|with))?\s+(.+)$", text, re.IGNORECASE)
    if not m:
        reply(L.text("resume_help", lang))
        return "help"
    wanted = m.group(1).strip().lower()
    candidates = s.execute(
        select(Lead).where(Lead.account_id == account.id, (Lead.human_owned.is_(True)) | (Lead.status == "handoff"))
    ).scalars().all()
    matches = [l for l in candidates if wanted in (l.name or "").lower() or wanted.replace(" ", "") in l.phone]
    if len(matches) != 1:
        names = ", ".join(sorted({l.name or l.phone for l in candidates})[:8]) or L.text("resume_none", lang)
        reply(L.text("resume_ambiguous", lang, names=names))
        return "ambiguous"
    lead = matches[0]
    lead.human_owned = False
    lead.status = "in_conversation"
    lead.introduced = True  # the lead already met the assistant or the agent
    log_event(s, "ai_resumed", account.id, lead.id)
    jobs.schedule(s, "ai_turn", utcnow(), lead_id=lead.id, account_id=account.id, dedupe_key=f"ai_turn:{lead.id}",
                  reuse=True, replace_run_at=True, payload={"resumed": True})
    reply(L.text("resume_done", lang, name=lead.name or lead.phone))
    return "resumed"


# ---------------------------------------------------------------- outcomes

def opt_out(s, account: Account, lead: Lead, *, source: str) -> None:
    already = lead.do_not_contact
    lead.do_not_contact = True
    lead.status = "opted_out"
    jobs.cancel(s, lead.id, ["ai_turn", "followup", "mark_no_reply", "first_contact", "reminder"])
    log_event(s, "opted_out", account.id, lead.id, source=source)
    if already:
        return
    try:
        send_to_lead(s, account, lead, kind="opt_out_confirmation", text=L.text("opt_out_confirmation", L.lead_lang(account)))
    except WhatsAppError:
        pass


_CHANGE_RE = re.compile(r"cancel|anul|cambi|mover|aplaz|reprogram|annul|chang|report|reschedul|postpon|move", re.I)


def do_handoff(s, account: Account, lead: Lead, reason: str, *, notify_lead: bool = True) -> None:
    lead.status = "handoff"
    lead.human_owned = True
    jobs.cancel(s, lead.id, ["ai_turn", "followup", "mark_no_reply", "first_contact"])
    meeting = s.execute(select(Meeting).where(Meeting.lead_id == lead.id)).scalar_one_or_none()
    if meeting is not None and meeting.status == "booked" and _CHANGE_RE.search(reason or ""):
        # the owner wants to change or cancel: no automatic reminders for a time that may no longer stand (the agent decides)
        meeting.status = "change_requested"
        jobs.cancel(s, lead.id, ["reminder"])
        log_event(s, "meeting_change_requested", account.id, lead.id)
    log_event(s, "handoff", account.id, lead.id, reason=reason)
    if notify_lead:
        try:
            send_to_lead(s, account, lead, kind="handoff_notice",
                         text=L.text("handoff_to_lead", L.lead_lang(account), agent_first_name=first_name(account.agent_name) or L.text("the_agent", L.lead_lang(account))))
        except WhatsAppError:
            pass
    last_in = s.execute(select(Message.text).where(Message.lead_id == lead.id, Message.direction == "in")
                        .order_by(Message.id.desc())).first()
    why = _clip(reason, 80) + (f' ("{_clip(last_in[0], 80)}")' if last_in else "")
    alert_agent(s, account, T.AGENT_NEEDS_YOU, [L.agent_hi(account), lead_summary(lead), why])


def cold_hard_stop_out(s, account: Account, lead: Lead, kind: str) -> str:
    """A cold owner said stop in words, asked about their data, or threatened to report: final, no pitch.
    'stop' → the normal opt-out. 'data' / 'threat' → do_not_contact, one acknowledgement, and the agent is told why."""
    if kind == "stop":
        opt_out(s, account, lead, source="cold_words")
        return "opted_out"
    lang = L.lead_lang(account)
    lead.do_not_contact = True
    log_event(s, "opted_out", account.id, lead.id, source=f"cold_{kind}")
    try:  # the opt-out confirmation kind is the only one allowed once do_not_contact is set
        send_to_lead(s, account, lead, kind="opt_out_confirmation",
                     text=L.text("privacy_ack", lang, agent_first=first_name(account.agent_name) or L.text("the_agent", lang)))
    except WhatsAppError:
        pass
    do_handoff(s, account, lead, L.text(f"why_cold_{kind}", lang), notify_lead=False)
    return f"handoff_{kind}"


def cold_soft_no(s, account: Account, lead: Lead) -> str:
    """'No quiero agencias' and the like, from an owner we wrote to first. The first time: ONE reply that respects
    the answer and leaves the door open (the agent's own success_pitch, verbatim, or a neutral line; never the
    model's words, so nothing can be invented). Any later no: the final goodbye and do_not_contact.
    The count lives in lead.facts["_soft_no"] (a code-only key: save_facts can't touch it, the prompt never sees it)."""
    facts = dict(lead.facts or {})
    n = int(facts.get("_soft_no") or 0)
    facts["_soft_no"] = n + 1
    lead.facts = facts
    if n >= 1:
        opt_out(s, account, lead, source="cold_second_no")
        return "opted_out"
    lang = L.lead_lang(account)
    agent_first = first_name(tidy_name(account.agent_name)) or L.text("the_agent", lang)
    pitch = " ".join(str(prospecting_cfg(account).get("success_pitch") or "").split())
    text = f"{L.text('cold_soft_no_ack', lang)} {pitch or L.text('cold_door_open', lang, agent_first=agent_first)}"
    if not lead.introduced:  # G13
        text = L.text("intro_phrase", lang, agent_first=agent_first) + text
    lead.status = "not_interested"
    jobs.cancel(s, lead.id, ["ai_turn", "followup", "mark_no_reply"])
    log_event(s, "cold_soft_no", account.id, lead.id, n=1)
    try:
        if send_to_lead(s, account, lead, kind="ai_reply", text=text):
            lead.introduced = True
    except WhatsAppError:
        pass
    return "soft_no_pitch"


def _cold_pitch_repeated(account: Account, lead: Lead, reply: str) -> bool:
    """After the one pitch, the model must not send it (or the default door-open line) again."""
    if int((lead.facts or {}).get("_soft_no") or 0) < 1:
        return False
    lang = L.lead_lang(account)
    norm = lambda x: " ".join((x or "").lower().split())
    pitch = norm(prospecting_cfg(account).get("success_pitch"))
    door = norm(L.text("cold_door_open", lang, agent_first=""))
    r = norm(reply)
    return bool((pitch and pitch[:40] in r) or door[:30] in r)


# ---------------------------------------------------------------- slots & booking

def compute_free_slots(s, account: Account, max_slots: int = 6) -> list[dict]:
    cfg = settings_for(account)
    tz = ZoneInfo(account.timezone)
    now = utcnow()
    earliest = now + timedelta(hours=cfg["min_notice_hours"])
    horizon = now + timedelta(days=cfg["days_ahead"])
    dur = timedelta(minutes=cfg["meeting_minutes"])
    buf = timedelta(minutes=cfg["buffer_minutes"])
    busy = calendar_for_account(s, account).busy(now, horizon + timedelta(days=1))
    busy += [(m.start, m.end) for m in s.execute(select(Meeting).where(Meeting.account_id == account.id, Meeting.status == "booked")).scalars()]
    out: list[dict] = []
    per_day: dict = {}
    day = to_local(now, account.timezone).date()
    for _ in range(cfg["days_ahead"] + 1):
        if day.weekday() in cfg["slot_days"]:
            for w_start, w_end in cfg["slot_windows"]:
                t = datetime.combine(day, parse_hm(w_start), tzinfo=tz)
                end_w = datetime.combine(day, parse_hm(w_end), tzinfo=tz)
                while t + dur <= end_w:
                    su = to_utc_naive(t)
                    eu = su + dur
                    if su >= earliest and su <= horizon and not any(b0 < eu + buf and b1 > su - buf for b0, b1 in busy):
                        if per_day.get(day, 0) < 2:
                            out.append({"slot_id": t.strftime("%Y-%m-%dT%H:%M"), "label": when_label(su, account.timezone, L.lead_lang(account))})
                            per_day[day] = per_day.get(day, 0) + 1
                            t += timedelta(hours=3)  # spread the options within a day
                            continue
                    t += timedelta(minutes=30)
        day += timedelta(days=1)
        if len(out) >= max_slots:
            break
    return out[:max_slots]


def _slot_to_utc(slot_id: str, tz: str) -> datetime:
    return to_utc_naive(datetime.strptime(slot_id, "%Y-%m-%dT%H:%M").replace(tzinfo=ZoneInfo(tz)))


def event_description(account: Account, lead: Lead, meeting_type: str) -> str:
    lang = L.lead_lang(account)
    t = lambda k: L.text(k, lang)
    ads = {"es": "anuncio", "fr": "annonce", "en": "listing"}[lang]
    f = lead.facts or {}
    lines = [f"📞 {lead.phone} · WhatsApp: wa.me/{lead.phone.lstrip('+')}"]
    prop = ", ".join(p for p in [lead.property_type, lead.zone] if p)
    if lead.price:
        price = f"{lead.price:,.0f}"
        prop += f" · {price.replace(',', '.') if lang == 'es' else price.replace(',', ' ' if lang == 'fr' else ',')} €"
    if lead.listing_url:
        prop += f" ({ads}: {lead.listing_url})"
    if prop:
        lines.append(f"🏠 {prop}")
    cond = {"es": "Estado", "fr": "État", "en": "Condition"}[lang]
    if f.get("condition"):
        lines.append(f"🔧 {cond}: {f['condition']}")
    motive = " · ".join(x for x in [f"{t('ev_reason')}: {f['reason']}" if f.get("reason") else "", f"{t('ev_timeline')}: {f['timeline']}" if f.get("timeline") else ""] if x)
    if motive:
        lines.append(f"🗣 {motive}")
    if f.get("occupancy"):
        lines.append(f"🔑 {t('ev_occupancy')}: {f['occupancy']}")
    if f.get("decision_maker"):
        decides = {"es": "Decide", "fr": "Décide", "en": "Decides"}[lang]
        lines.append(f"👥 {decides}: {f['decision_maker']}")
    if f.get("other_agency"):
        lines.append(f"🏢 {t('ev_other_agency')}: {f['other_agency']}")
    if f.get("notes"):
        lines.append(f"💬 {f['notes']}")
    lines.append(f"{t('ev_type')}: {t('ev_visit') if meeting_type == 'visit' else t('ev_call')} · {t('ev_origin')}: {lead.source}")
    booked = {"es": "Agendado por Sparrow AI", "fr": "Planifié par Sparrow AI", "en": "Booked by Sparrow AI"}[lang]
    lines.append(f"— {booked}")
    return "\n".join(lines)


def _short_name(lead: Lead) -> str:
    """'Marta Pérez López' → 'Marta P.' (the lead's privacy in a calendar that may be shared)."""
    parts = (lead.name or "").split()
    if not parts:
        return lead.phone
    return parts[0] + (f" {parts[1][0]}." if len(parts) > 1 else "")


def book_slot(s, account: Account, lead: Lead, slot_id: str, meeting_type: str, address: str = "") -> dict:
    if slot_id not in (lead.offered_slots or []):
        return {"ok": False, "error": "slot_not_offered", "hint": "Usa un slot_id devuelto por get_free_slots."}  # G12
    lock = _booking_locks.setdefault(account.id, threading.Lock())
    with lock:  # G5 (plus the unique lead_id on meetings)
        existing = s.execute(select(Meeting).where(Meeting.lead_id == lead.id)).scalar_one_or_none()
        if existing and existing.status == "booked":
            return {"ok": False, "error": "already_booked", "when": when_label(existing.start, account.timezone, L.lead_lang(account))}
        cfg = settings_for(account)
        start = _slot_to_utc(slot_id, account.timezone)
        end = start + timedelta(minutes=cfg["meeting_minutes"])
        if start < utcnow() + timedelta(minutes=30):
            return {"ok": False, "error": "slot_too_soon"}
        cal = calendar_for_account(s, account)
        if cal.busy(start - timedelta(minutes=cfg["buffer_minutes"]), end + timedelta(minutes=cfg["buffer_minutes"])):
            return {"ok": False, "error": "slot_taken", "hint": "Ese hueco se acaba de ocupar. Llama a get_free_slots y ofrece otros."}
        clash = s.execute(select(Meeting).where(Meeting.account_id == account.id, Meeting.status == "booked",
                                                Meeting.start < end, Meeting.end > start)).first()
        if clash:
            return {"ok": False, "error": "slot_taken"}
        if address:
            lead.address = address
        jobs.cancel(s, lead.id, ["reminder"])  # reminders of an older booking must not fire
        where = lead.address if meeting_type == "visit" else L.text("ev_call_where", L.lead_lang(account), phone=lead.phone)
        prop = " ".join(p for p in [lead.property_type, lead.zone] if p)
        summary = f"{L.text('ev_summary', L.lead_lang(account))} · {_short_name(lead)}" + (f" · {prop}" if prop else "") + (" · TEST" if lead.is_test else "")
        event_id = cal.create_event(
            summary=summary,
            description=event_description(account, lead, meeting_type),
            location=where or "",
            start_utc=start,
            end_utc=end,
            timezone=account.timezone,
            request_id=f"lead-{lead.id}",
        )
        if existing:  # a previous booking was cancelled: reuse the row
            existing.start, existing.end, existing.type, existing.address = start, end, meeting_type, lead.address
            existing.google_event_id, existing.status, existing.is_test = event_id, "booked", lead.is_test
        else:
            s.add(Meeting(lead_id=lead.id, account_id=account.id, start=start, end=end, type=meeting_type,
                          address=lead.address, google_event_id=event_id, is_test=lead.is_test))
        lead.status = "booked"  # G6: only after the event exists
        s.commit()  # the event exists in Google: never lose (or duplicate) its record
    log_event(s, "meeting_booked", account.id, lead.id, start=start.isoformat(), type=meeting_type, test=lead.is_test)
    for which, delta in (("24h", timedelta(hours=24)), ("2h", timedelta(hours=2))):
        if start - delta > utcnow() + timedelta(minutes=10):
            jobs.schedule(s, "reminder", start - delta, lead_id=lead.id, account_id=account.id,
                          dedupe_key=f"reminder{which}:{lead.id}:{slot_id}", payload={"which": which})
    return {"ok": True, "when": when_label(start, account.timezone, L.lead_lang(account)), "where": where}


# ---------------------------------------------------------------- the AI turn

def build_messages(s, account: Account, lead: Lead) -> list[dict]:
    rows = s.execute(select(Message).where(Message.lead_id == lead.id, Message.status != "failed")
                     .order_by(Message.id.desc()).limit(40)).scalars().all()[::-1]
    lang = L.lead_lang(account)
    agent_first = first_name(account.agent_name) or L.text("the_agent", lang)
    cap = get_settings().ai_max_user_chars
    msgs: list[dict] = []
    for m in rows:
        if m.direction == "in":
            role, text = "user", m.text if len(m.text) <= cap else m.text[:cap] + L.text("msg_clipped", lang)
        elif m.sender == "agent":
            role, text = "assistant", f"{L.text('agent_wrote', lang, agent_first=agent_first)}: {m.text}"
        else:
            role, text = "assistant", m.text
        if not text:
            continue
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n" + text
        else:
            msgs.append({"role": role, "content": text})
    if msgs and msgs[0]["role"] == "assistant":
        msgs.insert(0, {"role": "user", "content": L.text("conversation_start", lang)})
    return msgs


@jobs.handler("ai_turn")
def job_ai_turn(s, job):
    account, lead = _load(s, job)
    run_ai_turn(s, account, lead)


def _ai_over_limit(s, account: Account, lead: Lead) -> str | None:
    """Caps on AI turns so nobody can burn the token budget: per lead (day, week) and per agent (day).
    Returns the window that is full, or None."""
    st, now = get_settings(), utcnow()

    def turns(since: datetime, *, whole_account: bool = False) -> int:
        q = select(func.count(LlmTurn.id)).where(LlmTurn.created_at > since)
        if whole_account:
            q = q.join(Lead, Lead.id == LlmTurn.lead_id).where(Lead.account_id == account.id)
        else:
            q = q.where(LlmTurn.lead_id == lead.id)
        return s.execute(q).scalar_one()

    if turns(now - timedelta(days=1)) >= st.ai_max_turns_per_lead_day:
        return "lead_day"
    if turns(now - timedelta(days=7)) >= st.ai_max_turns_per_lead_week:
        return "lead_week"
    if turns(now - timedelta(days=1), whole_account=True) >= st.ai_max_turns_per_account_day:
        return "account_day"
    return None


def run_ai_turn(s, account: Account, lead: Lead) -> str:
    ok, why = can_contact(account, lead, kind="ai_reply")
    if not ok:
        return f"blocked:{why}"
    if lead.status in ("handoff", "opted_out", "skipped"):
        return "not_active"
    messages = build_messages(s, account, lead)
    if not messages or messages[-1]["role"] != "user":
        return "nothing_to_answer"
    if not window_open(lead):
        return "window_closed"
    full = _ai_over_limit(s, account, lead)
    if full:  # never call the model: a person takes over (one lead) or we just stop and log it (whole agent)
        log_event(s, "ai_limit_reached", account.id, lead.id, window=full)
        if full.startswith("lead"):
            do_handoff(s, account, lead, L.text("why_limit", L.lead_lang(account)))
            return "limit_handoff"
        return "account_budget"

    started = utcnow()
    state = {"terminal": None, "reason": ""}

    def execute(name: str, args: dict) -> dict:
        if name == "get_free_slots":
            slots = compute_free_slots(s, account)
            lead.offered_slots = sorted(set((lead.offered_slots or []) + [x["slot_id"] for x in slots]))
            if lead.status == "in_conversation":
                lead.status = "offering_slots"
            log_event(s, "slots_offered", account.id, lead.id, n=len(slots))
            return {"slots": slots} if slots else {"slots": [], "hint": "No hay huecos: usa handoff para que el agente proponga hora."}
        if name == "book_slot":
            return book_slot(s, account, lead, str(args.get("slot_id", "")), args.get("meeting_type", "visit"), args.get("address", "") or "")
        if name == "save_facts":
            facts = dict(lead.facts or {})
            facts.update({k: v for k, v in args.items() if v and not str(k).startswith("_")})  # "_" keys belong to code
            lead.facts = facts
            if args.get("address"):
                lead.address = args["address"]
            return {"ok": True}
        if name == "handoff":
            state["terminal"], state["reason"] = "handoff", args.get("reason", "")
            return {"ok": True, "note": "El sistema avisa al agente y envía el mensaje al lead. No escribas nada más."}
        if name == "stop":
            state["terminal"], state["reason"] = "stop", args.get("reason", "not_interested")
            return {"ok": True}
        return {"ok": False, "error": f"unknown tool {name}"}

    turn = LlmTurn(lead_id=lead.id)
    s.add(turn)
    t0 = time.time()
    try:
        system = build_system_prompt(account, lead, must_introduce=not lead.introduced)
        reply, transcript, model = run_tool_loop(system, messages, execute, stop_after=lambda: state["terminal"] is not None)
        turn.transcript, turn.reply, turn.model = transcript, reply, model
    except Exception as e:  # G10: never guess, hand over
        log.exception("LLM turn failed for lead %s", lead.id)
        turn.error = repr(e)[:2000]
        turn.latency_ms = int((time.time() - t0) * 1000)
        do_handoff(s, account, lead, L.text("why_tech_error", L.lead_lang(account)))
        return "error_handoff"
    turn.latency_ms = int((time.time() - t0) * 1000)

    s.flush()
    s.refresh(lead)
    if lead.human_owned and state["terminal"] != "handoff":
        return "agent_took_over"  # G8: echo arrived while the model was thinking
    if lead.last_inbound_at and lead.last_inbound_at > started and state["terminal"] is None:
        return "newer_message"  # the re-armed job will answer everything together

    cold = is_cold(lead)
    if state["terminal"] == "handoff":
        tag = (state["reason"] or "").strip().lower()
        if cold and (tag.startswith("[privacy]") or tag.startswith("[threat]")):  # the prompt's tags for data requests / threats
            return cold_hard_stop_out(s, account, lead, "data" if tag.startswith("[privacy]") else "threat")
        do_handoff(s, account, lead, state["reason"] or L.text("why_unsure", L.lead_lang(account)))
        return "handoff"
    if state["terminal"] == "stop":
        if state["reason"] == "opted_out":
            opt_out(s, account, lead, source="llm")
            return "opted_out"
        if cold:  # the soft-no ladder is code: one pitch (our text, not the model's), then the end
            return cold_soft_no(s, account, lead)
        lead.status = "not_interested"
        jobs.cancel(s, lead.id, ["followup", "mark_no_reply"])
        log_event(s, "not_interested", account.id, lead.id)
        try:
            send_to_lead(s, account, lead, kind="ai_reply",
                         text=reply or L.text("ai_no_reply_fallback", L.lead_lang(account)))
        except WhatsAppError:
            pass
        return "not_interested"

    reply = (reply or "").strip()
    if not reply and lead.status == "booked":  # the meeting exists: confirm it in our own words, never "I'll pass you to the agent"
        m = s.execute(select(Meeting).where(Meeting.lead_id == lead.id, Meeting.status == "booked")).scalars().first()
        if m is not None:
            lg = L.lead_lang(account)
            reply = L.text("booked_fallback", lg, name=first_name(tidy_name(lead.name)) or L.text("greeting_fallback", lg),
                           agent=first_name(tidy_name(account.agent_name)) or L.text("your_agent", lg),
                           when=L.short_when(to_local(m.start, account.timezone), lg))
    if not reply:
        do_handoff(s, account, lead, L.text("why_no_reply", L.lead_lang(account)))
        return "empty_handoff"
    if cold and _cold_pitch_repeated(account, lead, reply):  # a second pitch is never sent: the agent decides what next
        log_event(s, "cold_second_pitch_blocked", account.id, lead.id)
        do_handoff(s, account, lead, L.text("why_unsure", L.lead_lang(account)), notify_lead=False)
        return "second_pitch_blocked"
    lang = L.lead_lang(account)
    agent_first = first_name(account.agent_name) or L.text("your_agent", lang)
    if not lead.introduced and L.text("intro_marker", lang) not in reply.lower():  # G13
        reply = L.text("intro_phrase", lang, agent_first=agent_first) + reply
    parts = [p.strip() for p in re.split(r"\n\s*\n", reply) if p.strip()][:2]
    try:
        for p in parts:
            send_to_lead(s, account, lead, kind="ai_reply", text=p)
            lead.introduced = True
    except WhatsAppError as e:
        do_handoff(s, account, lead, L.text("send_failed_reason", L.lead_lang(account), err=e), notify_lead=False)
        return "send_failed_handoff"
    return "replied"
