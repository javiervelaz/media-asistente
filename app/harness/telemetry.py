"""turn_log: una fila por turno, siempre.

Se escribe en background: el logueo no puede sumarle latencia a un "pausa".
Si Neon esta lento o caido, el turno igual se responde — la telemetria es
best-effort, el reproductor no.
"""
import asyncio
import logging
import time

from app.db import execute

logger = logging.getLogger(__name__)

_tasks: set = set()

# `cache_write_tokens` se agrego el 01/10 porque faltaba la columna mas
# cara del sistema. El curador ya la medía y la imprimía en el log
# ("cache_r:25483 cache_w:16280") pero nunca llegaba a la base: una
# escritura de cache cuesta 1,25x el input, y en dos semanas fueron ~USD
# 1.14 invisibles, mas que todo lo que el turn_log reportaba.
#
# Es el modo de falla de siempre: no estaba roto, medía de menos, y una
# medicion que no incluye su linea mas grande es peor que no tener ninguna
# porque da una respuesta y suena convincente.
INSERT = """
INSERT INTO turn_log
    (session_id, room_id, text_in, intent, stage, confidence,
     model, input_tokens, cached_tokens, cache_write_tokens, output_tokens,
     latency_ms, ok)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
"""

#: Techo defensivo: un transporte roto puede mandar un mensaje de 40 KB y no
#: hay razon para guardarlo entero.
MAX_TEXT = 500


async def _escribir(args: tuple) -> None:
    try:
        await execute(INSERT, *args)
    except Exception:
        logger.exception("no pude escribir turn_log")


def log_turn(*, session_id: str, room_id: str | None, text_in: str,
             intent: str | None, stage: str, confidence: float | None = None,
             model: str | None = None, input_tokens: int = 0,
             cached_tokens: int = 0, cache_write_tokens: int = 0,
             output_tokens: int = 0,
             latency_ms: int | None = None, ok: bool = True) -> None:
    """Fire-and-forget. `model=None` es la definicion de turno gratis."""
    args = (session_id, room_id, (text_in or "")[:MAX_TEXT], intent, stage,
            confidence, model, input_tokens, cached_tokens,
            cache_write_tokens, output_tokens, latency_ms, ok)
    try:
        t = asyncio.create_task(_escribir(args))
        _tasks.add(t)
        t.add_done_callback(_tasks.discard)
    except RuntimeError:
        # sin loop corriendo (tests sincronicos): no es motivo para romper
        logger.debug("log_turn sin event loop; turno no registrado")


class Cronometro:
    """`with Cronometro() as c: ...` -> c.ms"""
    __slots__ = ("_t0", "ms")

    def __enter__(self):
        self._t0 = time.monotonic()
        self.ms = 0
        return self

    def __exit__(self, *exc):
        self.ms = int((time.monotonic() - self._t0) * 1000)
        return False
