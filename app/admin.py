"""Internal ops console (/admin). Also what you record for Meta App Review:
video 1 = send a message from here and show it arriving in WhatsApp; video 2 = create a template from here."""
import html
from datetime import timedelta
import secrets
from collections import Counter

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import func, select

from . import jobs as jobmod, onboarding, prospecting, templates as T
from .config import get_settings
from .db import session_scope, utcnow
from .engine import agent_wa, do_handoff, ingest_lead, log_event, sparrow_wa
from .guards import normalize_phone
from .models import Account, Event, Integration, Job, Lead, LlmTurn, Meeting, Message
from .web import templates as jinja
from .whatsapp import WhatsAppError

security = HTTPBasic()


def auth(creds: HTTPBasicCredentials = Depends(security)):
    st = get_settings()
    if st.whatsapp_mode == "live" and st.admin_password in ("", "change-me"):
        raise HTTPException(503, "Set ADMIN_PASSWORD (and ADMIN_USER) in the environment before using the admin console.")
    ok = secrets.compare_digest(creds.username, st.admin_user) and secrets.compare_digest(creds.password, st.admin_password)
    if not ok:
        raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})
    return creds.username


router = APIRouter(prefix="/admin", dependencies=[Depends(auth)])

FUNNEL = ["lead_received", "first_message_sent", "replied", "slots_offered", "meeting_booked", "handoff", "opted_out"]


def _back(url: str, msg: str = "") -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    return RedirectResponse(f"{url}{sep}msg={msg}" if msg else url, status_code=303)


@router.get("", response_class=HTMLResponse)
def dashboard(request: Request, msg: str = ""):
    with session_scope() as s:
        accounts = s.execute(select(Account).order_by(Account.id.desc())).scalars().all()
        leads = s.execute(select(Lead).order_by(Lead.updated_at.desc()).limit(50)).scalars().all()
        counts = Counter(dict(s.execute(select(Event.name, func.count()).group_by(Event.name)).all()))
        meetings = s.execute(select(Meeting).order_by(Meeting.start.desc()).limit(20)).scalars().all()
        failed_jobs = s.execute(select(Job).where(Job.status == "failed").order_by(Job.id.desc()).limit(10)).scalars().all()
        help_requests = s.execute(select(Event).where(Event.name == "onboarding_help_requested").order_by(Event.id.desc()).limit(10)).scalars().all()
        return jinja.TemplateResponse(request, "admin_dashboard.html", {
            "accounts": accounts, "leads": leads, "funnel": [(n, counts.get(n, 0)) for n in FUNNEL],
            "meetings": meetings, "failed_jobs": failed_jobs, "help_requests": help_requests, "msg": msg, "base_url": get_settings().base_url,
        })


@router.post("/accounts")
def create_account(agent_name: str = Form(...), agency: str = Form(""), zones: str = Form(""), personal_whatsapp: str = Form("")):
    with session_scope() as s:
        acc = Account(agent_name=agent_name, agency=agency, zones=zones, personal_whatsapp=normalize_phone(personal_whatsapp) or "")
        s.add(acc)
        s.flush()
        return _back(f"/admin/accounts/{acc.id}", "Cuenta creada")


@router.get("/accounts/{account_id}", response_class=HTMLResponse)
def account_page(request: Request, account_id: int, msg: str = ""):
    with session_scope() as s:
        acc = s.get(Account, account_id)
        if not acc:
            raise HTTPException(404)
        leads = s.execute(select(Lead).where(Lead.account_id == acc.id).order_by(Lead.updated_at.desc())).scalars().all()
        integ = onboarding.integrations_status(s, acc)
        tpl_status, tpl_error = [], ""
        if "meta" in integ:
            try:
                tpl_status = agent_wa(s, acc).list_templates()
            except Exception as e:  # the console must still open if Meta is down
                tpl_error = str(e)
        return jinja.TemplateResponse(request, "admin_account.html", {
            "a": acc, "leads": leads, "integ": integ, "tpl_status": tpl_status, "tpl_error": tpl_error, "msg": msg,
            "base_url": get_settings().base_url, "agent_templates": T.AGENT_TEMPLATES,
            "prospecting": prospecting_line(s, acc),
        })


