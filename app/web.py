"""Public web routes: onboarding chat, Google OAuth, Meta signup callback, lead intake, legal pages."""
import json
import re
import logging
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from datetime import timedelta

from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import func, select

from . import casafari, coldmsg, gcal, i18n, i18n_onb, lang as L, onboarding, templates as T
from .gcal import CalendarError
from .config import get_settings
from .db import session_scope, utcnow
from .engine import compute_free_slots, first_contact_values, ingest_lead, log_event
from .guards import normalize_phone, tidy_name
from .intake import from_lystos, normalize
from .models import Account, Integration, Job

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
        "lead_lang": L.lead_lang(acc),  # the language the agent's owners (leads) are spoken to in
        "lead_lang_set": "lang" in (acc.settings or {}),
        "cc": L.cc_for(acc),
        # what a new owner will receive from their number, with a made-up lead name
        "first_message": T.LEAD_FIRST_CONTACT.render(first_contact_values(acc, {"es": "Marta", "fr": "Marie", "en": "Emma"}[L.lead_lang(acc)], ""), L.lead_lang(acc)) if acc.agent_name else "",
        "prospecting": _prospecting_state(acc) if _prospecting_offered(acc) else None,  # hidden until the feature is switched on (Spain only)
        "integrations": integrations,
        "meta_payment": (integrations.get("meta") or {}).get("payment_state", ""),  # ok | missing | unknown | "" (not checked yet)
        "payment": {"required": onboarding.payment_required(), "paid": integrations.get("stripe", {}).get("status") == "active",
                    "url": pay_url, "fee": st.integration_fee_eur, "per_meeting": st.meeting_fee_eur},
    }


# ---------------------------------------------------------------- pages

@router.get("/", response_class=HTMLResponse)
def landing(request: Request):
    lang = i18n.pick_lang(request)
    r = templates.TemplateResponse(request, "landing.html", {"lang": lang, "L": i18n.LANDING[lang]})
    i18n.remember(r, request, lang)
    return r


@router.post("/start")
async def start(request: Request):
    """Starts an onboarding. The website demo can post what the agent already told it (agency, zones, name, phone) so we never ask twice."""
    try:
        form = await request.form()
    except Exception:
        form = {}
    clean = lambda k, n=200: " ".join(str(form.get(k, "") or "").split())[:n]  # one line: these end up in WhatsApp template parameters
    with session_scope() as s:
        acc = Account()
        s.add(acc)
        s.flush()
        chosen = clean("lang", 5) or request.query_params.get("lang", "")
        if chosen in L.LANGS:
            L.set_lead_lang(acc, chosen)  # a French visitor most likely has French clients: confirmed with them in the chat
            acc.settings = {**(acc.settings or {}), "lang": chosen}
        acc.agency, acc.zones, acc.agent_name = clean("agency"), clean("zones", 500), tidy_name(clean("name"))
        phone = normalize_phone(clean("phone", 30), L.cc_for(acc)) if clean("phone", 30) else None
        if phone:
            acc.personal_whatsapp = phone
        log_event(s, "onboarding_started", acc.id, prefilled=bool(acc.agency or acc.zones or acc.agent_name))
        token = acc.public_token
    lang = request.query_params.get("lang", "")  # explicit choice only (the cookie keeps the rest)
    return RedirectResponse(f"/o/{token}" + (f"?lang={lang}" if lang in i18n.LANGS else ""), status_code=303)


