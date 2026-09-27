from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


FROZEN_NOW: datetime | None = None  # tests can freeze/advance the clock


def utcnow() -> datetime:
    """All datetimes in the DB are naive UTC."""
    if FROZEN_NOW is not None:
        return FROZEN_NOW
    return datetime.now(timezone.utc).replace(tzinfo=None)


_engine = None
_Session = None


def _url(url: str) -> str:
    # Supabase / Railway give "postgres://" or "postgresql://"; use the psycopg 3 driver.
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def get_engine():
    global _engine, _Session
    if _engine is None:
        url = _url(get_settings().database_url)
        kwargs = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        _engine = create_engine(url, **kwargs)
        _Session = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def reset_engine():
    """Used by tests to point at a fresh database."""
    global _engine, _Session
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _Session = None


def is_postgres() -> bool:
    return get_engine().dialect.name == "postgresql"


def init_db():
    from . import models  # noqa: F401  (register tables)

    Base.metadata.create_all(get_engine())


@contextmanager
def session_scope():
    get_engine()
    s = _Session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