@router.post("/accounts/{account_id}/action")
def account_action(account_id: int, action: str = Form(...)):
    with session_scope() as s:
        acc = s.get(Account, account_id)
        url = f"/admin/accounts/{account_id}"
        if action == "link_test_number":
            onboarding.link_test_number(s, acc)
            return _back(url, "Número de prueba vinculado")
        if action == "submit_templates":
            try:
                res = onboarding.submit_templates(s, acc, agent_wa(s, acc))
            except WhatsAppError as e:
                return _back(url, f"Error: {e}")
            jobmod.schedule(s, "check_templates", utcnow() + timedelta(minutes=1), account_id=acc.id, dedupe_key=f"check_templates:{acc.id}", reuse=True)
            ok = sum(1 for r in res if r["ok"])
            errs = "; ".join(f"{r['name']}: {r['error']}" for r in res if not r["ok"])[:300]
            return _back(url, f"Plantillas enviadas a Meta: {ok}/{len(res)} {errs}")
        if action == "go_live":
            acc.status = "live"
            log_event(s, "account_live", acc.id, manual=True)
            prospecting.activate_if_requested(s, acc)
            from .engine import rearm_queued_leads
            rearm_queued_leads(s, acc)
            return _back(url, "Cuenta activa")
        if action == "pause":
            acc.status = "paused"
            return _back(url, "Cuenta en pausa")
        return _back(url)


@router.post("/accounts/{account_id}/check_payment")
def check_payment(account_id: int):
    """Asks Meta now whether this agent's WhatsApp Business account has a payment method."""
    with session_scope() as s:
        acc = s.get(Account, account_id)
        if acc is None:
            raise HTTPException(404)
        state = onboarding.check_meta_payment(s, acc)
        detail = ((s.execute(select(Integration).where(Integration.account_id == account_id, Integration.provider == "meta")).scalar_one_or_none() or Integration()).external or {}).get("payment_detail", "")
    return _back(f"/admin/accounts/{account_id}", {"ok": "Pago de Meta: OK", "missing": "Pago de Meta: FALTA el método de pago",
                                                   "unknown": "Pago de Meta: no se pudo comprobar" + (f" ({detail})" if detail else "")}[state])


@router.post("/accounts/{account_id}/test_lead")
def test_lead(account_id: int, name: str = Form(...), phone: str = Form(...), zone: str = Form(""), property_type: str = Form("piso"),
              test: str = Form(""), any_hour: str = Form("")):  # an unchecked checkbox sends nothing, so "" must mean "not a test"
    with session_scope() as s:
        acc = s.get(Account, account_id)
        if test:  # rehearsals must be repeatable: the old test lead with this number (and its chat, jobs, meeting) goes first
            p = normalize_phone(phone)
            old = s.execute(select(Lead).where(Lead.account_id == acc.id, Lead.phone == p, Lead.is_test.is_(True))).scalar_one_or_none() if p else None
            if old is not None:
                prospecting.delete_lead(s, old)
        lead, result = ingest_lead(s, acc, {"name": name, "phone": phone, "zone": zone, "property_type": property_type, "source": "admin"},
                                   is_test=bool(test), any_hour=bool(test and any_hour))
        return _back(f"/admin/accounts/{account_id}", f"Lead: {result}")


