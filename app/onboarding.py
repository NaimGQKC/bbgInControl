"""Onboarding actions: Google Calendar, Meta Embedded Signup, templates, activation."""
import logging
from datetime import timedelta

from sqlalchemy import select

from . import jobs, templates as T
from .config import get_settings
from .crypto import encrypt
from .db import utcnow
from .engine import alert_agent, first_name, log_event
from .models import Account, Integration
from .whatsapp import (
    FakeWhatsApp,
    WhatsAppClient,
    WhatsAppError,
    exchange_code_for_token,
    get_phone_number,
    list_phone_numbers,
    subscribe_app_to_waba,
    sync_smb_app_data,
)

log = logging.getLogger("sparrow.onboarding")

REQUIRED_ANSWERS = ["agent_name", "agency", "zones", "personal_whatsapp"]
STEPS = ["questions", "google", "lystos", "wa_check", "whatsapp", "waiting_templates", "done"]


def upsert_integration(s, account_id: int, provider: str, **fields) -> Integration:
    integ = s.execute(select(Integration).where(Integration.account_id == account_id, Integration.provider == provider)).scalar_one_or_none()
    if not integ:
        integ = Integration(account_id=account_id, provider=provider)
        s.add(integ)
    for k, v in fields.items():
        setattr(integ, k, v)
    integ.status = fields.get("status", "active")
    s.flush()
    return integ


def integrations_status(s, account: Account) -> dict:
    rows = s.execute(select(Integration).where(Integration.account_id == account.id)).scalars().all()
    return {r.provider: {"status": r.status, **{k: v for k, v in (r.external or {}).items() if k != "token"}} for r in rows}


def advance(account: Account, to: str | None = None) -> None:
    if to:
        account.onboarding_step = to
        return
    i = STEPS.index(account.onboarding_step) if account.onboarding_step in STEPS else 0
    account.onboarding_step = STEPS[min(i + 1, len(STEPS) - 1)]


def save_google(s, account: Account, token_data: dict) -> None:
    upsert_integration(
        s,
        account.id,
        "google",
        external={"calendar_id": "primary", "scope": token_data.get("scope", "")},
        token_enc=encrypt(token_data["access_token"]),
        refresh_token_enc=encrypt(token_data.get("refresh_token", "")) if token_data.get("refresh_token") else "",
        expires_at=utcnow() + timedelta(seconds=int(token_data.get("expires_in", 3600))),
    )
    log_event(s, "google_connected", account.id)
    if account.onboarding_step in ("questions", "google"):
        advance(account, "lystos")


def submit_templates(s, account: Account, wa: WhatsAppClient) -> list[dict]:
    results = []
    for tpl in T.AGENT_TEMPLATES:
        try:
            r = wa.create_template(tpl)
            results.append({"name": tpl.name, "ok": True, "status": r.get("status", "")})
        except WhatsAppError as e:
            # "already exists" is fine when re-running
            results.append({"name": tpl.name, "ok": False, "error": str(e)})
    log_event(s, "templates_submitted", account.id, results=results)
    return results


def _pick_phone_number(numbers: list[dict], account: Account) -> str:
    """Coexistence signup only tells us the WABA: find its phone number (prefer the agent's own)."""
    if not numbers:
        raise WhatsAppError("No phone number found in the WhatsApp account")
    mine = "".join(ch for ch in (account.personal_whatsapp or "") if ch.isdigit())
    for n in numbers:
        digits = "".join(ch for ch in n.get("display_phone_number", "") if ch.isdigit())
        if mine and digits and (digits.endswith(mine[-9:]) or mine.endswith(digits[-9:])):
            return n["id"]
    return numbers[0]["id"]


def connect_meta(s, account: Account, *, code: str, waba_id: str, phone_number_id: str | None = None) -> dict:
    """Called right after Embedded Signup finishes. The code expires in ~30s."""
    st = get_settings()
    if st.whatsapp_mode == "fake":
        token = "fake-token"
        phone_number_id = phone_number_id or f"fake-phone-{account.id}"
        wa = FakeWhatsApp(phone_number_id=phone_number_id, waba_id=waba_id)
    else:
        token = exchange_code_for_token(code)
        if not phone_number_id:
            phone_number_id = _pick_phone_number(list_phone_numbers(waba_id, token), account)
        wa = WhatsAppClient(token, phone_number_id, waba_id)
    upsert_integration(s, account.id, "meta", external={"waba_id": waba_id, "phone_number_id": phone_number_id}, token_enc=encrypt(token))
    info = {}
    if st.whatsapp_mode != "fake":
        subscribe_app_to_waba(waba_id, token)
        for sync_type in ("smb_app_state_sync", "history"):  # Coexistence: contacts + chat history (within 24h)
            try:
                sync_smb_app_data(phone_number_id, token, sync_type)
            except WhatsAppError as e:
                log.warning("smb sync %s failed: %s", sync_type, e)
        try:
            info = get_phone_number(phone_number_id, token)
        except WhatsAppError:
            pass
    integ = s.execute(select(Integration).where(Integration.account_id == account.id, Integration.provider == "meta")).scalar_one()
    integ.external = {**integ.external, "display_phone_number": info.get("display_phone_number", ""), "verified_name": info.get("verified_name", "")}
    account.last_agent_activity_at = utcnow()
    results = submit_templates(s, account, wa)
    advance(account, "waiting_templates")
    jobs.schedule(s, "check_templates", utcnow() + timedelta(minutes=1), account_id=account.id, dedupe_key=f"check_templates:{account.id}", reuse=True)
    log_event(s, "meta_connected", account.id, waba_id=waba_id, phone_number_id=phone_number_id)
    return {"ok": True, "templates": results}


def link_test_number(s, account: Account) -> None:
    """Dev/pilot: use the Meta test number from the environment for this account."""
    st = get_settings()
    upsert_integration(s, account.id, "meta", external={"waba_id": st.wa_waba_id, "phone_number_id": st.wa_phone_number_id, "test_number": True}, token_enc="")


@jobs.handler("check_templates")
def job_check_templates(s, job):
    """Activate the account automatically once the first-contact template is approved."""
    from .engine import agent_wa

    account = s.get(Account, job.account_id)
    if account.status == "paused":
        return
    try:
        wa = agent_wa(s, account)
        if not wa.waba_id:
            integ = s.execute(select(Integration).where(Integration.account_id == account.id, Integration.provider == "meta")).scalar_one_or_none()
            wa.waba_id = (integ.external or {}).get("waba_id", "") if integ else ""
        status = {t["name"]: t.get("status") for t in wa.list_templates()}
    except WhatsAppError as e:
        status, err = {}, str(e)
        log.warning("template check failed: %s", err)
    first = status.get(T.LEAD_FIRST_CONTACT.name)
    if first == "APPROVED":
        if account.status == "onboarding":
            account.status = "live"
            advance(account, "done")
            log_event(s, "account_live", account.id)
        return
    if first == "REJECTED":
        log_event(s, "template_rejected", account.id, template=T.LEAD_FIRST_CONTACT.name)
        alert_agent(s, account, T.AGENT_ACTION_REQUIRED, [first_name(account.agent_name) or "hola", "WhatsApp rechazó un mensaje; lo revisamos nosotros"])
        return
    if utcnow() - job.created_at < timedelta(days=3):
        job.status = "pending"
        job.run_at = utcnow() + timedelta(minutes=10)
