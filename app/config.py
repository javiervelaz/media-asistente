"""Configuración cargada desde .env"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    api_key: str
    anthropic_api_key: str
    claude_model: str = "claude-haiku-4-5-20251001"
    mpv_socket: str = "/tmp/mpvsocket"
    log_level: str = "INFO"


settings = Settings()