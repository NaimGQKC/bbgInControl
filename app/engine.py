"""The conversation engine: intake → first message → AI chat → booking → reminders, plus handover and alerts."""
import logging
import re
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from . import jobs, templates as T
from .config import get_settings
from .crypto import decrypt
from .db import utcnow
from .gcal import CalendarError, calendar_for_account
from .guards import (
    DAYS,
    HOT_STAGES,
    NO_AI_TAGS,
    OPT_OUT_WORDS,
    can_contact,
    first_name,
    next_allowed_time,
    normalize_phone,
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
from .whatsapp import FakeWhatsApp, WhatsAppClient, WhatsAppError

log = logging.getLogger("sparrow.engine")
_booking_locks: dict[int, threading.Lock] = {}

MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


# ---------------------------------------------------------------- helpers

def when_label(dt_utc: datetime, tz: str) -> str:
    d = to_local(dt_utc, tz)
    return f"{DAYS[d.weekday()]} {d.day} de {MONTHS[d.month - 1]}, {d:%H:%M}"


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
    try:
        if template is not None:
            wid = wa.send_template(lead.phone, template, values or [])
            _store_out(s, account, lead, sender=sender, type_="template", text=template.render(values or []), template_name=template.name, wa_id=wid)
        else:
            wid = wa.send_text(lead.phone, text)
            _store_out(s, account, lead, sender=sender, type_="text", text=text, wa_id=wid)
        s.commit()  # the message is out: never lose its record (and never resend it on a retry)
    except WhatsAppError as e:
        _store_out(s, account, lead, sender=sender, type_="template" if template else "text",
                   text=template.render(values or []) if template else text or "", template_name=template.name if template else "",
                   status="failed", error=str(e))
        log_event(s, "send_failed", account.id, lead.id, error=str(e), code=e.code)
        s.commit()
        raise
    return True


def _sparrow_template(s, account: Account, to: str | None, template: T.Template, values: list[str]) -> None:
    wa = sparrow_wa()
    if not wa or not to:
        log.warning("cannot send %s for account %s (no Sparrow number or no recipient): %s", template.name, account.id, template.render(values))
        log_event(s, "alert_not_sent", account.id, template=template.name, text=template.render(values))
        return
    try:
        wid = wa.send_template(to, template, values)
        s.add(Message(account_id=account.id, direction="out", sender="system", type="template",
                      text=template.render(values), template_name=template.name, wa_message_id=wid, status="sent"))
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
    phone = normalize_phone(data.get("phone"))
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
    return [
        first_name(lead_name) or "buenas",
        account.agent_name or "tu agente",
        account.agency or "tu inmobiliaria",
        zone or (account.zones.split(",")[0].strip() if account.zones else "tu zona"),
        first_name(account.agent_name) or "el agente",
    ]


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
    if send_to_lead(s, account, lead, kind="first", template=T.LEAD_FIRST_CONTACT, values=first_contact_values(account, lead.name, lead.zone)):
        lead.status = "contacted"
        lead.introduced = True
        latency = (utcnow() - lead.created_at).total_seconds()
        log_event(s, "first_message_sent", account.id, lead.id, seconds_after_lead=round(latency))
        jobs.schedule(s, "followup", _at_local(account, utcnow(), 1), lead_id=lead.id, account_id=account.id,
                      dedupe_key=f"followup1:{lead.id}", payload={"n": 1})
        jobs.schedule(s, "followup", _at_local(account, utcnow(), 3), lead_id=lead.id, account_id=account.id,
                      dedupe_key=f"followup2:{lead.id}", payload={"n": 2})


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
        sent = send_to_lead(s, account, lead, kind="followup", template=T.LEAD_FOLLOWUP_1,
                            values=[first_name(lead.name) or "buenas", lead.zone or "tu zona"])
    else:
        sent = send_to_lead(s, account, lead, kind="followup", template=T.LEAD_FOLLOWUP_2, values=[first_name(lead.name) or "buenas"])
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
    where = meeting.address or ("por teléfono" if meeting.type == "call" else "tu vivienda")
    agent_first = first_name(account.agent_name) or "el agente"
    lead_first = first_name(lead.name) or "buenas"
    if which == "24h":
        tpl, values = T.MEETING_REMINDER_24H, [lead_first, f"{local:%H:%M}", agent_first, where]
    else:
        tpl, values = T.MEETING_REMINDER_2H, [lead_first, agent_first, where, f"{local:%H:%M}"]
    if window_open(lead):
        sent = send_to_lead(s, account, lead, kind="reminder", text=tpl.render(values))
    else:
        sent = send_to_lead(s, account, lead, kind="reminder", template=tpl, values=values)
    if sent:
        log_event(s, f"reminder_{which}_sent", account.id, lead.id)


@jobs.handler("ai_turn:failed")
def job_ai_turn_failed(s, job):  # G10: fail closed
    account, lead = _load(s, job)
    if lead.status not in ("handoff", "opted_out"):
        do_handoff(s, account, lead, "error técnico del asistente", notify_lead=False)


@jobs.handler("first_contact:failed")
def job_first_contact_failed(s, job):
    account, lead = _load(s, job)
    lead.status = "failed"
    log_event(s, "first_contact_failed", account.id, lead.id, error=job.last_error[:500])


@jobs.handler("phone_health")
def job_phone_health(s, job):
    """Coexistence disconnects if the agent's phone app is inactive ~14 days: warn on day 12."""
    nxt = utcnow() + timedelta(days=1)
    jobs.schedule(s, "phone_health", nxt, dedupe_key=f"phone_health:{nxt:%Y-%m-%d}")
    for account in s.execute(select(Account).where(Account.status == "live")).scalars():
        last = account.last_agent_activity_at or account.created_at
        if utcnow() - last > timedelta(days=12):
            if not account.phone_inactive_alerted_at or account.phone_inactive_alerted_at < last:
                alert_agent(s, account, T.AGENT_PHONE_INACTIVE, [first_name(account.agent_name) or "hola"])
                account.phone_inactive_alerted_at = utcnow()


# ---------------------------------------------------------------- inbound (webhooks)

def _find_lead(s, account: Account, phone: str) -> Lead | None:
    p = normalize_phone(phone)
    if not p:
        return None
    return s.execute(select(Lead).where(Lead.account_id == account.id, Lead.phone == p)).scalar_one_or_none()


def _already_have(s, wa_message_id: str | None) -> bool:
    return bool(wa_message_id) and s.execute(select(Message.id).where(Message.wa_message_id == wa_message_id)).first() is not None


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
            text = transcribe(audio, mime) or ""
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

    # Exact STOP/BAJA → opt out in code, no LLM needed. A plain "no" counts only as the answer to follow-up 2.
    word = text.strip().lower().rstrip(".!")
    if word in OPT_OUT_WORDS or (word in ("no", "no gracias", "no, gracias") and _last_template(s, lead) == T.LEAD_FOLLOWUP_2.name):
        opt_out(s, account, lead, source="keyword")
        return "opted_out"

    if lead.do_not_contact or lead.human_owned or lead.status in ("handoff", "opted_out", "skipped"):
        return "stored_only"
    if lead.status in ("new", "queued", "contacted", "no_reply", "not_interested"):
        lead.status = "in_conversation"
    if mtype == "audio" and "sin transcribir" in text:
        send_to_lead(s, account, lead, kind="ai_reply", text=T.VOICE_NOT_SUPPORTED)
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

    m = re.match(r"^\s*sigue(?:\s+con)?\s+(.+)$", text, re.IGNORECASE)
    if not m:
        reply("Para que tu asistente retome una conversación, escribe: Sigue con <nombre del cliente>")
        return "help"
    wanted = m.group(1).strip().lower()
    candidates = s.execute(
        select(Lead).where(Lead.account_id == account.id, (Lead.human_owned.is_(True)) | (Lead.status == "handoff"))
    ).scalars().all()
    matches = [l for l in candidates if wanted in (l.name or "").lower() or wanted.replace(" ", "") in l.phone]
    if len(matches) != 1:
        names = ", ".join(sorted({l.name or l.phone for l in candidates})[:8]) or "ninguna"
        reply(f"No he encontrado a una sola persona con ese nombre. Conversaciones en pausa: {names}")
        return "ambiguous"
    lead = matches[0]
    lead.human_owned = False
    lead.status = "in_conversation"
    lead.introduced = True  # the lead already met the assistant or the agent
    log_event(s, "ai_resumed", account.id, lead.id)
    jobs.schedule(s, "ai_turn", utcnow(), lead_id=lead.id, account_id=account.id, dedupe_key=f"ai_turn:{lead.id}",
                  reuse=True, replace_run_at=True, payload={"resumed": True})
    reply(f"Hecho, sigo yo con {lead.name or lead.phone}.")
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
        send_to_lead(s, account, lead, kind="opt_out_confirmation", text=T.OPT_OUT_CONFIRMATION)
    except WhatsAppError:
        pass


def do_handoff(s, account: Account, lead: Lead, reason: str, *, notify_lead: bool = True) -> None:
    lead.status = "handoff"
    lead.human_owned = True
    jobs.cancel(s, lead.id, ["ai_turn", "followup", "mark_no_reply", "first_contact"])
    log_event(s, "handoff", account.id, lead.id, reason=reason)
    if notify_lead:
        try:
            send_to_lead(s, account, lead, kind="handoff_notice",
                         text=T.HANDOFF_TO_LEAD.format(agent_first_name=first_name(account.agent_name) or "el agente"))
        except WhatsAppError:
            pass
    last_in = s.execute(select(Message.text).where(Message.lead_id == lead.id, Message.direction == "in")
                        .order_by(Message.id.desc())).first()
    why = _clip(reason, 80) + (f' ("{_clip(last_in[0], 80)}")' if last_in else "")
    alert_agent(s, account, T.AGENT_NEEDS_YOU, [first_name(account.agent_name) or "hola", lead_summary(lead), why])


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
                            out.append({"slot_id": t.strftime("%Y-%m-%dT%H:%M"), "label": when_label(su, account.timezone)})
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
    f = lead.facts or {}
    lines = [f"📞 {lead.phone} · WhatsApp: wa.me/{lead.phone.lstrip('+')}"]
    prop = ", ".join(p for p in [lead.property_type, lead.zone] if p)
    if lead.price:
        prop += f" · {lead.price:,.0f} €".replace(",", ".")
    if lead.listing_url:
        prop += f" (anuncio: {lead.listing_url})"
    if prop:
        lines.append(f"🏠 {prop}")
    if f.get("condition"):
        lines.append(f"🔧 Estado: {f['condition']}")
    motive = " · ".join(x for x in [f"Motivo: {f['reason']}" if f.get("reason") else "", f"Plazo: {f['timeline']}" if f.get("timeline") else ""] if x)
    if motive:
        lines.append(f"🗣 {motive}")
    if f.get("occupancy"):
        lines.append(f"🔑 Ocupación: {f['occupancy']}")
    if f.get("decision_maker"):
        lines.append(f"👥 Decide: {f['decision_maker']}")
    if f.get("other_agency"):
        lines.append(f"🏢 ¿Otra agencia?: {f['other_agency']}")
    if f.get("notes"):
        lines.append(f"💬 {f['notes']}")
    lines.append(f"Tipo: {'visita' if meeting_type == 'visit' else 'llamada'} · Origen: {lead.source}")
    lines.append("— Agendado por Sparrow AI")
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
            return {"ok": False, "error": "already_booked", "when": when_label(existing.start, account.timezone)}
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
        where = lead.address if meeting_type == "visit" else f"Llamada · {lead.phone}"
        prop = " ".join(p for p in [lead.property_type, lead.zone] if p)
        summary = f"Valoración · {_short_name(lead)}" + (f" · {prop}" if prop else "") + (" · TEST" if lead.is_test else "")
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
    return {"ok": True, "when": when_label(start, account.timezone), "where": where}


# ---------------------------------------------------------------- the AI turn

def build_messages(s, account: Account, lead: Lead) -> list[dict]:
    rows = s.execute(select(Message).where(Message.lead_id == lead.id, Message.status != "failed")
                     .order_by(Message.id.desc()).limit(40)).scalars().all()[::-1]
    agent_first = first_name(account.agent_name) or "el agente"
    msgs: list[dict] = []
    for m in rows:
        if m.direction == "in":
            role, text = "user", m.text
        elif m.sender == "agent":
            role, text = "assistant", f"[Mensaje escrito en persona por {agent_first}, no por ti]: {m.text}"
        else:
            role, text = "assistant", m.text
        if not text:
            continue
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n" + text
        else:
            msgs.append({"role": role, "content": text})
    if msgs and msgs[0]["role"] == "assistant":
        msgs.insert(0, {"role": "user", "content": "[Inicio de la conversación]"})
    return msgs


@jobs.handler("ai_turn")
def job_ai_turn(s, job):
    account, lead = _load(s, job)
    run_ai_turn(s, account, lead)


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
            facts.update({k: v for k, v in args.items() if v})
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
        do_handoff(s, account, lead, "error técnico del asistente")
        return "error_handoff"
    turn.latency_ms = int((time.time() - t0) * 1000)

    s.flush()
    s.refresh(lead)
    if lead.human_owned and state["terminal"] != "handoff":
        return "agent_took_over"  # G8: echo arrived while the model was thinking
    if lead.last_inbound_at and lead.last_inbound_at > started and state["terminal"] is None:
        return "newer_message"  # the re-armed job will answer everything together

    if state["terminal"] == "handoff":
        do_handoff(s, account, lead, state["reason"] or "el asistente no está seguro")
        return "handoff"
    if state["terminal"] == "stop":
        if state["reason"] == "opted_out":
            opt_out(s, account, lead, source="llm")
            return "opted_out"
        lead.status = "not_interested"
        jobs.cancel(s, lead.id, ["followup", "mark_no_reply"])
        log_event(s, "not_interested", account.id, lead.id)
        try:
            send_to_lead(s, account, lead, kind="ai_reply",
                         text=reply or "Entendido, gracias por responder. Si más adelante quieres una valoración, escríbeme por aquí.")
        except WhatsAppError:
            pass
        return "not_interested"

    reply = (reply or "").strip()
    if not reply:
        do_handoff(s, account, lead, "el asistente no generó respuesta")
        return "empty_handoff"
    agent_first = first_name(account.agent_name) or "tu agente"
    if not lead.introduced and "asistente virtual" not in reply.lower():  # G13
        reply = f"Soy el asistente virtual de {agent_first}. " + reply
    parts = [p.strip() for p in re.split(r"\n\s*\n", reply) if p.strip()][:2]
    try:
        for p in parts:
            send_to_lead(s, account, lead, kind="ai_reply", text=p)
            lead.introduced = True
    except WhatsAppError as e:
        do_handoff(s, account, lead, f"no se pudo enviar la respuesta ({e})", notify_lead=False)
        return "send_failed_handoff"
    return "replied"