@router.post("/accounts/{account_id}/send")
def send_message(account_id: int, to: str = Form(...), kind: str = Form("hello_world"), text: str = Form("")):
    """App Review video 1: a message sent from our app, received in WhatsApp."""
    with session_scope() as s:
        acc = s.get(Account, account_id)
        phone = normalize_phone(to)
        if not phone:
            return _back(f"/admin/accounts/{account_id}", "Número no válido")
        if s.execute(select(Lead.id).where(Lead.account_id == acc.id, Lead.phone == phone)).first():
            # Leads are only ever messaged through send_to_lead (opt-out, hours, window checks)
            return _back(f"/admin/accounts/{account_id}", "Ese número es un lead: usa el flujo normal, no este envío manual")
        wa = agent_wa(s, acc)
        try:
            if kind == "hello_world":
                wid = wa.send_raw_template(phone, "hello_world", "en_US")
                body = "hello_world"
            else:
                wid = wa.send_text(phone, text)
                body = text
        except WhatsAppError as e:
            return _back(f"/admin/accounts/{account_id}", f"Error de WhatsApp: {e}")
        s.add(Message(account_id=acc.id, direction="out", sender="system", type="text" if kind != "hello_world" else "template",
                      text=body, wa_message_id=wid, status="sent"))
        return _back(f"/admin/accounts/{account_id}", f"Enviado ({wid})")


@router.post("/sparrow/templates")
def sparrow_templates():
    """Submit the Sparrow-number templates (needs-you alerts) to the Sparrow WABA."""
    st = get_settings()
    wa = sparrow_wa()
    if not wa or not st.sparrow_waba_id:
        return _back("/admin", "Configura SPARROW_PHONE_NUMBER_ID, SPARROW_WA_TOKEN y SPARROW_WABA_ID")
    wa.waba_id = st.sparrow_waba_id
    res = []
    for tpl in T.SPARROW_TEMPLATES:
        for lang in (("es",) if tpl is T.OPS_NEEDS_HELP else ("es", "fr", "en")):  # agents get their alerts in their own language
            try:
                wa.create_template(tpl, language=lang)
                res.append(f"{tpl.name}/{lang}: ok")
            except WhatsAppError as e:
                res.append(f"{tpl.name}/{lang}: {e}")
    return _back("/admin", "; ".join(res)[:400])


@router.get("/leads/{lead_id}", response_class=HTMLResponse)
def lead_page(request: Request, lead_id: int, msg: str = ""):
    with session_scope() as s:
        lead = s.get(Lead, lead_id)
        if not lead:
            raise HTTPException(404)
        acc = s.get(Account, lead.account_id)
        msgs = s.execute(select(Message).where(Message.lead_id == lead.id).order_by(Message.id)).scalars().all()
        meeting = s.execute(select(Meeting).where(Meeting.lead_id == lead.id)).scalar_one_or_none()
        turns = s.execute(select(LlmTurn).where(LlmTurn.lead_id == lead.id).order_by(LlmTurn.id.desc()).limit(5)).scalars().all()
        jobs_ = s.execute(select(Job).where(Job.lead_id == lead.id).order_by(Job.id.desc())).scalars().all()
        events = s.execute(select(Event).where(Event.lead_id == lead.id).order_by(Event.id)).scalars().all()
        return jinja.TemplateResponse(request, "admin_lead.html", {
            "lead": lead, "a": acc, "msgs": msgs, "meeting": meeting, "turns": turns, "jobs": jobs_, "events": events, "msg": msg,
        })


@router.post("/leads/{lead_id}/action")
def lead_action(lead_id: int, action: str = Form(...)):
    with session_scope() as s:
        lead = s.get(Lead, lead_id)
        acc = s.get(Account, lead.account_id)
        url = f"/admin/leads/{lead_id}"
        if action == "pause_ai":
            lead.human_owned = True
            jobmod.cancel(s, lead.id, ["ai_turn", "followup", "first_contact", "mark_no_reply"])
            return _back(url, "IA en pausa para este lead")
        if action == "resume_ai":
            lead.human_owned = False
            if lead.status == "handoff":
                lead.status = "in_conversation"
            jobmod.schedule(s, "ai_turn", utcnow(), lead_id=lead.id, account_id=acc.id, dedupe_key=f"ai_turn:{lead.id}", reuse=True, replace_run_at=True)
            return _back(url, "IA reanudada")
        if action == "handoff":
            do_handoff(s, acc, lead, "manual desde admin", notify_lead=False)
            return _back(url, "Handoff hecho")
        if action == "do_not_contact":
            lead.do_not_contact = True
            lead.status = "opted_out"
            jobmod.cancel(s, lead.id, ["ai_turn", "followup", "first_contact", "mark_no_reply", "reminder"])
            log_event(s, "opted_out", acc.id, lead.id, source="admin")
            return _back(url, "Marcado como no contactar")
        return _back(url)


