"""Orquestador de un turno: rutear -> ejecutar -> renderizar -> loguear.

Un solo camino, sin agent loop. La implementacion obvia del harness seria un
agent loop conversacional con el curador como tool; esa es la cara. Dos agent
loops anidados significan que Sonnet gasta tokens para decidir que hay que
gastar tokens, y el costo queda repartido entre dos sesiones que no se pueden
atribuir por separado.

Las dos unicas decisiones de gasto del modulo estan en `_resolver_fallback` y
`_freno_de_gasto`, y las dos quedan escritas en turn_log.
"""
import logging

from app.config import settings
from app.harness import clasificador, executors, queries, render, session
from app.harness.intents import (ARTISTA, FALLBACK, HAIKU, IMPLEMENTADOS,
                                 Intent, Result)
from app import local_search
from app.harness.router import etapa1, normalizar, sin_verbo
from app.harness.telemetry import Cronometro, log_turn

logger = logging.getLogger(__name__)


async def _rutear(text: str) -> tuple[Intent, dict]:
    """Patrones → clasificador → fallback. Devuelve (intent, uso).

    El orden es el costo: la etapa 1 es gratis y resuelve la mayoría, la 2
    cuesta unos cientos de tokens y solo corre si la 1 no entendió.
    """
    it = etapa1(text)
    if it is not None:
        return it, {}

    # Etapa 1.5: el mensaje es el nombre de un artista y nada mas.
    # "Ataque 77", "Rolling stones", "arctick monkeys" aparecieron asi en
    # turn_log y pagaban clasificacion. Preguntarle a la base si ese texto
    # es un artista cuesta una query y cero tokens — y es mas confiable que
    # el modelo para nombres con typos, porque el trigram los tolera.
    try:
        if await local_search.es_monografico(text):
            return Intent(name="playlist", slots={"prompt": sin_verbo(text)},
                          confidence=1.0, stage=ARTISTA), {}
    except Exception:
        logger.exception("no pude chequear si el texto es un artista")

    if not settings.harness_clasificador:
        return Intent(name="no_entendido", slots={}, confidence=0.0,
                      stage=FALLBACK), {}

    it, uso = await clasificador.clasificar(text)
    if it is None:
        # La API falló o tardó: el turno sigue por el fallback, que es
        # exactamente lo que pasaba antes de que existiera la etapa 2.
        return Intent(name="no_entendido", slots={}, confidence=0.0,
                      stage=FALLBACK), uso

    if it.name == "no_entendido" or it.confidence < settings.harness_confianza_minima:
        # Repreguntar cuesta cero; adivinar mal cuesta una playlist que nadie
        # pidió. El stage queda en HAIKU para que turn_log muestre que se
        # pagó por no decidir — es la señal de que falta un patrón.
        return Intent(name="repreguntar", slots={"texto": text.strip()},
                      confidence=it.confidence, stage=HAIKU), uso

    if it.name not in IMPLEMENTADOS:
        logger.warning("el clasificador devolvió %s, sin ejecutor", it.name)
        return Intent(name="repreguntar", slots={"texto": text.strip()},
                      confidence=it.confidence, stage=HAIKU), uso

    return it, uso


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


async def _freno_de_gasto(intent: Intent, st, text: str) -> Result | None:
    """Avisa el costo y pide confirmacion antes de llamar al curador.

    Devuelve un Result si hay que frenar; None si el turno sigue de largo.

    El default (`fallback`) frena solo lo que el router NO entendio: un
    "arma una playlist de post-punk" explicito es gasto intencional y pedirle
    permiso cada vez seria molesto. Lo que hay que frenar es el gasto que el
    usuario no pidio — que es exactamente el que no se ve.
    """
    modo = settings.harness_confirmar_gasto
    if intent.name != "playlist" or modo == "nunca":
        return None
    entendido = intent.stage != FALLBACK
    if modo == "fallback" and entendido:
        return None

    tokens = await queries.costo_tipico()
    st.ofrecer("playlist", "la playlist", prompt=intent.slots.get("prompt", text))
    return Result(render.confirmar_gasto(text.strip(), tokens, entendido),
                  ok=True, data={"frenado": True, "estimado": tokens})


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
        ruteado, uso_router = await _rutear(text)
        intent = _resolver_fallback(ruteado, text)
        freno = await _freno_de_gasto(intent, st, text)
        if freno is not None:
            res, intent = freno, Intent(name="confirmar_gasto",
                                        slots={}, confidence=intent.confidence,
                                        stage=intent.stage)
        else:
            res = await executors.ejecutar(intent, st)

    uso = _uso(res)
    # Lo que costó CLASIFICAR se suma a lo que costó ejecutar: el turno vale
    # lo que vale entero, no solo su parte cara.
    uso["input_tokens"] += int(uso_router.get("in") or 0)
    uso["output_tokens"] += int(uso_router.get("out") or 0)
    uso["cached_tokens"] += int(uso_router.get("cache_read") or 0)
    # `gasto` lo marca el ejecutor de playlist. No alcanza con el nombre del
    # intent (por una confirmacion el ruteado es `confirmar`) ni con los
    # tokens (la via local devuelve 0 y sigue siendo una playlist).
    gasto = bool((res.data or {}).get("gasto"))
    clasificado = bool(uso_router.get("in"))
    modelo = (settings.curator_model if gasto and settings.curator_enabled
              else settings.claude_model if (gasto or clasificado) else None)

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
