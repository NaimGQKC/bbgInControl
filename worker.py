"""Run the job worker as its own process (optional; by default the web process runs it: RUN_WORKER_IN_WEB=true)."""
from app import engine, onboarding  # noqa: F401  (register job handlers)
from app.db import init_db
from app.jobs import worker_loop
from app.main import schedule_daily_jobs

if __name__ == "__main__":
    init_db()
    schedule_daily_jobs()
    worker_loop()