# ---------------------------------------------------------------- prospecting (one line per account)

def prospecting_line(s, acc: Account) -> dict:
    """What the console shows per account: on/off, zone, leads today, last run, why it is paused, template status."""
    cfg = prospecting.config_for(acc)
    last = s.execute(select(Event).where(Event.account_id == acc.id, Event.name == "prospect_run").order_by(Event.at.desc(), Event.id.desc()).limit(1)).scalar_one_or_none()
    d = (last.data or {}) if last else {}
    return {
        "account_id": acc.id,
        "name": acc.agent_name,
        "enabled": bool(cfg.get("enabled")),
        "zone": cfg.get("zone_name") or "",
        "leads_today": prospecting.created_today(s, acc),
        "daily_cap": prospecting.effective_cap(cfg),
        "last_run": {"at": last.at.isoformat(timespec="minutes"), "fetched": d.get("fetched", 0), "created": d.get("created", 0),
                     "searches": d.get("searches", 0), "error": d.get("error", "")} if last else None,
        "paused_reason": cfg.get("paused_reason") or "",
        "cold_template_status": cfg.get("cold_template_status") or "",
    }


@router.get("/billing", response_class=HTMLResponse)
def billing_page(month: str = ""):
    """What each agent owes for one month. The agent pays when the meeting is SCHEDULED: every meeting our assistant books in their
    calendar counts (test meetings left out, one per lead), even if they later delete or move the event. The other columns are
    information: whether the event is still in their calendar, and owners who asked to cancel in chat (a person decides about a credit)."""
    from datetime import datetime as _dt

    e = html.escape
    try:
        first = _dt.strptime(month, "%Y-%m")
    except ValueError:
        first = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last = (first.replace(year=first.year + (first.month == 12), month=first.month % 12 + 1))
    fee = get_settings().meeting_fee_eur
    rows, total = [], 0
    with session_scope() as s:
        changed_ids = {lid for (lid,) in s.execute(select(Event.lead_id).where(Event.name == "meeting_change_requested")).all()}
        for acc in s.execute(select(Account).order_by(Account.id)).scalars():
            ms = s.execute(select(Meeting).where(Meeting.account_id == acc.id, Meeting.is_test.is_(False),
                                                 Meeting.created_at >= first, Meeting.created_at < last)).scalars().all()
            if not ms:
                continue
            deleted = [m for m in ms if m.status == "cancelled"]
            review = [m for m in ms if m.lead_id in changed_ids]
            total += len(ms) * fee
            rows.append(f"<tr><td><a href='/admin/accounts/{acc.id}'>{e(acc.agent_name or str(acc.id))}</a></td><td><b>{len(ms)}</b></td>"
                        f"<td>{len(deleted)}</td><td>{len(review)}</td><td><b>{len(ms) * fee} €</b></td></tr>")
    head = "<tr><th>Agente</th><th>Reservadas (se cobran)</th><th>Evento borrado por el agente</th><th>Revisar: el lead pidió cancelar/cambiar</th><th>Importe</th></tr>"
    return HTMLResponse(f"<!doctype html><meta charset='utf-8'><title>Facturación · Sparrow Ops</title><p><a href='/admin'>← Ops</a></p>"
                        f"<h1>Facturación {first:%Y-%m}</h1><p>El agente paga cuando la cita se agenda: {fee} € por cada cita reservada en nuestros chats, "
                        f"aunque luego borre o mueva el evento. Citas de prueba excluidas. Mes por fecha de reserva. Otro mes: <code>?month=AAAA-MM</code></p>"
                        f"<table border='1' cellpadding='6' cellspacing='0'>{head}{''.join(rows) or '<tr><td colspan=5>Ninguna cita este mes</td></tr>'}</table><p><b>Total: {total} €</b></p>")


