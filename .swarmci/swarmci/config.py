from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    data_dir: Path = Path("data")
    artifact_dir: Path = Path("artifacts")
    bu_base_url: str = "http://localhost:8001/v1"
    bu_model: str = "browser-use/bu-30b-a3b-preview"
    bu_api_key: str = "local"
    gemma_base_url: str = "http://localhost:8002/v1"
    gemma_model: str = "google/gemma-3-12b-it"
    gemma_api_key: str = "local"
    lambda_api_key: str = ""
    browser_use_api_key: str = ""
    nango_secret_key: str = ""
    nango_connection_id: str = ""
    nango_provider_config_key: str = "github"
    agentmail_api_key: str = ""
    respan_api_key: str = ""
    respan_enabled: bool = True
    respan_base_url: str = "https://api.respan.ai/api"
    respan_endpoint: str = ""  # Backwards-compatible full endpoint override.
    headless: bool = True
    ray_address: str = "auto"
    fixture_url: str = ""
    execution: str = "local"


settings = Settings()
