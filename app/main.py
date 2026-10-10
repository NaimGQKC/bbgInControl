import logging
import threading
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI

from . import admin, engine, jobs, onboarding, web, webhooks  # noqa: F401  (engine/onboarding register job handlers)
from .config import get_settings
from .db import init_db, session_scope, utcnow

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
_stop = threading.Event()


def schedule_daily_jobs() -> None:
    with session_scope() as s:
        tomorrow = utcnow() + timedelta(days=1)
        jobs.schedule(s, "phone_health", utcnow() + timedelta(minutes=5), dedupe_key=f"phone_health:{utcnow():%Y-%m-%d}")
        jobs.schedule(s, "phone_health", tomorrow, dedupe_key=f"phone_health:{tomorrow:%Y-%m-%d}")
        jobs.schedule(s, "google_check", utcnow() + timedelta(minutes=10), dedupe_key=f"google_check:{utcnow():%Y-%m-%d}")
        jobs.schedule(s, "google_check", tomorrow, dedupe_key=f"google_check:{tomorrow:%Y-%m-%d}")


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
