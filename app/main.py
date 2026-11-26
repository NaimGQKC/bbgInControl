import logging
import threading
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI

from . import admin, engine, jobs, onboarding, prospecting, web, webhooks  # noqa: F401  (engine/onboarding/prospecting register job handlers)
from .config import get_settings
from .db import init_db, session_scope, utcnow

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
_stop = threading.Event()


def schedule_daily_jobs() -> None:
    with session_scope() as s:
        tomorrow = utcnow() + timedelta(days=1)
        jobs.schedule(s, "watchdog", utcnow() + timedelta(minutes=2), dedupe_key=f"watchdog:boot:{utcnow():%Y-%m-%d-%H-%M}")
        jobs.schedule(s, "phone_health", utcnow() + timedelta(minutes=5), dedupe_key=f"phone_health:{utcnow():%Y-%m-%d}")
        jobs.schedule(s, "phone_health", tomorrow, dedupe_key=f"phone_health:{tomorrow:%Y-%m-%d}")
        jobs.schedule(s, "google_check", utcnow() + timedelta(minutes=10), dedupe_key=f"google_check:{utcnow():%Y-%m-%d}")
        jobs.schedule(s, "google_check", tomorrow, dedupe_key=f"google_check:{tomorrow:%Y-%m-%d}")
        # Prospecting (opt-in, PROSPECTING_ENABLED): the scan queues one run per enabled account at its allowed hours
        jobs.schedule(s, "prospect_scan", utcnow() + timedelta(minutes=15), dedupe_key=f"prospect_scan:{utcnow():%Y-%m-%d}")
        jobs.schedule(s, "prospect_scan", tomorrow, dedupe_key=f"prospect_scan:{tomorrow:%Y-%m-%d}")
        # CASAFARI storage limit: always runs, even with prospecting off
        jobs.schedule(s, "source_cleanup", utcnow() + timedelta(minutes=20), dedupe_key=f"source_cleanup:{utcnow():%Y-%m-%d}")
        jobs.schedule(s, "source_cleanup", tomorrow, dedupe_key=f"source_cleanup:{tomorrow:%Y-%m-%d}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    schedule_daily_jobs()
    if get_settings().run_worker_in_web:
        threading.Thread(target=jobs.worker_loop, args=(_stop,), daemon=True).start()
    yield
    _stop.set()


app = FastAPI(title="Sparrow AI", lifespan=lifespan)
app.include_router(webhooks.router)
app.include_router(web.router)
app.include_router(admin.router)
