"""Orquestador de un turno: rutear -> ejecutar -> renderizar -> loguear.

Un solo camino, sin agent loop. La implementacion obvia del harness seria un
agent loop conversacional con el curador como tool; esa es la cara. Dos agent
loops anidados significan que Sonnet gasta tokens para decidir que hay que
gastar tokens, y el costo queda repartido entre dos sesiones que no se pueden
atribuir por separado.

La unica decision de gasto del modulo esta en `_resolver_fallback`, y queda
escrita en turn_log.
"""
import logging

from app.config import settings
from app.harness import executors, session
from app.harness.intents import FALLBACK, Intent, Result
from app.harness.router import normalizar, rutear
from app.harness.telemetry import Cronometro, log_turn

logger = logging.getLogger(__name__)


def _resolver_fallback(intent: Intent, text: str) -> Intent:
    """Que hacer con lo que el router no entendio.

    Con `harness_fallback_playlist=True` (default) se mantiene el
    comportamiento que ya tiene el bot de Telegram: todo texto libre arma una
    playlist. La diferencia es que ahora queda registrado como fallback, con
    su costo, en vez de ser gasto invisible.
    """
    if intent.name != "no_entendido":
        return intent
    if not settings.harness_fallback_playlist:
        return intent

    # Guarda de longitud. "hola", "gracias", "ok", un typo: nada de eso es un
    # pedido de musica, pero todos cuestan lo mismo que uno. Repreguntar sale
    # cero y el usuario confirma en un turno.
    palabras = len(normalizar(text).split())
    if palabras < settings.harness_min_palabras_playlist:
        return Intent(name="repreguntar", slots={"texto": text.strip()},
                      confidence=0.0, stage=FALLBACK)

    return Intent(name="playlist", slots={"prompt": text},
                  confidence=0.0, stage=FALLBACK)


def _uso(res: Result) -> dict:
    u = (res.data or {}).get("usage") or {}
    return {
        "input_tokens": int(u.get("in") or 0),
        "cached_tokens": int(u.get("cache_read") or 0),
        "output_tokens": int(u.get("out") or 0),
    }


async def responder(text: str, session_id: str, room_id: str = "main") -> dict:
    st = session.get(session_id, room_id)
    st.room_id = room_id or st.room_id

    with Cronometro() as c:
        intent = _resolver_fallback(rutear(text), text)
        res: Result = await executors.ejecutar(intent, st)

    uso = _uso(res)
    gasto = intent.name == "playlist"
    modelo = (settings.curator_model if gasto and settings.curator_enabled
              else settings.claude_model if gasto else None)

    log_turn(session_id=session_id, room_id=room_id, text_in=text,
             intent=intent.name, stage=intent.stage,
             confidence=intent.confidence,
             model=modelo, latency_ms=c.ms, ok=res.ok, **uso)

    total = uso["input_tokens"] + uso["output_tokens"]
    return {
        "reply": res.reply,
        "intent": intent.name,
        "stage": intent.stage,
        "confidence": intent.confidence,
        "ok": res.ok,
        "free": modelo is None,
        "cost_tokens": total,
        "latency_ms": c.ms,
        "actions": res.actions,
    }
