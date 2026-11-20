"""How many usable leads does CASAFARI give per area, and what does each cost? Writes nothing to the database.

    python scripts/casafari_yield.py Zaragoza Huesca                 # fake data (free)
    CASAFARI_MODE=live CASAFARI_API_KEY=... python scripts/casafari_yield.py Zaragoza --since-days 30
    python scripts/casafari_yield.py --location-id 312416 --mode live

Every search costs 10 tokens (about €0.40). The script refuses to make more than --max-searches (default 5) and
never more than 10 without --yes. It never prints phone numbers or names.
"""
import argparse
import os
import sys
import unicodedata
from collections import Counter
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import casafari, prospecting as P  # noqa: E402
from app.config import get_settings  # noqa: E402

HARD_LIMIT = 10


def _plain(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower().strip()


def pick_location(cl, name: str) -> dict | None:
    """Spanish places only; an exact name ("Huesca", "Zaragoza capital") beats a partial one ("Casbas de Huesca"),
    then a city beats a province or region."""
    q = _plain(name)
    results = [r for r in cl.find_location(name) if r.get("country_code") == "ES"]
    results.sort(key=lambda r: (_plain(r.get("name")) not in (q, f"{q} capital"), r.get("kind") != "city", r.get("level") or 99))
    return results[0] if results else None


def area_report(cl, location_id: int, label: str, args, cfg: dict) -> dict:
    today = date.today()
    since = today - timedelta(days=args.since_days)
    listings, cursor, searches = [], None, 0
    for _ in range(args.pages):
        page = cl.search(location_id, since, until=today, operation=cfg["operation"], cursor=cursor)
        searches += 1
        listings += page.get("listings") or []
        cursor = page.get("next")
        if not cursor:
            break
    types = Counter(str(x.get("property_type") or "?") for x in listings)
    portals = Counter(str(x.get("portal") or "?") for x in listings)
    with_phone = sum(1 for x in listings if (x.get("contact") or {}).get("phone"))
    kept, skipped = P.filter_listings(listings, cfg, since)
    tokens = searches * casafari.TOKENS_PER_SEARCH
    eur = tokens * casafari.EUR_PER_TOKEN
    return {"label": label, "location_id": location_id, "searches": searches, "fetched": len(listings), "types": types,
            "portals": portals, "with_phone": with_phone, "usable": len(kept), "skipped": skipped, "tokens": tokens,
            "eur": eur, "more": bool(cursor), "sample": [P.mask_phone(k["phone"]) for k in kept[:3]]}


def pct(a, b) -> str:
    return f"{100 * a / b:.0f}%" if b else "-"


def print_report(r: dict) -> None:
    n = r["fetched"]
    print(f"\n== {r['label']} (location {r['location_id']})")
    print(f"  searches: {r['searches']}  tokens: {r['tokens']}  cost: €{r['eur']:.2f}{'  (more pages available)' if r['more'] else ''}")
    print(f"  particular listings fetched: {n}")
    print(f"  by type: {dict(r['types'])}")
    print(f"  by portal: {dict(r['portals'])}")
    print(f"  with phone: {r['with_phone']} ({pct(r['with_phone'], n)})")
    print(f"  usable after filters + dedupe: {r['usable']} ({pct(r['usable'], n)})")
    print(f"  skipped: {dict(r['skipped'])}")
    print(f"  EUR per usable lead: {'€%.2f' % (r['eur'] / r['usable']) if r['usable'] else '-'}")
    print(f"  sample (masked): {', '.join(r['sample']) or '-'}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("places", nargs="*", help="place names, e.g. Zaragoza Huesca")
    ap.add_argument("--location-id", type=int, action="append", default=[], help="CASAFARI location id (skips the lookup)")
    ap.add_argument("--mode", choices=["live", "fake"], help="default: CASAFARI_MODE (fake)")
    ap.add_argument("--since-days", type=int, default=30, help="look back this many days (max 89)")
    ap.add_argument("--pages", type=int, default=1, help="pages per area (25 listings each, 10 tokens each)")
    ap.add_argument("--max-searches", type=int, default=5, help="refuse if the plan needs more paid searches than this")
    ap.add_argument("--types", default="apartment,house", help="property types that count as usable")
    ap.add_argument("--min-price", type=float)
    ap.add_argument("--max-price", type=float)
    ap.add_argument("--operation", default="sale", choices=["sale", "rent"])
    ap.add_argument("--yes", action="store_true", help=f"allow more than {HARD_LIMIT} paid searches")
    args = ap.parse_args(argv)
    args.since_days = max(0, min(args.since_days, P.MAX_LOOKBACK_DAYS))
    args.pages = max(1, min(args.pages, P.MAX_PAGES_HARD))

    areas = len(args.places) + len(args.location_id)
    if not areas:
        ap.error("give at least one place name or --location-id")
    planned = areas * args.pages
    if planned > args.max_searches:
        print(f"Refusing: {planned} paid searches planned, --max-searches is {args.max_searches}.", file=sys.stderr)
        return 2
    if planned > HARD_LIMIT and not args.yes:
        print(f"Refusing: {planned} paid searches (more than {HARD_LIMIT}) needs --yes.", file=sys.stderr)
        return 2

    if args.mode:
        os.environ["CASAFARI_MODE"] = args.mode
        get_settings.cache_clear()
    cl = casafari.client()
    mode = get_settings().casafari_mode
    cfg = {**P.DEFAULTS, "property_types": [t.strip() for t in args.types.split(",") if t.strip()],
           "min_price": args.min_price, "max_price": args.max_price, "operation": args.operation}
    print(f"CASAFARI yield ({mode}), since {args.since_days} days, up to {planned} paid searches (≤ {planned * casafari.TOKENS_PER_SEARCH} tokens)")

    targets = [(lid, f"id {lid}") for lid in args.location_id]
    for name in args.places:
        loc = pick_location(cl, name)
        if not loc:
            print(f"\n== {name}: no Spanish location found")
            continue
        targets.append((loc["id"], f"{name} -> {loc['name']} ({loc.get('kind')})"))

    total = Counter()
    for lid, label in targets:
        try:
            r = area_report(cl, lid, label, args, cfg)
        except casafari.CasafariOutOfTokens as e:
            print(f"\n== {label}: out of tokens ({e.upgrade_url or 'no upgrade url'}). Stopping.")
            break
        except casafari.CasafariRateLimited as e:
            print(f"\n== {label}: rate limited, retry after {e.retry_after}s. Stopping.")
            break
        except casafari.CasafariError as e:
            print(f"\n== {label}: error {e.status}: {e}")
            continue
        print_report(r)
        total.update({"fetched": r["fetched"], "with_phone": r["with_phone"], "usable": r["usable"], "tokens": r["tokens"]})
    if len(targets) > 1 and total["fetched"]:
        eur = total["tokens"] * casafari.EUR_PER_TOKEN
        print(f"\n== total: {total['fetched']} fetched, {pct(total['with_phone'], total['fetched'])} with phone, "
              f"{total['usable']} usable, {total['tokens']} tokens (€{eur:.2f}), "
              f"{'€%.2f' % (eur / total['usable']) if total['usable'] else '-'} per usable lead")
    return 0


if __name__ == "__main__":
    sys.exit(main())
