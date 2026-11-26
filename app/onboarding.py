"""Onboarding actions: Google Calendar, Meta Embedded Signup, templates, activation."""
import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from . import coldmsg, gcal, jobs, lang as L, templates as T
from .config import get_settings
from .crypto import decrypt, encrypt
from .db import utcnow
from .engine import alert_agent, alert_ops, first_name, log_event
from .guards import normalize_phone, tidy_name
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

REQUIRED_ANSWERS = ["agent_name", "agency", "zones"]  # the mobile for alerts comes from Meta (or one extra question after it)
STEPS = ["google", "questions", "payment", "wa_check", "whatsapp", "waiting_templates", "done"]


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


def _connected(integ: dict, provider: str) -> bool:
    return integ.get(provider, {}).get("status") == "active"


def payment_required() -> bool:
    return bool(get_settings().stripe_payment_link)


def after_questions(s, account: Account) -> str:
    return "payment" if payment_required() and not _connected(integrations_status(s, account), "stripe") else "wa_check"


def normalize_step(s, account: Account) -> None:
    """Puts accounts created by older versions of the flow (or skipped steps) back on the right step."""
    integ = integrations_status(s, account)
    step = "payment" if account.onboarding_step == "lystos" else account.onboarding_step
    if step in ("questions", "payment") and not _connected(integ, "google"):
        step = "google"
    if step == "questions" and all(getattr(account, f) for f in REQUIRED_ANSWERS):
        step = after_questions(s, account)
    if step == "payment" and not (payment_required() and not _connected(integ, "stripe")):
        step = "wa_check"
    account.onboarding_step = step


def mark_paid(s, account: Account, session_id: str) -> bool:
    """Stripe says the integration fee is paid. Idempotent: Stripe retries webhooks."""
    current = s.execute(select(Integration).where(Integration.account_id == account.id, Integration.provider == "stripe")).scalar_one_or_none()
    if current and current.status == "active":
        return False
    upsert_integration(s, account.id, "stripe", external={"session_id": session_id})
    log_event(s, "payment_received", account.id, session_id=session_id)
    if account.onboarding_step == "payment":
        advance(account, "wa_check")
    return True


HELP_KINDS = {"call": "QUIERE QUE LE LLAMEN", "chat": "Quiere que le escriban"}


def request_help(s, account: Account, *, message: str, contact: str, kind: str = "chat") -> None:
    """The agent asked to talk to a person (call or message): log it (shows in /admin) and WhatsApp the operator."""
    kind = kind if kind in HELP_KINDS else "chat"
    log_event(s, "onboarding_help_requested", account.id, step=account.onboarding_step, message=message, contact=contact, kind=kind)
    alert_ops(s, account, T.OPS_NEEDS_HELP, [account.agent_name or "Un agente", account.onboarding_step, contact, f"{HELP_KINDS[kind]}. {message}".strip()])


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
    info = gcal.userinfo(token_data["access_token"])  # we already know who they are: don't ask
    if info.get("name") and (not account.agent_name or " " not in account.agent_name.strip()):  # a first name from the website demo gives way to the full name from Google
        account.agent_name = tidy_name(str(info["name"]).strip()[:200])
    if not account.email and info.get("email"):
        account.email = str(info["email"]).strip()[:200]
    if account.onboarding_step == "google":
        advance(account, "questions")


def submit_templates(s, account: Account, wa: WhatsAppClient) -> list[dict]:
    results = []
    lang = L.lead_lang(account)  # this agent's leads speak this language: submit the templates in it
    for tpl in T.AGENT_TEMPLATES:
        try:
            r = wa.create_template(tpl, language=lang)
            results.append({"name": tpl.name, "language": lang, "ok": True, "status": r.get("status", "")})
        except WhatsAppError as e:
            # "already exists" is fine when re-running
            results.append({"name": tpl.name, "language": lang, "ok": False, "error": str(e)})
    log_event(s, "templates_submitted", account.id, results=results)
    cold = submit_cold_template(s, account, wa, schedule_check=False)  # the caller schedules the check; go-live never waits for it
    if cold:
        results.append(cold)
    return results


