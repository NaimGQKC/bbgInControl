"""Internal ops console (/admin). Also what you record for Meta App Review:
video 1 = send a message from here and show it arriving in WhatsApp; video 2 = create a template from here."""
import secrets
from collections import Counter

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import func, select

from . import jobs as jobmod, onboarding, templates as T
from .config import get_settings
from .db import session_scope, utcnow
from .engine import agent_wa, do_handoff, ingest_lead, log_event, sparrow_wa
from .guards import normalize_phone
from .models import Account, Event, Job, Lead, LlmTurn, Meeting, Message
from .web import templates as jinja
from .whatsapp import WhatsAppError

security = HTTPBasic()


def auth(creds: HTTPBasicCredentials = Depends(security)):
    st = get_settings()
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
        return jinja.TemplateResponse(request, "admin_dashboard.html", {
            "accounts": accounts, "leads": leads, "funnel": [(n, counts.get(n, 0)) for n in FUNNEL],
            "meetings": meetings, "failed_jobs": failed_jobs, "msg": msg, "base_url": get_settings().base_url,
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
            ok = sum(1 for r in res if r["ok"])
            errs = "; ".join(f"{r['name']}: {r['error']}" for r in res if not r["ok"])[:300]
            return _back(url, f"Plantillas enviadas a Meta: {ok}/{len(res)} {errs}")
        if action == "go_live":
            acc.status = "live"
            log_event(s, "account_live", acc.id, manual=True)
            return _back(url, "Cuenta activa")
        if action == "pause":
            acc.status = "paused"
            return _back(url, "Cuenta en pausa")
        return _back(url)


@router.post("/accounts/{account_id}/test_lead")
def test_lead(account_id: int, name: str = Form(...), phone: str = Form(...), zone: str = Form(""), property_type: str = Form("piso"),
              test: str = Form("")):  # an unchecked checkbox sends nothing, so "" must mean "not a test"
    with session_scope() as s:
        acc = s.get(Account, account_id)
        lead, result = ingest_lead(s, acc, {"name": name, "phone": phone, "zone": zone, "property_type": property_type, "source": "admin"},
                                   is_test=bool(test))
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
        try:
            wa.create_template(tpl)
            res.append(f"{tpl.name}: ok")
        except WhatsAppError as e:
            res.append(f"{tpl.name}: {e}")
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
