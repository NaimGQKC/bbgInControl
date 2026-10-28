"""Public web routes: onboarding chat, Google OAuth, Meta signup callback, lead intake, legal pages."""
import json
import logging
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import select

from . import gcal, onboarding, templates as T
from .gcal import CalendarError
from .config import get_settings
from .db import session_scope
from .engine import compute_free_slots, first_contact_values, ingest_lead, log_event
from .guards import normalize_phone
from .intake import from_lystos, normalize
from .models import Account, Integration

log = logging.getLogger("sparrow.web")
router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "web_templates"))


def _signer():
    return URLSafeSerializer(get_settings().session_secret, salt="oauth")


def _account_by_token(s, token: str) -> Account:
    acc = s.execute(select(Account).where(Account.public_token == token)).scalar_one_or_none()
    if not acc:
        raise HTTPException(404, "Enlace no válido")
    return acc


_GENERIC_MAIL = {"gmail", "googlemail", "hotmail", "outlook", "live", "msn", "yahoo", "icloud", "me", "proton", "protonmail", "telefonica", "movistar", "terra", "orange", "wanadoo"}


def _agency_hint(email: str) -> str:
    """A work email domain often names the agency (laura@remax-confort.es -> 'Remax Confort'). Only a suggestion, never saved."""
    dom = (email or "").rpartition("@")[2].lower()
    label = dom.split(".")[0] if dom else ""
    if not label or label in _GENERIC_MAIL:
        return ""
    return " ".join(w.capitalize() for w in label.replace("_", "-").split("-") if w)


def _state(s, acc: Account) -> dict:
    st = get_settings()
    onboarding.normalize_step(s, acc)
    integrations = onboarding.integrations_status(s, acc)
    link = st.stripe_payment_link
    pay_url = f"{link}{'&' if '?' in link else '?'}client_reference_id={acc.public_token}&prefilled_email={quote(acc.email)}" if link else ""
    return {
        "step": acc.onboarding_step,
        "status": acc.status,
        "assistant": st.assistant_name,
        "answers": {
            "agent_name": acc.agent_name,
            "agency": acc.agency,
            "zones": acc.zones,
            "email": acc.email,
            "personal_whatsapp": acc.personal_whatsapp,
            "extra_context": acc.extra_context,
        },
        "agency_hint": _agency_hint(acc.email),
        # what a new owner will receive from their number, with a made-up lead name
        "first_message": T.LEAD_FIRST_CONTACT.render(first_contact_values(acc, "Marta", "")) if acc.agent_name else "",
        "integrations": integrations,
        "payment": {"required": onboarding.payment_required(), "paid": integrations.get("stripe", {}).get("status") == "active",
                    "url": pay_url, "fee": st.integration_fee_eur, "per_meeting": st.meeting_fee_eur},
    }


# ---------------------------------------------------------------- pages

@router.get("/", response_class=HTMLResponse)
def landing(request: Request):
    return templates.TemplateResponse(request, "landing.html", {})


@router.post("/start")
async def start(request: Request):
    """Starts an onboarding. The website demo can post what the agent already told it (agency, zones, name, phone) so we never ask twice."""
    try:
        form = await request.form()
    except Exception:
        form = {}
    clean = lambda k, n=200: str(form.get(k, "") or "").strip()[:n]
    with session_scope() as s:
        acc = Account()
        s.add(acc)
        s.flush()
        acc.agency, acc.zones, acc.agent_name = clean("agency"), clean("zones", 500), clean("name")
        phone = normalize_phone(clean("phone", 30)) if clean("phone", 30) else None
        if phone:
            acc.personal_whatsapp = phone
        log_event(s, "onboarding_started", acc.id, prefilled=bool(acc.agency or acc.zones or acc.agent_name))
        token = acc.public_token
    return RedirectResponse(f"/o/{token}", status_code=303)


