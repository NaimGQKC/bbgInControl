"""CASAFARI Private Seller Leads API (particular listings with the owner's phone). Spain only.

`client()` gives the real client or the fake one (CASAFARI_MODE=live|fake, default fake), like WHATSAPP_MODE.
Every search costs tokens (10 per call, about €0.40), so callers must cap how many they make (see app/prospecting.py).
CASAFARI terms: results may be stored only while our CASAFARI account is open + 30 days, never resold, and
contacting sellers is our legal responsibility. Callers keep only what a lead needs, never the raw payload.
"""
import logging
from datetime import date

import httpx

from .config import get_settings

log = logging.getLogger("sparrow.casafari")

BASE_URL = "https://platform.casafari.com"
TOKENS_PER_SEARCH = 10
EUR_PER_TOKEN = 0.04
MAX_PAGE_SIZE = 25  # the Free plan caps at 25 (the API allows 50)
PROXY_KEY = "proxy-injected"  # sandbox only: the network proxy adds the real key, so we send no header


class CasafariError(Exception):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


class CasafariOutOfTokens(CasafariError):
    """402: the plan has no tokens left (the response carries an upgrade_url)."""

    def __init__(self, msg: str, upgrade_url: str = ""):
        super().__init__(msg, 402)
        self.upgrade_url = upgrade_url


class CasafariRateLimited(CasafariError):
    """429: too many requests (Free plan: 6 per minute)."""

    def __init__(self, msg: str, retry_after: int = 60):
        super().__init__(msg, 429)
        self.retry_after = retry_after


def _day(d) -> str | None:
    if d is None:
        return None
    return d.isoformat() if isinstance(d, date) else str(d)


def _search_body(location_id: int, since, until=None, operation=None, property_type=None,
                 confirmed_only=False, page_size=MAX_PAGE_SIZE, cursor=None) -> dict:
    body = {"location_id": int(location_id), "since": _day(since), "page_size": max(1, min(int(page_size), MAX_PAGE_SIZE))}
    if until:
        body["until"] = _day(until)
    if operation:
        body["operation"] = operation
    if property_type:
        body["property_type"] = property_type
    if confirmed_only:
        body["confirmed_only"] = True
    if cursor:
        body["cursor"] = cursor
    return body


class CasafariClient:
    """The real API (httpx, short timeouts, typed errors for 402/429)."""

    def __init__(self, api_key: str, base_url: str = BASE_URL, timeout: float = 20.0):
        if not api_key:
            raise CasafariError("CASAFARI_API_KEY is not set")
        headers = {"Accept": "application/json"}
        if api_key != PROXY_KEY:
            headers["Authorization"] = f"Bearer {api_key}"
        self._http = httpx.Client(base_url=base_url, headers=headers, timeout=httpx.Timeout(timeout, connect=10.0))
        self.searches = 0  # paid calls made by this client

    def _check(self, r: httpx.Response) -> dict:
        if r.status_code < 400:
            return r.json()
        try:
            problem = r.json()
        except ValueError:
            problem = {}
        detail = problem.get("detail") or problem.get("title") or r.text[:200]
        if r.status_code == 402:
            raise CasafariOutOfTokens(f"CASAFARI: out of tokens ({detail})", problem.get("upgrade_url", ""))
        if r.status_code == 429:
            try:
                retry = int(r.headers.get("Retry-After", "60"))
            except ValueError:
                retry = 60
            raise CasafariRateLimited(f"CASAFARI: rate limited ({detail})", retry)
        raise CasafariError(f"CASAFARI {r.status_code}: {detail}", r.status_code)

    def find_location(self, query: str) -> list[dict]:
        """Free (no tokens), but counts toward the 10 distinct places per day of the Free plan."""
        try:
            r = self._http.get("/v1/locations/search", params={"query": query})
        except httpx.HTTPError as e:
            raise CasafariError(f"CASAFARI request failed: {e}") from e
        return self._check(r).get("results", [])

    def search(self, location_id: int, since, until=None, operation=None, property_type=None,
               confirmed_only=False, page_size=MAX_PAGE_SIZE, cursor=None) -> dict:
        """One page of private-seller listings: {"listings": [...], "next": cursor|None, "contacts_included": bool}.
        Costs TOKENS_PER_SEARCH tokens."""
        body = _search_body(location_id, since, until, operation, property_type, confirmed_only, page_size, cursor)
        self.searches += 1
        try:
            r = self._http.post("/v1/private-sellers/search", json=body)
        except httpx.HTTPError as e:
            raise CasafariError(f"CASAFARI request failed: {e}") from e
        data = self._check(r)
        return {"listings": data.get("listings") or [], "next": data.get("next"), "contacts_included": bool(data.get("contacts_included"))}


# ---------------------------------------------------------------- fake (tests / local demo)

def _l(i, portal, ptype, price, area, phone, name="", *, first_seen="2026-09-28", op="sale", email=None, loc=312416):
    return {
        "id": f"psl_fake{i:03d}",
        "url": f"https://example.test/{portal.split()[0].lower()}/{i}",
        "portal": portal,
        "first_seen": first_seen,
        "operation": op,
        "property_type": ptype,
        "price": price,
        "currency": "EUR",
        "area": area,
        "bedrooms": 3 if ptype in ("apartment", "house") else None,
        "bathrooms": 1 if ptype in ("apartment", "house") else None,
        "location_id": loc,
        "coordinates": {"lat": 41.65, "lon": -0.88},
        "title": f"Fake listing {i}",
        "contact": {"name": name or None, "phone": phone, "email": email},
    }