@router.get("/o/{token}", response_class=HTMLResponse)
def onboarding_page(request: Request, token: str):
    st = get_settings()
    with session_scope() as s:
        acc = _account_by_token(s, token)
        state = _state(s, acc)
    lang = i18n.pick_lang(request)
    r = templates.TemplateResponse(
        request,
        "onboarding.html",
        {
            "lang": lang,
            "i18n": i18n_onb.strings(lang),
            "page_title": i18n.ONB_TITLE[lang],
            "token": token,
            "state_json": json.dumps(state, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"),
            "meta_app_id": st.meta_app_id,
            "meta_config_id": st.meta_config_id,
            "graph_version": st.graph_version,
            "fake": st.whatsapp_mode == "fake",
        },
    )
    i18n.remember(r, request, lang)
    r.headers["Cache-Control"] = "no-store"  # going Back after Google must not show a stale page
    return r


# ---------------------------------------------------------------- onboarding API

@router.get("/api/o/{token}")
def api_state(token: str):
    with session_scope() as s:
        return _state(s, _account_by_token(s, token))


@router.post("/api/o/{token}/answers")
async def api_answers(token: str, request: Request):
    try:
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError
    except ValueError:
        return JSONResponse({"ok": False, "error": "Petición no válida."}, 400)
    with session_scope() as s:
        acc = _account_by_token(s, token)
        for f in ("agent_name", "agency", "zones", "email", "extra_context"):
            if f in data:
                v = _nobrackets(str(data[f]).strip()[:500])
                if f != "extra_context":  # these go into WhatsApp template parameters: Meta rejects newlines and tabs there
                    v = " ".join(v.split())[: 120 if f == "agency" else 500]
                setattr(acc, f, tidy_name(v) if f == "agent_name" else v)
        if data.get("lead_lang") in L.LANGS and data["lead_lang"] != L.lead_lang(acc):
            onboarding.change_lead_lang(s, acc, data["lead_lang"])
        elif data.get("lead_lang") in L.LANGS:
            acc.settings = {**(acc.settings or {}), "lang": data["lead_lang"]}  # confirmed: remember that they chose it
        if "personal_whatsapp" in data:
            p = normalize_phone(data["personal_whatsapp"], L.cc_for(acc))
            if not p:
                return JSONResponse({"ok": False, "error": "Ese número no parece válido. Prueba con el formato 600 000 000."}, 400)
            acc.personal_whatsapp = p
        if acc.onboarding_step == "questions" and all(getattr(acc, f) for f in onboarding.REQUIRED_ANSWERS):
            onboarding.advance(acc, onboarding.after_questions(s, acc))
        return {"ok": True, **_state(s, acc)}


# ---------------------------------------------------------------- prospecting setup (the agent writes the cold first message)

PROSPECTING_TYPES = ("apartment", "house")
PLACE_KINDS = {"city", "district", "neighbourhood", "neighborhood", "province"}
PLACES_PER_HOUR = 20  # lookups per account per hour (CASAFARI's Free plan allows 10 distinct places a day)
PLACES_SEEN_MAX = 40
MAX_PRICE = 20_000_000
ACK_TEXT_VERSION = 1


def _nobrackets(text: str) -> str:
    """Names, agencies and zones never need < or >: dropping them closes the door on injected HTML/JS."""
    return str(text).replace("<", "").replace(">", "")


def _pros(acc: Account) -> dict:
    return dict((acc.settings or {}).get("prospecting") or {})


def _prospecting_offered(acc: Account) -> bool:
    return bool(get_settings().prospecting_enabled) and L.lead_lang(acc) == "es"


def _prospecting_state(acc: Account) -> dict:
    p, lang = _pros(acc), L.lead_lang(acc)
    return {
        "enabled": bool(p.get("enabled")),
        "zone_name": p.get("zone_name", ""),
        "location_id": p.get("location_id"),
        "property_types": p.get("property_types") or list(PROSPECTING_TYPES),
        "min_price": p.get("min_price"),
        "max_price": p.get("max_price"),
        "daily_cap": p.get("daily_cap", 5),
        "cold_body": p.get("cold_body") or coldmsg.default_body(lang),
        "success_pitch": p.get("success_pitch", ""),
        "risks": coldmsg.RISKS.get(lang, coldmsg.RISKS["es"]),
        "paused_reason": p.get("paused_reason", ""),
        "cold_template_status": p.get("cold_template_status", ""),
        "done": bool(p.get("decided_at") or p.get("confirmed_at")),  # the agent has answered this step (turned it on or said no)
    }


def _is_spain(loc: dict) -> bool:
    cc = str(loc.get("country_code") or loc.get("country") or "").upper()
    if cc:
        return cc == "ES"
    crumbs = loc.get("breadcrumbs") or []
    return bool(crumbs) and str(crumbs[0]).strip().lower() in ("españa", "spain")


def _place(loc: dict) -> dict | None:
    kind = str(loc.get("kind") or "").lower()
    if not _is_spain(loc) or (kind and kind not in PLACE_KINDS) or not isinstance(loc.get("id"), int) or isinstance(loc.get("id"), bool):
        return None
    crumbs = [str(c) for c in (loc.get("breadcrumbs") or [])][1:]  # drop the country
    return {"id": loc["id"], "name": str(loc.get("name") or "")[:120], "detail": ", ".join(crumbs) or kind}


@router.get("/api/o/{token}/places")
def api_places(token: str, q: str = ""):
    """Zone search for prospecting: Spanish cities, districts, neighbourhoods and provinces. Max 20 lookups per account per hour."""
    q = (q or "").strip()[:80]
    if len(q) < 3:
        return {"places": []}
    hour = f"{utcnow():%Y-%m-%dT%H}"
    with session_scope() as s:
        acc = _account_by_token(s, token)
        counter = dict((acc.settings or {}).get("places_calls") or {})
        n = counter.get("n", 0) if counter.get("hour") == hour else 0
        if n >= PLACES_PER_HOUR:
            return JSONResponse({"places": [], "error": "rate_limited"}, 429)
        acc.settings = {**(acc.settings or {}), "places_calls": {"hour": hour, "n": n + 1}}
        try:
            found = casafari.client().find_location(q)
        except casafari.CasafariError as e:
            log.warning("places lookup failed for account %s: %s", acc.id, e)
            return JSONResponse({"places": [], "error": "unavailable"}, 502)
        places = [p for p in (_place(x) for x in found) if p][:10]
        if places:  # remember what we offered: the save only accepts a location id that came from here
            seen = dict((acc.settings or {}).get("places_seen") or {})
            for p in places:
                seen.pop(str(p["id"]), None)
                seen[str(p["id"])] = p["name"]
            seen = dict(list(seen.items())[-PLACES_SEEN_MAX:])
            acc.settings = {**(acc.settings or {}), "places_seen": seen}
        return {"places": places}


def _privacy_url() -> str:
    return get_settings().base_url.rstrip("/") + "/privacidad"


@router.post("/api/o/{token}/prospecting/preview")
async def api_prospecting_preview(token: str, request: Request):
    """What an owner would read for the agent's text (made-up owner and flat), plus what is wrong and what is risky."""
    data = await request.json()
    body = str(data.get("cold_body") or "").strip()[:2000]
    with session_scope() as s:
        acc = _account_by_token(s, token)
        lang = L.lead_lang(acc)
        problems, warnings = coldmsg.validate(body)
        text = coldmsg.render(body or coldmsg.default_body(lang), lang, _privacy_url(), coldmsg.SAMPLE.get(lang, coldmsg.SAMPLE["es"]))
        return {"text": text, "problems": problems, "warnings": warnings}


def _int_or_none(v, name: str):
    if v in (None, ""):
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise ValueError(name)
    try:
        f = float(v)
    except ValueError:
        raise ValueError(name)
    if f != int(f):
        raise ValueError(name)
    return int(f)


def _bad(field: str, msg: str, **extra):
    return JSONResponse({"ok": False, "error": msg, "field": field, **extra}, 400)


@router.post("/api/o/{token}/prospecting")
async def api_prospecting(token: str, request: Request):
    """Save the prospecting setup. The agent decides everything here and accepts the risks; we validate hard and keep the proof."""
    data = await request.json()
    if not isinstance(data, dict):
        return _bad("body", "Datos no válidos.")
    with session_scope() as s:
        acc = _account_by_token(s, token)
        lang = L.lead_lang(acc)
        old = _pros(acc)
        want_on = data.get("enabled") is True

        # zone: the location id must be one we offered in `places` (or the one already saved)
        seen = (acc.settings or {}).get("places_seen") or {}
        try:
            loc = _int_or_none(data["location_id"], "location_id") if "location_id" in data else old.get("location_id")
        except ValueError:
            return _bad("location_id", "Zona no válida.")
        if loc is not None and str(loc) not in seen and loc != old.get("location_id"):
            return _bad("location_id", "Elige la zona de la lista.")
        zone = str(data.get("zone_name") or "").strip()[:80] if "zone_name" in data else old.get("zone_name", "")
        if loc is not None and not zone:
            zone = str(seen.get(str(loc)) or "")
        if want_on and (loc is None or not zone):
            return _bad("location_id", "Elige la zona donde quieres captar propietarios.")

        types = data.get("property_types", old.get("property_types") or list(PROSPECTING_TYPES))
        if not isinstance(types, list) or not types or not all(isinstance(x, str) and x in PROSPECTING_TYPES for x in types):
            return _bad("property_types", "Elige pisos, casas o ambos.")
        types = [x for x in PROSPECTING_TYPES if x in types]

        try:
            lo = _int_or_none(data["min_price"], "min_price") if "min_price" in data else old.get("min_price")
            hi = _int_or_none(data["max_price"], "max_price") if "max_price" in data else old.get("max_price")
        except ValueError as e:
            return _bad(str(e), "Precio no válido.")
        for name, v in (("min_price", lo), ("max_price", hi)):
            if v is not None and not 0 <= v <= MAX_PRICE:
                return _bad(name, "Precio no válido.")
        if lo is not None and hi is not None and lo > hi:
            return _bad("min_price", "El precio mínimo no puede ser mayor que el máximo.")

        try:
            cap = _int_or_none(data["daily_cap"], "daily_cap") if "daily_cap" in data else old.get("daily_cap")
        except ValueError:
            cap = None
        cap = max(1, min(20, cap if cap is not None else 5))

        body = _nobrackets(str(data["cold_body"]).strip()) if data.get("cold_body") else (old.get("cold_body") or coldmsg.default_body(lang))
        problems, warnings = coldmsg.validate(body)
        if problems:
            return _bad("cold_body", "El primer mensaje no es válido.", problems=problems, warnings=warnings)

        pitch = str(data.get("success_pitch") or "").strip() if "success_pitch" in data else old.get("success_pitch", "")
        if len(pitch) > 400:
            return _bad("success_pitch", "Esa frase es demasiado larga (máximo 400 caracteres).")

        if want_on and data.get("ack_risk") is not True:
            return _bad("ack_risk", "Tienes que aceptar los riesgos para activarlo.")

        now_iso = utcnow().isoformat()
        new = {**old, "location_id": loc, "zone_name": zone, "property_types": types, "min_price": lo, "max_price": hi,
               "daily_cap": cap, "cold_body": body, "success_pitch": pitch, "decided_at": old.get("decided_at") or now_iso}
        declined = not want_on and not old.get("cold_body")  # "ahora no": keep nothing about a message and never send a template to Meta
        if declined:
            new.pop("cold_body", None)
        elif body != old.get("cold_body"):
            new["cold_version"] = int(old.get("cold_version") or 0) + 1
            new["cold_template_status"] = "pending"  # another job submits it to Meta and updates this
        reason = None
        if want_on and old.get("paused_reason"):
            want_on, reason = False, "paused"  # a pause (quality, opt-outs, credit, operator) is lifted by the operator only
        elif want_on and acc.status != "live":
            want_on, reason = False, "not_live"  # not connected yet: saved, but off until the account is live
            new.update({"enable_when_live": True, "ack_at": now_iso, "ack_text_version": ACK_TEXT_VERSION})  # asked for + accepted; go-live turns it on
        elif want_on and not old.get("confirmed_at"):
            new.update({"confirmed_at": now_iso, "ack_at": now_iso, "ack_text_version": ACK_TEXT_VERSION})
        new["enabled"] = want_on
        acc.settings = {**(acc.settings or {}), "prospecting": new}
        if not declined and new.get("cold_template_status") == "pending":
            s.flush()
            onboarding.submit_cold_template(s, acc)  # no-op until WhatsApp is connected; connect submits it then
        log_event(s, "prospecting_saved", acc.id, enabled=want_on, reason=reason or "", cold_version=new.get("cold_version"))
        out = {"saved": True, "enabled": want_on}
        if reason:
            out["reason"] = reason
            if reason == "paused":
                out["paused_reason"] = old.get("paused_reason")
        return out


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


@router.post("/api/o/{token}/meta_payment/recheck")
async def api_meta_payment_recheck(token: str):
    """The agent says they added the payment method at Meta: ask Meta again."""
    with session_scope() as s:
        acc = _account_by_token(s, token)
        onboarding.check_meta_payment(s, acc)
        return {"ok": True, **_state(s, acc)}


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
        current = f"nombre={acc.agent_name!r}, inmobiliaria={acc.agency!r}, zonas={acc.zones!r}, cómo trabaja={acc.extra_context!r}, móvil={acc.personal_whatsapp!r}"
        log_event(s, "help_chat", acc.id, chars=len(text), image=bool(image))
    lang = data.get("lang") if data.get("lang") in i18n.LANGS else "es"
    out = helpchat.answer(history, text, image, context, current, lang)
    if not out or not out[0]:
        return {"ok": False}
    reply, proposal = out
    if proposal and proposal["field"] == "personal_whatsapp" and not normalize_phone(proposal["value"]):
        proposal = None
    return {"ok": True, "reply": reply, "proposal": proposal}


@router.post("/api/o/{token}/calendar_check")
def api_calendar_check(token: str):
    """Right after Google connects: read the calendar once, so a missing permission shows up now and not at the first lead."""
    with session_scope() as s:
        acc = _account_by_token(s, token)
        try:
            slots = compute_free_slots(s, acc, max_slots=4)
            labels = [x["label"] for x in slots]
        except CalendarError as e:
            log_event(s, "calendar_check_failed", acc.id, error=str(e)[:300])
            return {"ok": False}
        log_event(s, "calendar_verified", acc.id, free_slots=len(labels))
        days, picks, ids = set(), [], []
        for x in slots:  # two different days read better than two times on the same day
            day_key = re.split(r",| à ", x["label"])[0]  # "jueves 29 de octubre" / "jeudi 29 octobre" / "Thursday 29 October"
            if day_key not in days:
                days.add(day_key)
                picks.append(x["label"])
                ids.append(x["slot_id"])
        return {"ok": True, "first": picks[:2], "first_ids": ids[:2]}


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
                return {"ok": True, "label": ext["test_event"]["label"], "slot_id": ext["test_event"].get("slot_id", "")}
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
        ext["test_event"] = {"id": eid, "label": slots[0]["label"], "slot_id": slots[0]["slot_id"]}
        integ.external = ext
        log_event(s, "test_event_created", acc.id)
        return {"ok": True, "label": slots[0]["label"], "slot_id": slots[0]["slot_id"]}


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
        phone = normalize_phone(data.get("phone"), L.cc_for(acc)) if data.get("phone") else None
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
    lang = i18n.pick_lang(request)
    return templates.TemplateResponse(request, "legal.html", {
        "lang": lang, "page": page, "contact_email": st.contact_email, "legal_entity": st.legal_entity, "legal_nif": st.legal_nif,
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
    """Used by an external uptime check: 503 when the database does not answer or the queue is stuck (a due job waiting > 5 min)."""
    from sqlalchemy import text

    try:
        with session_scope() as s:
            s.execute(text("SELECT 1"))
            late = s.execute(select(func.count(Job.id)).where(Job.status == "pending", Job.run_at < utcnow() - timedelta(minutes=5))).scalar_one()
    except Exception:
        return JSONResponse({"ok": False, "error": "database"}, 503)
    if late:
        return JSONResponse({"ok": False, "error": "queue", "late_jobs": late}, 503)
    return {"ok": True}
