"""Salida por Telegram desde la Pi, para lo que no entra en un request.

`/chat` vive detras de Cloudflare, que corta a los 120 s. Una playlist del
curador tarda ~180 s. Cuando un turno no termina a tiempo, la respuesta no
puede volver por el request: sale por aca, directo a la API de Telegram, sin
pasar por Cloudflare ni por n8n.

Best-effort, igual que telemetry: si Telegram falla, la musica suena igual.
"""
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


def chat_id_de(session_id: str) -> str | None:
    """`tg:937324746` -> `937324746`. Otro transporte -> None."""
    if session_id and session_id.startswith("tg:"):
        return session_id[3:] or None
    return None


async def telegram(session_id: str, texto: str) -> bool:
    chat_id = chat_id_de(session_id)
    if chat_id is None:
        logger.info("respuesta tardia sin transporte para %s: %s",
                    session_id, texto[:120])
        return False
    if not settings.telegram_bot_token:
        logger.warning("respuesta tardia para %s perdida: falta "
                       "TELEGRAM_BOT_TOKEN en el .env", session_id)
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}"
                "/sendMessage",
                json={"chat_id": chat_id, "text": texto})
            r.raise_for_status()
        return True
    except Exception:
        logger.exception("no pude mandar la respuesta tardia a telegram "
                         "(chat_id=%s)", chat_id)
        return False
