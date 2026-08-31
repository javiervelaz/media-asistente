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
    # Proporcion maxima de tracks sin respaldo en un tool result (0..1).
    # 1.0 = solo medir, no recortar. 0.0 = estricto, solo verificados.
    curator_max_libres: float = 0.2
    # --- harness conversacional ---
    # Que hacer con un turno que el router no entiende. True = va al
    # curador, que es lo que hace hoy el bot de Telegram con todo texto
    # libre. Queda logueado en turn_log con model=curator, asi que el
    # costo del fallback deja de ser invisible. Ponelo en False cuando
    # la etapa 1 cubra lo suficiente como para que repreguntar salga
    # mas barato que adivinar.
    harness_fallback_playlist: bool = True
    harness_n_tracks: int = 14
    # Palabras minimas para mandar un texto no reconocido al curador. Un
    # mensaje de una sola palabra que no matcheo ningun patron casi nunca es
    # un pedido curatorial, y siempre cuesta lo mismo que uno real.
    harness_min_palabras_playlist: int = 2
    # "hoy" es hoy DONDE ESTA EL USUARIO. Si el proceso corre en UTC, a las
    # 21:30 de Cordoba ya es manana en UTC y "que escuche hoy" devuelve vacio
    # justo en el horario en que mas se usa el reproductor.
    harness_tz: str = "America/Argentina/Cordoba"
    # Cuando un turno va a terminar en el curador, avisar el costo y pedir
    # confirmacion en vez de gastar de una.
    #   "fallback" (default) — solo cuando el router NO entendio el pedido.
    #                          Un "arma una playlist de X" explicito es gasto
    #                          intencional y no necesita permiso.
    #   "siempre"            — confirma todo lo que gaste.
    #   "nunca"              — comportamiento historico.
    harness_confirmar_gasto: str = "fallback"

    mb_user_agent: str = "Charly/1.0 ( javiervelaz@hotmail.com )"


settings = Settings()