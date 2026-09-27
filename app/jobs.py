"""Tiny job queue on top of Postgres (the `jobs` table). Every outgoing message and scheduled action is a row."""
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy import select

from .db import is_postgres, session_scope, utcnow
from .models import Job

log = logging.getLogger("sparrow.jobs")

HANDLERS: dict[str, Callable] = {}
_sqlite_lock = threading.Lock()


def handler(job_type: str):
    def deco(fn):
        HANDLERS[job_type] = fn
        return fn

    return deco


def schedule(
    s,
    job_type: str,
    run_at: datetime,
    *,
    lead_id: int | None = None,
    account_id: int | None = None,
    dedupe_key: str | None = None,
    payload: dict | None = None,
    reuse: bool = False,
    replace_run_at: bool = False,
) -> Job | None:
    """dedupe_key makes scheduling idempotent (G3/G4).
    reuse=True: a finished job with the same key is re-armed (e.g. one 'ai_turn' row per lead).
    replace_run_at=True: a pending job with the same key gets the new run_at (burst grouping)."""
    if dedupe_key:
        existing = s.execute(select(Job).where(Job.dedupe_key == dedupe_key)).scalar_one_or_none()
        if existing:
            if existing.status == "pending":
                if replace_run_at:
                    existing.run_at = run_at
                return existing
            if existing.status == "running" and reuse:
                # A turn is running now: run again right after it (new message arrived meanwhile)
                existing.payload = {**(existing.payload or {}), "rerun_at": run_at.isoformat()}
                return existing
            if reuse:
                existing.status = "pending"
                existing.run_at = run_at
                existing.attempts = 0
                existing.last_error = ""
                existing.payload = payload or {}
                return existing
            return None
    job = Job(type=job_type, run_at=run_at, lead_id=lead_id, account_id=account_id, dedupe_key=dedupe_key, payload=payload or {})
    s.add(job)
    s.flush()
    return job


def cancel(s, lead_id: int, types: list[str]) -> int:
    jobs = s.execute(select(Job).where(Job.lead_id == lead_id, Job.type.in_(types), Job.status == "pending")).scalars().all()
    for j in jobs:
        j.status = "cancelled"
    return len(jobs)


STUCK_AFTER = timedelta(minutes=10)


def _claim(limit: int = 5) -> list[int]:
    with session_scope() as s:
        # Jobs left "running" by a crash/deploy go back to the queue
        for j in s.execute(select(Job).where(Job.status == "running", Job.claimed_at < utcnow() - STUCK_AFTER)).scalars():
            j.status = "pending"
        s.flush()
        q = select(Job).where(Job.status == "pending", Job.run_at <= utcnow()).order_by(Job.run_at).limit(limit)
        if is_postgres():
            q = q.with_for_update(skip_locked=True)
        jobs = s.execute(q).scalars().all()
        for j in jobs:
            j.status = "running"
            j.claimed_at = utcnow()
            j.attempts += 1
        return [j.id for j in jobs]


def run_job(job_id: int) -> None:
    with session_scope() as s:
        job = s.get(Job, job_id)
        fn = HANDLERS.get(job.type)
        if not fn:
            job.status = "failed"
            job.last_error = f"no handler for {job.type}"
            return
        try:
            fn(s, job)
            s.flush()
            s.refresh(job, ["payload"])  # a webhook may have asked for a re-run meanwhile (other session)
            if job.status == "running":
                rerun = (job.payload or {}).get("rerun_at")
                if rerun:
                    job.status = "pending"
                    job.run_at = datetime.fromisoformat(rerun)
                    job.payload = {k: v for k, v in job.payload.items() if k != "rerun_at"}
                else:
                    job.status = "done"
        except Exception as e:
            log.exception("job %s (%s) failed", job.id, job.type)
            s.rollback()
            job = s.get(Job, job_id)
            job.last_error = repr(e)[:2000]
            if job.attempts < 3:
                job.status = "pending"
                job.run_at = utcnow() + timedelta(seconds=30 * job.attempts)
            else:
                job.status = "failed"
                on_fail = HANDLERS.get(job.type + ":failed")
                if on_fail:
                    try:
                        on_fail(s, job)
                    except Exception:
                        log.exception("failure handler crashed")


def run_due(limit: int = 10) -> int:
    if not is_postgres():
        with _sqlite_lock:
            ids = _claim(limit)
            for i in ids:
                run_job(i)
            return len(ids)
    ids = _claim(limit)
    for i in ids:
        run_job(i)
    return len(ids)


def run_until_idle(max_rounds: int = 50, advance_to: datetime | None = None) -> None:
    """Tests: run everything that is due now."""
    for _ in range(max_rounds):
        if run_due(50) == 0:
            return


def worker_loop(stop: threading.Event | None = None, interval: float = 2.0) -> None:
    log.info("worker started")
    from .webhooks import sweep_unprocessed

    while not (stop and stop.is_set()):
        try:
            sweep_unprocessed()
            if run_due() == 0:
                time.sleep(interval)
        except Exception:
            log.exception("worker loop error")
            time.sleep(5)
