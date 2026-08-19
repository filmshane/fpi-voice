from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "127.0.0.1"
    port: int = 8792
    crm_db_path: str = "/home/shanem/FPI-Corp/CRM/fpi_crm.db"
    public_base_url: str = "https://firstpropertyinvestment.us"
    company_name: str = "First Property Investment"
    company_website: str = "http://firstpropertyinvestment.us/"
    service_area: str = "Chattanooga TN / Cleveland TN"
    contact_email: str = "shane.a.miller@live.com"

    retell_api_key: str = ""
    retell_agent_id: str = "agent_deaec073f1969cc0341dbfa620"
    retell_from_number: str = ""
    retell_api_base: str = "https://api.retellai.com"

    # ElevenLabs (Alex primary)
    elevenlabs_api_key: str = ""
    elevenlabs_agent_id: str = ""
    elevenlabs_agent_phone_number_id: str = ""
    elevenlabs_webhook_secret: str = ""
    elevenlabs_webhook_enforce_signature: bool = True
    elevenlabs_api_base: str = "https://api.elevenlabs.io"

    llm_base_url: str = "http://127.0.0.1:8645/v1"
    llm_api_key: str = "hermes-proxy"
    llm_model: str = "grok-4.20-reasoning"

    # Optional shared secret for manual dispatch endpoints
    dispatch_secret: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