@router.get("/o/{token}", response_class=HTMLResponse)
def onboarding_page(request: Request, token: str):
    st = get_settings()
    with session_scope() as s:
        acc = _account_by_token(s, token)
        state = _state(s, acc)
    return templates.TemplateResponse(
        request,
        "onboarding.html",
        {
            "token": token,
            "state_json": json.dumps(state, ensure_ascii=False),
            "meta_app_id": st.meta_app_id,
            "meta_config_id": st.meta_config_id,
            "graph_version": st.graph_version,
            "fake": st.whatsapp_mode == "fake",
        },
    )


# ---------------------------------------------------------------- onboarding API

@router.get("/api/o/{token}")
def api_state(token: str):
    with session_scope() as s:
        return _state(s, _account_by_token(s, token))


@router.post("/api/o/{token}/answers")
async def api_answers(token: str, request: Request):
    data = await request.json()
    with session_scope() as s:
        acc = _account_by_token(s, token)
        for f in ("agent_name", "agency", "zones", "email", "extra_context"):
            if f in data:
                setattr(acc, f, str(data[f]).strip()[:500])
        if "personal_whatsapp" in data:
            p = normalize_phone(data["personal_whatsapp"])
            if not p:
                return JSONResponse({"ok": False, "error": "Ese número no parece válido. Prueba con el formato 600 000 000."}, 400)
            acc.personal_whatsapp = p
        if acc.onboarding_step == "questions" and all(getattr(acc, f) for f in onboarding.REQUIRED_ANSWERS):
            onboarding.advance(acc, onboarding.after_questions(s, acc))
        return {"ok": True, **_state(s, acc)}


@router.post("/api/o/{token}/step")
async def api_step(token: str, request: Request):
    data = await request.json()
    with session_scope() as s:
        acc = _account_by_token(s, token)
        if data.get("done") == "wa_check" and acc.onboarding_step == "wa_check":
            onboarding.advance(acc, "whatsapp")
        return {"ok": True, **_state(s, acc)}


@router.post("/api/o/{token}/meta")
async def api_meta(token: str, request: Request):
    data = await request.json()
    code, waba_id, pnid = data.get("code"), data.get("waba_id"), data.get("phone_number_id")
    if not (code and waba_id):
        return JSONResponse({"ok": False, "error": "Faltan datos de Meta. Vuelve a intentarlo."}, 400)
    with session_scope() as s:
        acc = _account_by_token(s, token)
        try:
            res = onboarding.connect_meta(s, acc, code=code, waba_id=waba_id, phone_number_id=pnid)
        except Exception as e:
            log.exception("meta connect failed")
            log_event(s, "meta_connect_failed", acc.id, error=str(e))
            return JSONResponse({"ok": False, "error": "No hemos podido terminar la conexión con WhatsApp. Vuelve a intentarlo."}, 500)
        return {**res, **_state(s, acc)}


@router.post("/api/o/{token}/meta_event")
async def api_meta_event(token: str, request: Request):
    data = await request.json()
    with session_scope() as s:
        acc = _account_by_token(s, token)
        log_event(s, "meta_signup_event", acc.id, event=str(data.get("event")), current_step=str(data.get("current_step", "")))
    return {"ok": True}


