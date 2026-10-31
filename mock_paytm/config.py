from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    mock_paytm_database_url: str = "postgresql+psycopg://sahayak:sahayak@localhost:5432/mock_paytm"
    mock_paytm_public_url: str = "http://localhost:8001"
    settlement_processing_seconds: int = 10
    refund_processing_seconds: int = 10
    heartbeat_timeout_seconds: int = 300
    seed_merchants: int = 50
    seed_days: int = 30
    seed_random: int = 42


@lru_cache
def get_settings() -> Settings:
    return Settings()
