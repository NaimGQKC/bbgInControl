"""Meta webhook receiver: verify signature, store the raw event once, answer fast, process after."""
import hashlib
import hmac
import json
import logging
from datetime import timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select

from . import engine, onboarding
from .config import get_settings
from .db import session_scope, utcnow
from .models import Account, Event, Integration, WebhookEvent

log = logging.getLogger("sparrow.webhooks")
router = APIRouter()


@router.get("/webhooks/meta")
def verify(request: Request):
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == get_settings().meta_verify_token:
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(403, "bad verify token")


def _valid_signature(body: bytes, header: str | None) -> bool:
    secret = get_settings().meta_app_secret
    if not secret:
        return get_settings().whatsapp_mode == "fake"  # only skip the check in fake/dev mode
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[7:])


@router.post("/webhooks/meta")
async def receive(request: Request, background: BackgroundTasks):
    body = await request.body()
    if not _valid_signature(body, request.headers.get("x-hub-signature-256")):
        raise HTTPException(401, "bad signature")
    payload = json.loads(body or b"{}")
    ext_id = hashlib.sha256(body).hexdigest()
    with session_scope() as s:
        if s.execute(select(WebhookEvent.id).where(WebhookEvent.external_id == ext_id)).first():
            return {"ok": True, "duplicate": True}  # G3: exact same delivery twice
        ev = WebhookEvent(provider="meta", external_id=ext_id, payload=payload)
        s.add(ev)
        s.flush()
        ev_id = ev.id
    background.add_task(process_event, ev_id)
    return {"ok": True}


def _valid_stripe_signature(body: bytes, header: str | None) -> bool:
    secret = get_settings().stripe_webhook_secret
    if not secret:
        return get_settings().whatsapp_mode == "fake"  # only skip the check in fake/dev mode
    pairs = [p.split("=", 1) for p in (header or "").split(",") if "=" in p]
    try:
        stamp = int(dict(pairs)["t"])
    except (KeyError, ValueError):
        return False
    if abs(int(utcnow().replace(tzinfo=timezone.utc).timestamp()) - stamp) > 300:
        return False  # replayed delivery
    expected = hmac.new(secret.encode(), f"{stamp}.".encode() + body, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, v) for k, v in pairs if k == "v1")


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    """Stripe tells us the €90 was paid; the agent's onboarding chat then moves on by itself."""
    body = await request.body()
    if not _valid_stripe_signature(body, request.headers.get("stripe-signature")):
        raise HTTPException(401, "bad signature")
    event = json.loads(body or b"{}")
    obj = (event.get("data") or {}).get("object") or {}
    if event.get("type") == "checkout.session.completed" and obj.get("payment_status") == "paid":
        with session_scope() as s:
            acc = s.execute(select(Account).where(Account.public_token == obj.get("client_reference_id"))).scalar_one_or_none()
            if acc:
                onboarding.mark_paid(s, acc, str(obj.get("id", "")))
            else:
                log.warning("stripe payment %s for an unknown onboarding link", obj.get("id"))
    return {"ok": True}


def _account_for_phone_number_id(s, phone_number_id: str) -> Account | None:
    for integ in s.execute(select(Integration).where(Integration.provider == "meta")).scalars():
        if (integ.external or {}).get("phone_number_id") == phone_number_id:
            return s.get(Account, integ.account_id)
    return None


def _msg_fields(m: dict) -> dict:
    t = m.get("type", "text")
    out = {"id": m.get("id"), "type": t, "text": ""}
    if t == "text":
        out["text"] = (m.get("text") or {}).get("body", "")
    elif t == "button":
        out["text"] = (m.get("button") or {}).get("text", "")
    elif t == "interactive":
        i = m.get("interactive") or {}
        out["text"] = (i.get("button_reply") or i.get("list_reply") or {}).get("title", "")
    elif t == "audio":
        out["audio_id"] = (m.get("audio") or {}).get("id")
    elif t in ("image", "video", "document"):
        out["text"] = (m.get(t) or {}).get("caption", "")
    return out


def process_event(event_id: int) -> None:
    with session_scope() as s:
        ev = s.get(WebhookEvent, event_id)
        if ev.processed_at:
            return
        try:
            _process(s, ev.payload)
            ev.processed_at = utcnow()
        except Exception as e:
            log.exception("webhook processing failed")
            s.rollback()
            ev = s.get(WebhookEvent, event_id)
            ev.error = repr(e)[:2000]


def sweep_unprocessed() -> int:
    """Worker safety net: events received but never processed (e.g. a deploy right after answering 200)."""
    with session_scope() as s:
        ids = [i for (i,) in s.execute(select(WebhookEvent.id).where(
            WebhookEvent.processed_at.is_(None), WebhookEvent.error == "",
            WebhookEvent.received_at < utcnow() - timedelta(seconds=60)).limit(20)).all()]
    for i in ids:
        process_event(i)
    return len(ids)


def _refresh_quality_for_waba(s, waba_id: str) -> None:
    from . import quality
    if not waba_id:
        return
    for integ in s.execute(select(Integration).where(Integration.provider == "meta")).scalars():
        if (integ.external or {}).get("waba_id") == waba_id:
            account = s.get(Account, integ.account_id)
            if account is not None:
                quality.refresh(s, account)


def _process(s, payload: dict) -> None:
    st = get_settings()
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            field, value = change.get("field"), change.get("value") or {}
            pnid = (value.get("metadata") or {}).get("phone_number_id", "")
            if field == "message_template_status_update":
                s.add(Event(name="template_status", data=value))
                continue
            if field == "phone_number_quality_update":  # Meta changed the number's quality/limit: re-read the rating now
                s.add(Event(name=f"webhook_{field}", data={"event": value.get("event"), "current_limit": value.get("current_limit")}))
                _refresh_quality_for_waba(s, str(entry.get("id") or ""))
                continue
            if field not in ("messages", "smb_message_echoes"):
                s.add(Event(name=f"webhook_{field}", data={"phone_number_id": pnid}))
                continue
            if pnid and pnid == st.sparrow_phone_number_id:
                for m in value.get("messages", []):
                    engine.handle_sparrow_inbound(s, m.get("from", ""), _msg_fields(m))
                continue
            account = _account_for_phone_number_id(s, pnid)
            if not account:
                log.warning("webhook for unknown phone_number_id %s", pnid)
                continue
            if field == "messages":
                for m in value.get("messages", []):
                    engine.handle_inbound(s, account, m.get("from", ""), _msg_fields(m))
                for stt in value.get("statuses", []):
                    err = ""
                    if stt.get("errors"):
                        e0 = stt["errors"][0]
                        err = f"{e0.get('code')}: {e0.get('title') or e0.get('message')}"
                    engine.handle_status(s, stt.get("id", ""), stt.get("status", ""), err)
            else:  # smb_message_echoes: the agent wrote from the WhatsApp Business app
                for m in value.get("message_echoes", []):
                    engine.handle_echo(s, account, m.get("to", ""), _msg_fields(m))
            s.flush()
