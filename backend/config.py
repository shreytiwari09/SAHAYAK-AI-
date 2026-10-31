from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    gemini_api_key: str = ""
    # Best model first. Comma-separated; empty means the built-in default cascade.
    model_cascade: str = ""
    llm_attempt_timeout_seconds: float = 8.0
    llm_deadline_seconds: float = 60.0

    # Dev defaults are SQLite files so the agent runs without Docker; set these to Postgres URLs
    # (postgresql+psycopg://... for audit, postgresql://... for checkpoints) for the full stack.
    audit_database_url: str = "sqlite:///data/audit.db"
    checkpoint_url: str = "data/checkpoints.db"

    # WhatsApp (Meta Cloud API) and speech (Sarvam). Empty values mean: not configured.
    meta_wa_token: str = ""
    meta_phone_id: str = ""
    meta_verify_token: str = "sahayak-verify"
    meta_app_secret: str = ""  # when set, webhook signatures (X-Hub-Signature-256) are enforced
    whatsapp_default_merchant: str = ""  # merchant for numbers that are not linked
    whatsapp_links: str = ""  # "919876543210:M1001,919812345678:M1002"
    # The address phones can reach this server on (e.g. a Cloudflare tunnel URL). Links the agent
    # sends, such as the KYC link, are built from it, and the mock Paytm is served under /mock.
    # When set, requests from outside this machine must present it (see backend/access.py).
    access_key: str = ""
    public_base_url: str = ""
    voice_replies: bool = True
    sarvam_api_key: str = ""
    sarvam_tts_model: str = "bulbul:v4-flash"
    sarvam_stt_model: str = "saaras:v4"
    sarvam_speaker: str = ""
    pause_poll_seconds: float = 3.0
    data_dir: str = "data"
    agents_file: str = "agents.yaml"  # which AI rep looks after which merchants

    # Seconds between automatic proactive scans; 0 means only when asked (POST /monitor/scan).
    monitor_interval_seconds: float = 0.0
    fake_llm: bool = False
    inprocess_mock: bool = False

    backend_database_url: str = "postgresql+psycopg://sahayak:sahayak@localhost:5432/sahayak"
    mock_paytm_base_url: str = "http://localhost:8001"
    tool_timeout_seconds: float = 10.0
    # Defaults to backend/gate/policy.yaml next to the gate code.
    policy_path: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