@router.get("/prospecting", response_class=HTMLResponse)
def prospecting_page(msg: str = ""):
    """Minimal console: one line per account that has prospecting set up, with pause / resume."""
    e = html.escape
    rows = []
    with session_scope() as s:
        for acc in s.execute(select(Account).order_by(Account.id.desc())).scalars():
            if not (acc.settings or {}).get("prospecting"):
                continue
            ln = prospecting_line(s, acc)
            lr = ln["last_run"]
            last = f"{lr['at']} · {lr['fetched']} vistos, {lr['created']} nuevos, {lr['searches']} búsquedas" + (f" · error {lr['error']}" if lr["error"] else "") if lr else "—"
            btn = ("pause", "Pausar") if ln["enabled"] else ("resume", "Reanudar")
            rows.append(
                f"<tr><td><a href='/admin/accounts/{ln['account_id']}'>{e(ln['name'] or str(ln['account_id']))}</a></td>"
                f"<td>{'ON' if ln['enabled'] else 'off'}</td><td>{e(ln['zone'])}</td><td>{ln['leads_today']}/{ln['daily_cap']}</td>"
                f"<td>{e(last)}</td><td>{e(ln['paused_reason'] or '—')}</td><td>{e(ln['cold_template_status'] or '—')}</td>"
                f"<td><form method='post' action='/admin/accounts/{ln['account_id']}/prospecting'><button name='action' value='{btn[0]}'>{btn[1]}</button></form></td></tr>")
    head = "<tr><th>Agente</th><th>Estado</th><th>Zona</th><th>Hoy</th><th>Última búsqueda</th><th>Pausa</th><th>Plantilla</th><th></th></tr>"
    flash = f"<p><b>{e(msg)}</b></p>" if msg else ""
    return HTMLResponse(f"<!doctype html><meta charset='utf-8'><title>Prospecting · Sparrow Ops</title><p><a href='/admin'>← Ops</a></p><h1>Prospecting</h1>{flash}"
                        f"<table border='1' cellpadding='6' cellspacing='0'>{head}{''.join(rows) or '<tr><td colspan=8>Ninguna cuenta</td></tr>'}</table>")


@router.post("/accounts/{account_id}/prospecting")
def prospecting_action(account_id: int, action: str = Form(...)):
    """pause: the operator stops it (the agent cannot turn it back on). resume: lifts any pause and restarts the ramp-up."""
    with session_scope() as s:
        acc = s.get(Account, account_id)
        if not acc:
            raise HTTPException(404)
        cfg = dict((acc.settings or {}).get("prospecting") or {})
        if action == "pause":
            prospecting.pause(s, acc, "operator", tell_agent=False)
            return _back("/admin/prospecting", "Prospecting en pausa")
        if action == "resume":
            if not cfg.get("location_id"):
                return _back("/admin/prospecting", "Falta la zona: no se puede reanudar")
            if not cfg.get("ack_at"):
                return _back("/admin/prospecting", "El agente no ha aceptado los riesgos: no se puede reanudar")
            cfg.update({"enabled": True, "paused_reason": "", "confirmed_at": utcnow().isoformat()})
            acc.settings = {**(acc.settings or {}), "prospecting": cfg}
            log_event(s, "prospect_resumed", acc.id, by="operator")
            return _back("/admin/prospecting", "Prospecting reanudado (vuelve a empezar con el límite bajo)")
        return _back("/admin/prospecting")