# Deterministic fixtures. All phones are fictional (+34 600 0xx xxx test range or obviously fake).
FAKE_LISTINGS = [
    _l(1, "Idealista", "apartment", 185000, 90, "+34600100001", "MARIA LOPEZ"),
    _l(2, "Fotocasa for Sale", "apartment", 185000, 90, "+34600100002", "Maria"),  # same flat as 1, other portal (other phone)
    _l(3, "Habitaclia", "house", 320000, 160, "600 100 003", "juan perez"),
    _l(4, "Pisos", "apartment", 140000, 70, None, "Ana"),  # no phone
    _l(5, "Idealista", "plot", 60000, 500, "+34600100005", "Luis"),  # plot: excluded by default
    _l(6, "Idealista", "investment", 900000, 800, "+34600100006"),  # investment: excluded by default
    _l(7, "Fotocasa for Sale", "apartment", 210000, 95, "+447700900123", "John"),  # foreign number
    _l(8, "Pisos", "apartment", 99000, 60, "+34600100001", "María López"),  # same phone as 1 (another flat)
    _l(9, "Idealista", "apartment", 175000, 85, "+34600100009", "Particular"),  # generic name -> no name
    _l(10, "Habitaclia", "apartment", 2500000, 300, "+34600100010", "Pedro"),  # above a typical max price
    _l(11, "Idealista", "apartment", 155000, 75, "+34600100011", "Carmen", op="rent"),  # rent
    _l(12, "Pisos", "house", 260000, 140, "934 000 012", "Jordi"),  # landline
    _l(13, "Idealista", "apartment", 199000, 88, "+34600100013", "Elena", first_seen="2026-05-01"),  # too old
    _l(14, "Fotocasa for Sale", "apartment", 230000, 100, "+34600100014", "Sofia"),
    _l(15, "Idealista", "apartment", 230000, 100, "+34600100015", "Sofia"),  # same flat as 14, other portal
    _l(16, "Pisos", "apartment", None, None, "+34600100016", "Raúl"),  # no price / area (kept, never cross-portal dup)
    _l(17, "Idealista", "apartment", 120000, 65, "12345", "Bad"),  # invalid phone
    _l(18, "Habitaclia", "apartment", 168000, 80, "+34711100018", "Nuria"),
    _l(19, "Idealista", "apartment", 172000, 82, "+34600100019", "Iván"),
    _l(20, "Pisos", "house", 289000, 150, "+34600100020", "Lucía"),
    _l(21, "Idealista", "apartment", 148000, 72, "+34600100021", "Marcos"),
    _l(22, "Fotocasa for Sale", "apartment", 205000, 92, "+34600100022", "Teresa"),
    _l(23, "Habitaclia", "apartment", 133000, 68, "+34600100023", "Óscar"),
    _l(24, "Idealista", "apartment", 245000, 110, "+34600100024", "Pilar"),
    _l(25, "Pisos", "apartment", 159000, 77, "+34600100025", "Hugo"),
]

FAKE_LOCATIONS = [
    {"id": 312416, "name": "Zaragoza capital", "kind": "city", "level": 5, "country_code": "ES", "breadcrumbs": ["España", "Aragón", "Zaragoza"]},
    {"id": 312400, "name": "Zaragoza", "kind": "province", "level": 3, "country_code": "ES", "breadcrumbs": ["España", "Aragón"]},
    {"id": 280796, "name": "Madrid capital", "kind": "city", "level": 5, "country_code": "ES", "breadcrumbs": ["España", "Madrid"]},
]


class FakeCasafari:
    """In memory. `calls` records every paid search; `listings` can be replaced by a test."""

    calls: list[dict] = []
    listings: list[dict] = FAKE_LISTINGS
    fail_with: Exception | None = None  # set to raise on the next search (402/429 tests)

    @classmethod
    def reset(cls):
        cls.calls = []
        cls.listings = FAKE_LISTINGS
        cls.fail_with = None

    @property
    def searches(self) -> int:
        return len(self.calls)

    def find_location(self, query: str) -> list[dict]:
        q = (query or "").strip().lower()
        return [loc for loc in FAKE_LOCATIONS if q and q in loc["name"].lower()]

    def search(self, location_id: int, since, until=None, operation=None, property_type=None,
               confirmed_only=False, page_size=MAX_PAGE_SIZE, cursor=None) -> dict:
        body = _search_body(location_id, since, until, operation, property_type, confirmed_only, page_size, cursor)
        FakeCasafari.calls.append(body)
        if FakeCasafari.fail_with is not None:
            e, FakeCasafari.fail_with = FakeCasafari.fail_with, None
            raise e
        rows = [x for x in FakeCasafari.listings if x["location_id"] == body["location_id"] and x["first_seen"] >= body["since"]]
        if operation:
            rows = [x for x in rows if x["operation"] == operation]
        if property_type:
            rows = [x for x in rows if x["property_type"] == property_type]
        start = int(cursor or 0)
        page = rows[start:start + body["page_size"]]
        nxt = str(start + body["page_size"]) if start + body["page_size"] < len(rows) else None
        return {"listings": [dict(x, contact=dict(x["contact"])) for x in page], "next": nxt, "contacts_included": True}


def client():
    st = get_settings()
    if st.casafari_mode == "live":
        return CasafariClient(st.casafari_api_key)
    return FakeCasafari()
