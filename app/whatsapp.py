"""WhatsApp Cloud API client (real + fake).

Docs: https://developers.facebook.com/docs/whatsapp/cloud-api
"""
import itertools
import logging

import httpx

from .config import get_settings
from .templates import Template

log = logging.getLogger("sparrow.whatsapp")


class WhatsAppError(Exception):
    def __init__(self, message: str, code: int | None = None, payload: dict | None = None):
        super().__init__(message)
        self.code = code
        self.payload = payload or {}


def _graph(path: str) -> str:
    return f"https://graph.facebook.com/{get_settings().graph_version}/{path.lstrip('/')}"


def _check(r: httpx.Response) -> dict:
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text}
    if r.status_code >= 400 or "error" in data:
        err = data.get("error", {}) if isinstance(data, dict) else {}
        raise WhatsAppError(err.get("message") or f"HTTP {r.status_code}", err.get("code"), data)
    return data


class WhatsAppClient:
    def __init__(self, token: str, phone_number_id: str, waba_id: str = ""):
        self.token = token
        self.phone_number_id = phone_number_id
        self.waba_id = waba_id

    @property
    def _headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    def _post_message(self, body: dict) -> str:
        body = {"messaging_product": "whatsapp", "recipient_type": "individual", **body}
        r = httpx.post(_graph(f"{self.phone_number_id}/messages"), json=body, headers=self._headers, timeout=20)
        data = _check(r)
        return data["messages"][0]["id"]

    def send_text(self, to: str, body: str) -> str:
        return self._post_message({"to": to.lstrip("+"), "type": "text", "text": {"preview_url": False, "body": body}})

    def send_template(self, to: str, template: Template, values: list[str], language: str | None = None) -> str:
        comp = [{"type": "body", "parameters": [{"type": "text", "text": str(v)} for v in values]}] if values else []
        return self._post_message(
            {
                "to": to.lstrip("+"),
                "type": "template",
                "template": {
                    "name": template.name,
                    "language": {"code": language or get_settings().template_language},
                    "components": comp,
                },
            }
        )

    def send_raw_template(self, to: str, name: str, language: str = "en_US") -> str:
        """For Meta's built-in test template (hello_world)."""
        return self._post_message({"to": to.lstrip("+"), "type": "template", "template": {"name": name, "language": {"code": language}}})

    def mark_read(self, wa_message_id: str) -> None:
        try:
            httpx.post(
                _graph(f"{self.phone_number_id}/messages"),
                json={"messaging_product": "whatsapp", "status": "read", "message_id": wa_message_id},
                headers=self._headers,
                timeout=10,
            )
        except Exception:  # never important
            pass

    def download_media(self, media_id: str) -> tuple[bytes, str]:
        meta = _check(httpx.get(_graph(media_id), headers=self._headers, timeout=20))
        r = httpx.get(meta["url"], headers=self._headers, timeout=60)
        r.raise_for_status()
        return r.content, meta.get("mime_type", "audio/ogg")

    def create_template(self, template: Template, language: str | None = None) -> dict:
        body = {
            "name": template.name,
            "language": language or get_settings().template_language,
            "category": template.category,
            "components": template.components_for_submit(),
        }
        return _check(httpx.post(_graph(f"{self.waba_id}/message_templates"), json=body, headers=self._headers, timeout=20))

    def list_templates(self) -> list[dict]:
        data = _check(
            httpx.get(
                _graph(f"{self.waba_id}/message_templates"),
                params={"fields": "name,status,category,language", "limit": 200},
                headers=self._headers,
                timeout=20,
            )
        )
        return data.get("data", [])


# ---------- Embedded Signup (Tech Provider) ----------

def exchange_code_for_token(code: str) -> str:
    """The code from Embedded Signup expires in ~30s: exchange it immediately."""
    s = get_settings()
    r = httpx.get(
        _graph("oauth/access_token"),
        params={"client_id": s.meta_app_id, "client_secret": s.meta_app_secret, "code": code},
        timeout=20,
    )
    return _check(r)["access_token"]


def subscribe_app_to_waba(waba_id: str, token: str) -> dict:
    """Makes Meta send this WABA's webhooks to our app."""
    return _check(httpx.post(_graph(f"{waba_id}/subscribed_apps"), headers={"Authorization": f"Bearer {token}"}, timeout=20))


def register_phone(phone_number_id: str, token: str, pin: str) -> dict:
    """Only for numbers NOT onboarded with Coexistence (Coexistence numbers are already registered)."""
    return _check(
        httpx.post(
            _graph(f"{phone_number_id}/register"),
            json={"messaging_product": "whatsapp", "pin": pin},
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
    )


def sync_smb_app_data(phone_number_id: str, token: str, sync_type: str) -> dict:
    """Coexistence: start contact sync ('smb_app_state_sync') or chat history sync ('history'). Must run within 24h of onboarding."""
    return _check(
        httpx.post(
            _graph(f"{phone_number_id}/smb_app_data"),
            json={"messaging_product": "whatsapp", "sync_type": sync_type},
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
    )


def list_phone_numbers(waba_id: str, token: str) -> list[dict]:
    data = _check(
        httpx.get(
            _graph(f"{waba_id}/phone_numbers"),
            params={"fields": "id,display_phone_number,verified_name"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
    )
    return data.get("data", [])


def get_phone_number(phone_number_id: str, token: str) -> dict:
    return _check(
        httpx.get(
            _graph(phone_number_id),
            params={"fields": "display_phone_number,verified_name,quality_rating"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
    )


# ---------- Fake (tests / local demo) ----------

class FakeWhatsApp(WhatsAppClient):
    outbox: list[dict] = []
    created_templates: list[dict] = []
    _ids = itertools.count(1)
    fail_next: Exception | None = None

    def __init__(self, token: str = "fake", phone_number_id: str = "fake-phone", waba_id: str = "fake-waba"):
        super().__init__(token, phone_number_id, waba_id)

    @classmethod
    def reset(cls):
        cls.outbox.clear()
        cls.created_templates.clear()
        cls.fail_next = None

    def _record(self, item: dict) -> str:
        if FakeWhatsApp.fail_next:
            e, FakeWhatsApp.fail_next = FakeWhatsApp.fail_next, None
            raise e
        mid = f"wamid.fake.{next(self._ids)}"
        FakeWhatsApp.outbox.append({"id": mid, "from": self.phone_number_id, **item})
        log.info("FAKE WA %s -> %s: %s", self.phone_number_id, item.get("to"), item.get("text"))
        return mid

    def send_text(self, to, body):
        return self._record({"to": to.lstrip("+"), "type": "text", "text": body})

    def send_template(self, to, template, values, language=None):
        return self._record({"to": to.lstrip("+"), "type": "template", "template": template.name, "text": template.render(values)})

    def send_raw_template(self, to, name, language="en_US"):
        return self._record({"to": to.lstrip("+"), "type": "template", "template": name, "text": name})

    def mark_read(self, wa_message_id):
        pass

    def download_media(self, media_id):
        return b"fake-audio", "audio/ogg"

    def create_template(self, template, language=None):
        FakeWhatsApp.created_templates.append({"name": template.name, "category": template.category})
        return {"id": f"tpl-{template.name}", "status": "PENDING", "category": template.category}

    def list_templates(self):
        return [{"name": t["name"], "status": "APPROVED", "category": t["category"]} for t in FakeWhatsApp.created_templates]