def submit_cold_template(s, account: Account, wa: WhatsAppClient | None = None, *, schedule_check: bool = True) -> dict | None:
    """Submit the agent's cold first message (settings["prospecting"]["cold_body"] + Sparrow's footer) to Meta as a
    MARKETING template in the lead language. Call it at connect (submit_templates does), when the agent saves a new
    cold_body, and when the lead language changes. A changed text gets a new version (a new template name); the
    same text in a new language keeps its name. Sets cold_template_status to "pending" until check_templates sees
    Meta's verdict. Returns the result row, or None when there is no text or WhatsApp isn't connected yet."""
    from .engine import agent_wa, cold_privacy_url, prospecting_cfg, set_prospecting

    cfg = prospecting_cfg(account)
    body = (cfg.get("cold_body") or "").strip()
    if not body:
        return None
    lang = L.lead_lang(account)
    problems, _ = coldmsg.validate(body)
    if problems:
        log_event(s, "cold_template_invalid", account.id, problems=problems)
        return {"name": "cold", "language": lang, "ok": False, "error": "invalid:" + ",".join(problems)}
    if wa is None:
        integ = s.execute(select(Integration).where(Integration.account_id == account.id, Integration.provider == "meta")).scalar_one_or_none()
        if not (integ and integ.status == "active"):
            return None  # submitted at connect
        try:
            wa = agent_wa(s, account)
        except WhatsAppError:
            return None
        wa.waba_id = wa.waba_id or (integ.external or {}).get("waba_id", "")
    sub = cfg.get("cold_submitted") or {}
    version, last = int(cfg.get("cold_version") or 0), int(sub.get("version") or 0)
    if body != sub.get("body") or not last:
        version = version if version > last else last + 1  # web may already have bumped it when the text was saved
    elif sub.get("lang") == lang and cfg.get("cold_template_status") in ("pending", "approved"):
        return {"name": T.cold_name(last), "language": lang, "ok": True, "status": "unchanged"}
    else:
        version = last
    privacy = cold_privacy_url()
    tpl = T.cold_first_contact(version, lang, body, privacy)
    try:
        r = wa.create_template(tpl, language=lang)
        row = {"name": tpl.name, "language": lang, "ok": True, "status": r.get("status", "")}
    except WhatsAppError as e:
        row = {"name": tpl.name, "language": lang, "ok": False, "error": str(e)}
    set_prospecting(account, cold_version=version, cold_template_status="pending", cold_template_reason="",
                    cold_submitted={"version": version, "lang": lang, "body": body, "privacy_url": privacy})
    log_event(s, "cold_template_submitted", account.id, result=row)
    if schedule_check:
        jobs.schedule(s, "check_templates", utcnow() + timedelta(minutes=1), account_id=account.id,
                      dedupe_key=f"check_templates:{account.id}", reuse=True, payload={"since": utcnow().isoformat()})
    return row


_COLD_STATUS = {"APPROVED": "approved", "REJECTED": "rejected", "DISABLED": "rejected"}  # PENDING, IN_APPEAL, PAUSED... → pending


def refresh_cold_status(s, account: Account, status: dict, reasons: dict) -> str | None:
    """check_templates saw Meta's list: store the cold template's state. Approved → the waiting cold leads go out;
    rejected → the agent and the operator are told (with Meta's reason when it gives one). Returns the status."""
    from .engine import prospecting_cfg, release_cold_queue, set_prospecting

    cfg = prospecting_cfg(account)
    sub = cfg.get("cold_submitted") or {}
    if not sub.get("version"):
        return None
    key = (T.cold_name(sub["version"]), sub.get("lang") or "es")
    if key not in status:
        return cfg.get("cold_template_status")
    new = _COLD_STATUS.get(str(status[key] or "").upper(), "pending")
    old = cfg.get("cold_template_status")
    if new == old:
        return new
    reason = str(reasons.get(key) or "")
    set_prospecting(account, cold_template_status=new, cold_template_reason=reason if new == "rejected" else "")
    log_event(s, f"cold_template_{new}", account.id, template=key[0], reason=reason)
    if new == "approved":
        release_cold_queue(s, account)
    elif new == "rejected":
        lang = L.lead_lang(account)
        why = reason or str(status[key] or "").lower() or "?"
        alert_agent(s, account, T.AGENT_ACTION_REQUIRED, [L.agent_hi(account), L.text("alert_cold_template_rejected", lang, reason=why)])
        alert_ops(s, account, T.OPS_NEEDS_HELP, [account.agent_name or "Un agente", "cold_template", account.email or "-",
                                                 f"Meta rechazó {key[0]} ({key[1]}): {why}"])
    return new


def change_lead_lang(s, account: Account, lang: str) -> None:
    """The agent's leads speak another language. Their WhatsApp templates must exist (and be approved) in it:
    if WhatsApp is already connected, submit them now and let the daily check notice the approval."""
    if lang not in L.LANGS:
        return
    L.set_lead_lang(account, lang)
    account.settings = {**(account.settings or {}), "lang": lang}
    log_event(s, "lead_lang_changed", account.id, lang=lang)
    integ = s.execute(select(Integration).where(Integration.account_id == account.id, Integration.provider == "meta")).scalar_one_or_none()
    if integ and integ.status == "active" and integ.token_enc:
        from .engine import agent_wa

        wa = agent_wa(s, account)
        wa.waba_id = wa.waba_id or (integ.external or {}).get("waba_id", "")
        submit_templates(s, account, wa)
        jobs.schedule(s, "check_templates", utcnow() + timedelta(minutes=1), account_id=account.id, dedupe_key=f"check_templates:{account.id}",
                      reuse=True, payload={"since": utcnow().isoformat()})


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
    if not account.personal_whatsapp:  # alerts default to the number they just connected; the chat lets them change it
        account.personal_whatsapp = normalize_phone(info.get("display_phone_number")) or ""
    account.last_agent_activity_at = utcnow()
    check_meta_payment(s, account, token=token, waba_id=waba_id)
    results = submit_templates(s, account, wa)
    advance(account, "waiting_templates")
    jobs.schedule(s, "check_templates", utcnow() + timedelta(minutes=1), account_id=account.id, dedupe_key=f"check_templates:{account.id}", reuse=True)
    log_event(s, "meta_connected", account.id, waba_id=waba_id, phone_number_id=phone_number_id)
    return {"ok": True, "templates": results}