@router.post("/api/o/{token}/help_chat")
async def api_help_chat(token: str, request: Request):
    """AI help while connecting WhatsApp. Capped per agent; the screenshot is read once and never stored. Failure = hand over to a person."""
    from sqlalchemy import func
    from . import helpchat
    from .models import Event

    st = get_settings()
    data = await request.json()
    text = str(data.get("text") or "").strip()[: st.ai_max_user_chars]
    image = data.get("image")
    if image is not None and not (isinstance(image, str) and image.startswith(("data:image/jpeg;base64,", "data:image/png;base64,", "data:image/webp;base64,")) and len(image) <= 4_000_000):
        return JSONResponse({"ok": False, "error": "No he podido leer esa captura. Prueba con otra."}, 400)
    if not text and not image:
        return JSONResponse({"ok": False, "error": "Escribe algo o sube una captura."}, 400)
    history = [{"role": "assistant" if m.get("role") == "bot" else "user", "text": str(m.get("text", ""))[: st.ai_max_user_chars]} for m in (data.get("history") or [])[-6:] if isinstance(m, dict)]
    with session_scope() as s:
        acc = _account_by_token(s, token)
        used = s.execute(select(func.count()).select_from(Event).where(Event.account_id == acc.id, Event.name == "help_chat")).scalar_one()
        if used >= st.ai_max_help_turns:
            return {"ok": False, "capped": True}
        last = s.execute(select(Event).where(Event.account_id == acc.id, Event.name == "meta_signup_event").order_by(Event.id.desc())).scalars().first()
        context = f"paso del alta={acc.onboarding_step}" + (f"; Meta: evento={last.data.get('event')}, paso={last.data.get('current_step')}" if last else "")
        log_event(s, "help_chat", acc.id, chars=len(text), image=bool(image))
    reply = helpchat.answer(history, text, image, context)
    if not reply:
        return {"ok": False}
    return {"ok": True, "reply": reply}


@router.post("/api/o/{token}/calendar_check")
def api_calendar_check(token: str):
    """Right after Google connects: read the calendar once, so a missing permission shows up now and not at the first lead."""
    with session_scope() as s:
        acc = _account_by_token(s, token)
        try:
            labels = [x["label"] for x in compute_free_slots(s, acc, max_slots=4)]
        except CalendarError as e:
            log_event(s, "calendar_check_failed", acc.id, error=str(e)[:300])
            return {"ok": False}
        log_event(s, "calendar_verified", acc.id, free_slots=len(labels))
        days, picks = set(), []
        for label in labels:  # two different days read better than two times on the same day
            if label.split(",")[0] not in days:
                days.add(label.split(",")[0])
                picks.append(label)
        return {"ok": True, "first": picks[:2]}


def _google_row(s, acc: Account):
    return s.execute(select(Integration).where(Integration.account_id == acc.id, Integration.provider == "google")).scalar_one_or_none()


@router.post("/api/o/{token}/test_event")
def api_test_event(token: str):
    """The 'wow' moment: a clearly labelled test valuation lands in their real calendar (their phone buzzes). They delete it with one tap."""
    from .engine import _slot_to_utc, settings_for
    with session_scope() as s:
        acc = _account_by_token(s, token)
        integ = _google_row(s, acc)
        if not integ or integ.status != "active":
            return {"ok": False}
        ext = dict(integ.external or {})
        try:
            cal = gcal.calendar_for_account(s, acc)
            if ext.get("test_event") and cal.get_event(ext["test_event"]["id"]):  # already created: don't duplicate
                return {"ok": True, "label": ext["test_event"]["label"]}
            slots = compute_free_slots(s, acc, max_slots=1)
            if not slots:
                return {"ok": False}
            start = _slot_to_utc(slots[0]["slot_id"], acc.timezone)
            from datetime import timedelta
            eid = cal.create_event(
                summary="[Prueba Sparrow] Valoración piso Marta",
                description="Esto es una prueba de Sparrow AI: así verás aquí tus valoraciones reales, con el teléfono y los datos del propietario.\nPuedes borrarla sin problema.",
                location="", start_utc=start, end_utc=start + timedelta(minutes=settings_for(acc)["meeting_minutes"]),
                timezone=acc.timezone, request_id=f"test-{acc.id}",
            )
        except CalendarError as e:
            log_event(s, "test_event_failed", acc.id, error=str(e)[:300])
            return {"ok": False}
        ext["test_event"] = {"id": eid, "label": slots[0]["label"]}
        integ.external = ext
        log_event(s, "test_event_created", acc.id)
        return {"ok": True, "label": slots[0]["label"]}


