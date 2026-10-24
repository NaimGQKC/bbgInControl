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
    # Shown on the legal pages (a Spanish provider must publish them). Override with LEGAL_ENTITY / LEGAL_NIF / LEGAL_ADDRESS.
    legal_entity: str = "Alejandro Naim Monge Rouchdi (autónomo)"
    legal_nif: str = "73160865G"
    legal_address: str = "Maria Zambrano 21, Zaragoza (España)"
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

    # Caps on AI usage, so nobody can burn the token budget (see engine._ai_over_limit)
    ai_max_turns_per_lead_day: int = 20  # AI replies to one lead in 24 h; beyond it the chat goes to the agent
    ai_max_turns_per_lead_week: int = 60  # same, over 7 days
    ai_max_turns_per_account_day: int = 400  # all leads of one agent in 24 h (circuit breaker)
    ai_max_help_turns: int = 15  # messages to the onboarding help chat per agent (screenshots included)
    ai_max_user_chars: int = 1000  # longer lead messages are cut before they reach the model
    llm_max_output_tokens: int = 1200  # per model call (includes the model's hidden reasoning)

    # Timing (seconds)
    first_message_delay: int = 30
    reply_delay: int = 30
    reply_max_delay: int = 60


@lru_cache
def get_settings() -> Settings:
    return Settings()
