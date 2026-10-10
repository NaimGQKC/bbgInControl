"""Google Calendar: OAuth + free/busy + events (real + fake)."""
import itertools
from datetime import datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx

from .config import get_settings
from .crypto import decrypt, encrypt
from .db import utcnow
from .models import Integration

SCOPES = "openid email profile https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/calendar.freebusy"
API = "https://www.googleapis.com/calendar/v3"


class CalendarError(Exception):
    pass


def redirect_uri() -> str:
    return get_settings().base_url.rstrip("/") + "/oauth/google/callback"


def auth_url(state: str) -> str:
    s = get_settings()
    if s.calendar_mode == "fake":  # demo / preview: "Google" approves instantly
        return redirect_uri() + "?" + urlencode({"state": state, "code": "fake-code"})
    q = {
        "client_id": s.google_client_id,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(q)


def exchange_code(code: str) -> dict:
    s = get_settings()
    if s.calendar_mode == "fake":
        return {"access_token": "fake", "refresh_token": "fake", "expires_in": 3600, "scope": ""}
    r = httpx.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": code,
            "client_id": s.google_client_id,
            "client_secret": s.google_client_secret,
            "redirect_uri": redirect_uri(),
            "grant_type": "authorization_code",
        },
        timeout=20,
    )
    if r.status_code >= 400:
        raise CalendarError(r.text)
    return r.json()


def userinfo(access_token: str) -> dict:
    """Name and email of the Google account that just connected ({} if unavailable: onboarding then just asks)."""
    if get_settings().calendar_mode == "fake":
        return {"name": "Laura García", "email": "laura@example.com"}
    try:
        r = httpx.get("https://openidconnect.googleapis.com/v1/userinfo", headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


def refresh(refresh_token: str) -> dict:
    s = get_settings()
    r = httpx.post(
        "https://oauth2.googleapis.com/token",
        data={
            "refresh_token": refresh_token,
            "client_id": s.google_client_id,
            "client_secret": s.google_client_secret,
            "grant_type": "refresh_token",
        },
        timeout=20,
    )
    if r.status_code >= 400:
        raise CalendarError(r.text)
    return r.json()


def _iso(dt_utc: datetime) -> str:
    return dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo:
        dt = dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    return dt


class CalendarClient:
    def __init__(self, access_token: str, calendar_id: str = "primary"):
        self.token = access_token
        self.calendar_id = calendar_id

    @property
    def _h(self):
        return {"Authorization": f"Bearer {self.token}"}

    def busy(self, start_utc: datetime, end_utc: datetime) -> list[tuple[datetime, datetime]]:
        r = httpx.post(
            f"{API}/freeBusy",
            json={"timeMin": _iso(start_utc), "timeMax": _iso(end_utc), "items": [{"id": self.calendar_id}]},
            headers=self._h,
            timeout=20,
        )
        if r.status_code >= 400:
            raise CalendarError(r.text)
        cal = r.json()["calendars"].get(self.calendar_id, {})
        if cal.get("errors"):
            raise CalendarError(str(cal["errors"]))
        return [(_parse(b["start"]), _parse(b["end"])) for b in cal.get("busy", [])]

    def create_event(self, *, summary, description, location, start_utc, end_utc, timezone, request_id) -> str:
        body = {
            "summary": summary,
            "description": description,
            "location": location,
            "start": {"dateTime": _iso(start_utc), "timeZone": timezone},
            "end": {"dateTime": _iso(end_utc), "timeZone": timezone},
            "reminders": {"useDefault": True},
            "extendedProperties": {"private": {"sparrow_request_id": request_id}},
        }
        r = httpx.post(f"{API}/calendars/{self.calendar_id}/events", json=body, headers=self._h, timeout=20)
        if r.status_code >= 400:
            raise CalendarError(r.text)
        return r.json()["id"]

    def get_event(self, event_id: str) -> dict | None:
        """Returns {'start': utc, 'end': utc} or None if deleted/cancelled."""
        r = httpx.get(f"{API}/calendars/{self.calendar_id}/events/{event_id}", headers=self._h, timeout=20)
        if r.status_code in (404, 410):
            return None
        if r.status_code >= 400:
            raise CalendarError(r.text)
        ev = r.json()
        if ev.get("status") == "cancelled":
            return None
        if not ev.get("start", {}).get("dateTime"):  # turned into an all-day event
            return {"start": None, "end": None, "all_day": True}
        return {"start": _parse(ev["start"]["dateTime"]), "end": _parse(ev["end"]["dateTime"])}

    def delete_event(self, event_id: str) -> None:
        httpx.delete(f"{API}/calendars/{self.calendar_id}/events/{event_id}", headers=self._h, timeout=20)


class FakeCalendar(CalendarClient):
    busy_blocks: list[tuple[datetime, datetime]] = []
    events: dict[str, dict] = {}
    _ids = itertools.count(1)

    def __init__(self, *a, **k):
        super().__init__("fake")

    @classmethod
    def reset(cls):
        cls.busy_blocks = []
        cls.events = {}

    def busy(self, start_utc, end_utc):
        blocks = list(FakeCalendar.busy_blocks)
        blocks += [(e["start"], e["end"]) for e in FakeCalendar.events.values() if not e.get("deleted")]
        return [(s, e) for s, e in blocks if s < end_utc and e > start_utc]

    def create_event(self, *, summary, description, location, start_utc, end_utc, timezone, request_id):
        eid = f"evt{next(self._ids)}"
        FakeCalendar.events[eid] = {
            "summary": summary,
            "description": description,
            "location": location,
            "start": start_utc,
            "end": end_utc,
        }
        return eid

    def get_event(self, event_id):
        e = FakeCalendar.events.get(event_id)
        if not e or e.get("deleted"):
            return None
        return {"start": e["start"], "end": e["end"]}

    def delete_event(self, event_id):
        if event_id in FakeCalendar.events:
            FakeCalendar.events[event_id]["deleted"] = True


def mark_revoked(session, account, integ) -> None:
    """The agent removed Sparrow in Google (or the refresh token died): delete the tokens (privacy policy) and ask them to reconnect."""
    from . import templates as T
    from .engine import alert_agent, first_name, log_event

    integ.token_enc = ""
    integ.refresh_token_enc = ""
    integ.status = "disconnected"
    log_event(session, "google_disconnected", account.id)
    alert_agent(session, account, T.AGENT_ACTION_REQUIRED, [first_name(account.agent_name) or "hola", "vuelve a conectar tu Google Calendar desde tu enlace de Sparrow"])


def calendar_for_account(session, account) -> CalendarClient:
    """Returns a ready client, refreshing the access token when needed."""
    if get_settings().calendar_mode == "fake":
        return FakeCalendar()
    integ = session.query(Integration).filter_by(account_id=account.id, provider="google").one_or_none()
    if not integ or integ.status != "active":
        raise CalendarError("Google Calendar not connected")
    if not integ.expires_at or integ.expires_at < utcnow() + timedelta(minutes=2):
        try:
            data = refresh(decrypt(integ.refresh_token_enc))
        except CalendarError as e:
            if "invalid_grant" in str(e):  # the agent revoked access or the token expired
                mark_revoked(session, account, integ)
            raise
        integ.token_enc = encrypt(data["access_token"])
        integ.expires_at = utcnow() + timedelta(seconds=int(data.get("expires_in", 3600)))
        session.flush()
    return CalendarClient(decrypt(integ.token_enc), (integ.external or {}).get("calendar_id", "primary"))