@router.post("/api/o/{token}/test_event/delete")
def api_test_event_delete(token: str):
    with session_scope() as s:
        acc = _account_by_token(s, token)
        integ = _google_row(s, acc)
        ext = dict(integ.external or {}) if integ else {}
        te = ext.pop("test_event", None)
        if te:
            try:
                gcal.calendar_for_account(s, acc).delete_event(te["id"])
            except CalendarError:
                pass
            integ.external = ext
            log_event(s, "test_event_deleted", acc.id)
        return {"ok": True}


@router.post("/api/o/{token}/help")
async def api_help(token: str, request: Request):
    """Talk to a person: tells you (the operator) who wants a call or a message, where they are stuck, and how to reach them."""
    data = await request.json()
    with session_scope() as s:
        acc = _account_by_token(s, token)
        phone = normalize_phone(data.get("phone")) if data.get("phone") else None
        if data.get("phone") and not phone:
            return JSONResponse({"ok": False, "error": "Ese número no parece válido."}, 400)
        contact = phone or acc.personal_whatsapp or acc.email
        if not contact:
            return JSONResponse({"ok": False, "error": "Déjame un móvil para poder escribirte."}, 400)
        onboarding.request_help(s, acc, message=str(data.get("message") or "").strip()[:300], contact=contact, kind=str(data.get("kind") or "chat"))
        return {"ok": True, "contact": contact}


# ---------------------------------------------------------------- Google OAuth

@router.get("/oauth/google/start")
def google_start(t: str):
    return RedirectResponse(gcal.auth_url(_signer().dumps({"t": t})))


@router.get("/oauth/google/callback")
def google_callback(state: str = "", code: str = "", error: str = ""):
    try:
        token = _signer().loads(state)["t"]
    except BadSignature:
        raise HTTPException(400, "state inválido")
    if error or not code:
        return RedirectResponse(f"/o/{token}?google=denied")
    data = gcal.exchange_code(code)
    with session_scope() as s:
        acc = _account_by_token(s, token)
        onboarding.save_google(s, acc, data)
    return RedirectResponse(f"/o/{token}?google=ok")


# ---------------------------------------------------------------- lead intake

@router.post("/intake/{intake_key}")
async def intake(intake_key: str, request: Request, source: str = "webhook"):
    """Push new leads here (Lystos, Zapier, forms). Body: one lead object or a list of them."""
    body = await request.json()
    if isinstance(body, dict):
        body = body.get("leads", [body])
    items = [i for i in body if isinstance(i, dict)] if isinstance(body, list) else []
    out = []
    with session_scope() as s:
        acc = s.execute(select(Account).where(Account.intake_key == intake_key)).scalar_one_or_none()
        if not acc:
            raise HTTPException(404, "unknown intake key")
        for item in items:
            data = from_lystos(item) if source == "lystos" else normalize(item, item.get("source") or source)
            lead, result = ingest_lead(s, acc, data)
            out.append({"lead_id": lead.id if lead else None, "result": result})
    return {"ok": True, "results": out}


# ---------------------------------------------------------------- legal (Meta and Google require these URLs)

def _legal(request: Request, page: str):
    st = get_settings()
    return templates.TemplateResponse(request, "legal.html", {
        "page": page, "contact_email": st.contact_email, "legal_entity": st.legal_entity, "legal_nif": st.legal_nif,
        "legal_address": st.legal_address, "integration_fee_eur": st.integration_fee_eur, "meeting_fee_eur": st.meeting_fee_eur,
    })


@router.get("/privacidad", response_class=HTMLResponse)
def privacy(request: Request):
    return _legal(request, "privacidad")


@router.get("/terminos", response_class=HTMLResponse)
def terms(request: Request):
    return _legal(request, "terminos")


@router.get("/eliminar-datos", response_class=HTMLResponse)
def data_deletion(request: Request):
    return _legal(request, "eliminar")


@router.get("/health")
def health():
    return {"ok": True}
