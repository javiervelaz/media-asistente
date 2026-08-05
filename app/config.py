"""Configuración cargada desde .env"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    api_key: str
    anthropic_api_key: str
    claude_model: str = "claude-haiku-4-5-20251001"
    mpv_socket: str = "/tmp/mpvsocket"
    log_level: str = "INFO"

    # --- v2 ---
    database_url: str
    curator_model: str = "claude-sonnet-4-6"
    curator_enabled: bool = True
    local_search_enabled: bool = False
    mb_user_agent: str = "Charly/1.0 ( javiervelaz@hotmail.com.com )"


settings = Settings()