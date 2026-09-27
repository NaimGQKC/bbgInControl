"""Data model (design doc section 9). One `Account` = one real estate agent."""
import secrets
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, utcnow


def _token() -> str:
    return secrets.token_urlsafe(24)


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    agent_name: Mapped[str] = mapped_column(String(200), default="")
    agency: Mapped[str] = mapped_column(String(200), default="")
    city: Mapped[str] = mapped_column(String(120), default="")
    zones: Mapped[str] = mapped_column(Text, default="")
    email: Mapped[str] = mapped_column(String(200), default="")
    personal_whatsapp: Mapped[str] = mapped_column(String(32), default="")  # where "needs you" alerts go
    extra_context: Mapped[str] = mapped_column(Text, default="")  # optional free text for the AI
    status: Mapped[str] = mapped_column(String(20), default="onboarding")  # onboarding|live|paused
    onboarding_step: Mapped[str] = mapped_column(String(40), default="questions")
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Madrid")
    public_token: Mapped[str] = mapped_column(String(64), default=_token, unique=True)  # onboarding link
    intake_key: Mapped[str] = mapped_column(String(64), default=_token, unique=True)  # lead intake webhook key
    last_agent_activity_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    phone_inactive_alerted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Integration(Base):
    __tablename__ = "integrations"
    __table_args__ = (UniqueConstraint("account_id", "provider"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    provider: Mapped[str] = mapped_column(String(20))  # meta|google|lystos
    external: Mapped[dict] = mapped_column(JSON, default=dict)  # waba_id, phone_number_id, calendar_id...
    token_enc: Mapped[str] = mapped_column(Text, default="")
    refresh_token_enc: Mapped[str] = mapped_column(Text, default="")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Lead(Base):
    __tablename__ = "leads"
    __table_args__ = (UniqueConstraint("account_id", "phone"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    source: Mapped[str] = mapped_column(String(40), default="manual")
    source_ref: Mapped[str] = mapped_column(String(200), default="")
    name: Mapped[str] = mapped_column(String(200), default="")
    phone: Mapped[str] = mapped_column(String(32))  # E.164, e.g. +34600000001
    lead_type: Mapped[str] = mapped_column(String(20), default="seller")  # seller|buyer|unknown
    property_type: Mapped[str] = mapped_column(String(60), default="")
    zone: Mapped[str] = mapped_column(String(200), default="")
    address: Mapped[str] = mapped_column(String(300), default="")
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    listing_url: Mapped[str] = mapped_column(String(500), default="")
    stage: Mapped[str] = mapped_column(String(60), default="")  # CRM stage (exclusiva, cerrado...)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(30), default="new")
    do_not_contact: Mapped[bool] = mapped_column(Boolean, default=False)
    human_owned: Mapped[bool] = mapped_column(Boolean, default=False)  # agent took over (J5)
    has_replied: Mapped[bool] = mapped_column(Boolean, default=False)
    introduced: Mapped[bool] = mapped_column(Boolean, default=False)  # AI identity already stated (G13)
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    burst_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    facts: Mapped[dict] = mapped_column(JSON, default=dict)
    offered_slots: Mapped[list] = mapped_column(JSON, default=list)
    unclear_count: Mapped[int] = mapped_column(Integer, default=0)
    is_test: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True, index=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"), nullable=True, index=True)
    direction: Mapped[str] = mapped_column(String(4))  # in|out
    sender: Mapped[str] = mapped_column(String(10))  # lead|ai|agent|system
    type: Mapped[str] = mapped_column(String(20), default="text")  # text|template|audio|image...
    text: Mapped[str] = mapped_column(Text, default="")
    template_name: Mapped[str] = mapped_column(String(100), default="")
    wa_message_id: Mapped[str | None] = mapped_column(String(200), unique=True, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(20))
    external_id: Mapped[str] = mapped_column(String(300), unique=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(40), index=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"), nullable=True, index=True)
    run_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(200), unique=True, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Meeting(Base):
    __tablename__ = "meetings"
    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), unique=True)  # G5: one booking per lead
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    start: Mapped[datetime] = mapped_column(DateTime)
    end: Mapped[datetime] = mapped_column(DateTime)
    type: Mapped[str] = mapped_column(String(10), default="visit")  # visit|call
    address: Mapped[str] = mapped_column(String(300), default="")
    google_event_id: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(20), default="booked")
    is_test: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LlmTurn(Base):
    __tablename__ = "llm_turns"
    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), index=True)
    model: Mapped[str] = mapped_column(String(60), default="")
    transcript: Mapped[list] = mapped_column(JSON, default=list)  # every request/response/tool call
    reply: Mapped[str] = mapped_column(Text, default="")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Event(Base):
    """Funnel log: lead_received, first_message_sent, replied, slots_offered, meeting_booked, handoff..."""

    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True, index=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(60), index=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