def check_meta_payment(s, account: Account, *, token: str | None = None, waba_id: str | None = None) -> str:
    """Asks Meta whether the agent's WhatsApp Business account has a payment method and remembers the answer next to the connection.
    A warning only (it never holds the account back): the onboarding shows what to do, /admin shows the state, and the operator is told once."""
    from .whatsapp import waba_payment_state

    integ = s.execute(select(Integration).where(Integration.account_id == account.id, Integration.provider == "meta")).scalar_one_or_none()
    if integ is None:
        return "unknown"
    ext = dict(integ.external or {})
    if get_settings().whatsapp_mode == "fake":
        from .whatsapp import FakeWhatsApp
        state = FakeWhatsApp.payment_state
    else:
        tok = token if token is not None else (decrypt(integ.token_enc) if integ.token_enc else "")
        state = waba_payment_state(waba_id or ext.get("waba_id") or "", tok) if tok else "unknown"
    before = ext.get("payment_state")
    integ.external = {**ext, "payment_state": state, "payment_checked_at": utcnow().isoformat()}
    log_event(s, "meta_payment_checked", account.id, state=state)
    if state == "missing" and before != "missing":
        alert_ops(s, account, T.OPS_NEEDS_HELP, [account.agent_name or f"cuenta {account.id}", "pago de Meta", account.personal_whatsapp or "-",
                                                 "su cuenta de WhatsApp Business no tiene método de pago: Meta no entregará mensajes"])
    return state


def link_test_number(s, account: Account) -> None:
    """Dev/pilot: use the Meta test number from the environment for this account."""
    st = get_settings()
    upsert_integration(s, account.id, "meta", external={"waba_id": st.wa_waba_id, "phone_number_id": st.wa_phone_number_id, "test_number": True}, token_enc="")


@jobs.handler("check_templates")
def job_check_templates(s, job):
    """Activate the account automatically once the first-contact template is approved. Also tracks the agent's cold
    template (prospecting), which never holds the account back."""
    from .engine import agent_wa

    account = s.get(Account, job.account_id)
    if account.status == "paused":
        return
    try:
        wa = agent_wa(s, account)
        if not wa.waba_id:
            integ = s.execute(select(Integration).where(Integration.account_id == account.id, Integration.provider == "meta")).scalar_one_or_none()
            wa.waba_id = (integ.external or {}).get("waba_id", "") if integ else ""
        listed = wa.list_templates()
        status = {(t["name"], t.get("language", "es")[:2]): t.get("status") for t in listed}
        reasons = {(t["name"], t.get("language", "es")[:2]): t.get("rejected_reason") for t in listed if t.get("rejected_reason") not in (None, "", "NONE")}
    except WhatsAppError as e:
        status, reasons, err = {}, {}, str(e)
        log.warning("template check failed: %s", err)
    cold = refresh_cold_status(s, account, status, reasons)
    since = job.created_at
    try:
        since = max(since, datetime.fromisoformat((job.payload or {}).get("since", "")))
    except ValueError:
        pass
    keep_checking = utcnow() - since < timedelta(days=3)
    first = status.get((T.LEAD_FIRST_CONTACT.name, L.lead_lang(account)))
    if first == "APPROVED":
        if account.status == "onboarding":
            account.status = "live"
            advance(account, "done")
            log_event(s, "account_live", account.id)
            alert_agent(s, account, T.AGENT_LIVE, [L.agent_hi(account)])
            from . import prospecting
            from .engine import rearm_queued_leads
            prospecting.activate_if_requested(s, account)
            rearm_queued_leads(s, account)
        again = cold == "pending"  # only the agent's cold template is still waiting for Meta
    elif first == "REJECTED":
        log_event(s, "template_rejected", account.id, template=T.LEAD_FIRST_CONTACT.name)
        alert_agent(s, account, T.AGENT_ACTION_REQUIRED, [L.agent_hi(account), L.text("alert_template_rejected", L.lead_lang(account))])
        again = False  # the operator fixes it; checking again would repeat the alert
    else:
        again = True
    if again and keep_checking:
        job.status = "pending"
        job.run_at = utcnow() + timedelta(minutes=10)
