"""All configuration comes from environment variables (never hardcode Meta IDs, tokens or numbers)."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # General
    base_url: str = "http://localhost:8000"  # public URL (used for OAuth redirects)
    database_url: str = "sqlite:///./sparrow.db"
    timezone: str = "Europe/Madrid"
    admin_user: str = "admin"
    admin_password: str = "change-me"
    session_secret: str = "change-me-too"
    contact_email: str = ""  # shown on the privacy / data deletion pages
    legal_entity: str = ""  # name shown on the legal pages (e.g. the autónomo's name); optional
    legal_nif: str = ""  # tax ID shown on the legal pages; optional
    legal_address: str = ""  # postal address shown on the legal pages; optional
    assistant_name: str = "Lucía"  # the named (and openly AI) guide agents talk to during onboarding
    ops_whatsapp: str = ""  # your own mobile: gets a WhatsApp when an agent presses "necesito ayuda"
    # Fernet key used to encrypt per-agent tokens in the DB (python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
    encryption_key: str = ""

    # Modes: "live" talks to the real APIs, "fake" keeps everything in memory (tests / local demo)
    whatsapp_mode: str = "live"
    calendar_mode: str = "live"
    llm_mode: str = "live"

    # Meta / WhatsApp Cloud API
    meta_app_id: str = ""
    meta_app_secret: str = ""
    meta_config_id: str = ""  # Embedded Signup configuration id (Facebook Login for Business)
    meta_verify_token: str = "sparrow-verify"  # the string you type in the webhook settings
    graph_version: str = "v23.0"
    # Dev / pilot fallback: the test number (used when an account has no Meta integration yet)
    wa_phone_number_id: str = ""
    wa_waba_id: str = ""
    wa_token: str = ""
    # The Sparrow number: sends "needs you" alerts to agents
    sparrow_phone_number_id: str = ""
    sparrow_wa_token: str = ""
    sparrow_waba_id: str = ""
    run_worker_in_web: bool = True  # one web app runs both the web server and the job worker
    template_language: str = "es"

    # Pricing (shown in the onboarding chat) and Stripe. No STRIPE_PAYMENT_LINK = the payment step is skipped (free pilots).
    integration_fee_eur: int = 90  # one-time, per agent
    meeting_fee_eur: int = 30  # per booked meeting
    stripe_payment_link: str = ""  # a Stripe Payment Link (https://buy.stripe.com/...)
    stripe_webhook_secret: str = ""  # whsec_... of the webhook that sends checkout.session.completed to /webhooks/stripe

    # Google Calendar
    google_client_id: str = ""
    google_client_secret: str = ""

    # The AI model. "azure_openai" (covered by Microsoft for Startups credits), "openai" or "anthropic"
    llm_provider: str = "azure_openai"
    azure_openai_llm_deployment: str = "gpt-5.4"  # the deployment name you chose in Azure AI Foundry
    llm_reasoning_effort: str = "low"  # for GPT-5 family models: low = fast replies. Empty for gpt-4o/4.1
    openai_model: str = "gpt-5.4"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"

    # OpenAI key: used for the AI when LLM_PROVIDER=openai, and for voice notes (optional: without it the AI asks the lead to write)
    openai_api_key: str = ""
    # ...or Azure OpenAI (covered by Microsoft for Startups credits): a whisper / gpt-4o-transcribe deployment
    azure_openai_endpoint: str = ""  # https://<resource>.openai.azure.com
    azure_openai_key: str = ""
    azure_transcribe_deployment: str = ""

    # Timing (seconds)
    first_message_delay: int = 30
    reply_delay: int = 30
    reply_max_delay: int = 60


@lru_cache
def get_settings() -> Settings:
    return Settings()
