"""Public web routes: onboarding chat, Google OAuth, Meta signup callback, lead intake, legal pages."""
import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import select

from . import gcal, onboarding
from .config import get_settings
from .crypto import encrypt
from .db import session_scope
from .engine import ingest_lead, log_event
from .guards import normalize_phone
from .intake import from_lystos, normalize
from .models import Account

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


def _state(s, acc: Account) -> dict:
    return {
        "step": acc.onboarding_step,
        "status": acc.status,
        "answers": {
            "agent_name": acc.agent_name,
            "agency": acc.agency,
            "city": acc.city,
            "zones": acc.zones,
            "email": acc.email,
            "personal_whatsapp": acc.personal_whatsapp,
            "extra_context": acc.extra_context,
        },
        "integrations": onboarding.integrations_status(s, acc),
    }


# ---------------------------------------------------------------- pages

@router.get("/", response_class=HTMLResponse)
def landing(request: Request):
    return templates.TemplateResponse(request, "landing.html", {})


@router.post("/start")
def start():
    with session_scope() as s:
        acc = Account()
        s.add(acc)
        s.flush()
        log_event(s, "onboarding_started", acc.id)
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
        for f in ("agent_name", "agency", "city", "zones", "email", "extra_context"):
            if f in data:
                setattr(acc, f, str(data[f]).strip()[:500])
        if "personal_whatsapp" in data:
            p = normalize_phone(data["personal_whatsapp"])
            if not p:
                return JSONResponse({"ok": False, "error": "Ese número no parece válido. Prueba con el formato 600 000 000."}, 400)
            acc.personal_whatsapp = p
        if acc.onboarding_step == "questions" and all(getattr(acc, f) for f in onboarding.REQUIRED_ANSWERS):
            onboarding.advance(acc, "google")
        return {"ok": True, **_state(s, acc)}


@router.post("/api/o/{token}/step")
async def api_step(token: str, request: Request):
    data = await request.json()
    with session_scope() as s:
        acc = _account_by_token(s, token)
        step = data.get("done")
        if step == "lystos":
            key = (data.get("lystos_key") or "").strip()
            onboarding.upsert_integration(s, acc.id, "lystos", external={"pending_call": not key}, token_enc=encrypt(key) if key else "",
                                          status="active" if key else "pending")
            onboarding.advance(acc, "wa_check")
        elif step == "wa_check":
            onboarding.advance(acc, "whatsapp")
        elif step == "skip_google":  # allowed only to keep going; Google is required before going live
            onboarding.advance(acc, "lystos")
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


@router.post("/api/o/{token}/test")
async def api_test(token: str, request: Request):
    """Optional: send a test conversation to a friend's phone."""
    data = await request.json()
    with session_scope() as s:
        acc = _account_by_token(s, token)
        integ = onboarding.integrations_status(s, acc).get("meta", {})
        if acc.status != "live" or integ.get("status") != "active":
            return JSONResponse({"ok": False, "error": "Tu asistente aún no está activo."}, 400)
        phone = normalize_phone(data.get("phone"))
        if not phone:
            return JSONResponse({"ok": False, "error": "Número no válido."}, 400)
        lead, result = ingest_lead(s, acc, {"name": data.get("name") or "Prueba", "phone": phone, "zone": (acc.zones or "").split(",")[0],
                                            "property_type": "piso", "source": "prueba"}, is_test=True)
        return {"ok": True, "result": result}


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

@router.get("/privacidad", response_class=HTMLResponse)
def privacy(request: Request):
    return templates.TemplateResponse(request, "legal.html", {"page": "privacidad", "contact_email": get_settings().contact_email})


@router.get("/terminos", response_class=HTMLResponse)
def terms(request: Request):
    return templates.TemplateResponse(request, "legal.html", {"page": "terminos", "contact_email": get_settings().contact_email})


@router.get("/eliminar-datos", response_class=HTMLResponse)
def data_deletion(request: Request):
    return templates.TemplateResponse(request, "legal.html", {"page": "eliminar", "contact_email": get_settings().contact_email})


@router.get("/health")
def health():
    return {"ok": True}
