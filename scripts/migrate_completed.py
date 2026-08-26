"""Agrega la senal de reproduccion completa a play_history.

Hasta ahora `played_ms` solo se escribia desde /control/next y /control/prev:
un track que terminaba solo no dejaba rastro. El scoring de local_search
contaba `NOT skipped AND played_ms > 0` como "completos", asi que ese termino
valia siempre cero. El sistema sabia que odiabas, no que te gustaba.

Idempotente: se puede correr varias veces.

Uso:  python -m scripts.migrate_completed
"""
import asyncio
import logging

from app.db import close_pool, execute, fetchrow

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("migrate")

DDL = [
    """ALTER TABLE play_history
         ADD COLUMN IF NOT EXISTS completed boolean NOT NULL DEFAULT false""",

    # El scoring filtra por completed y agrupa por recording: indice parcial,
    # que las filas completas son minoria.
    """CREATE INDEX IF NOT EXISTS play_history_completed_idx
         ON play_history (recording_mbid) WHERE completed""",
]

# Rescate de la señal que si existe: un /control/next despues de 30s no marca
# skipped, asi que played_ms > 0 sin skip es un track escuchado en serio.
BACKFILL = """
UPDATE play_history
SET completed = true
WHERE NOT completed
  AND played_ms IS NOT NULL
  AND played_ms > 0
  AND skipped IS DISTINCT FROM true
"""


async def main() -> None:
    for ddl in DDL:
        await execute(ddl)
        log.info("ok: %s", " ".join(ddl.split())[:70])

    antes = await fetchrow(
        "SELECT count(*) AS total, "
        "       count(*) FILTER (WHERE completed) AS completos "
        "FROM play_history")
    log.info("antes del backfill: %d completos de %d filas",
             antes["completos"], antes["total"])

    res = await execute(BACKFILL)
    log.info("backfill: %s", res)

    desp = await fetchrow(
        "SELECT count(*) AS total, "
        "       count(*) FILTER (WHERE completed) AS completos, "
        "       count(*) FILTER (WHERE skipped) AS skips, "
        "       count(DISTINCT recording_mbid) FILTER (WHERE completed) AS recordings "
        "FROM play_history")

    log.info("despues: %d completos y %d skips sobre %d filas",
             desp["completos"], desp["skips"], desp["total"])
    log.info("%d recordings distintos con senal positiva", desp["recordings"])

    if desp["completos"] == 0:
        log.warning("no hay senal positiva todavia: se va a acumular sola "
                    "a medida que escuches playlists con el fix puesto")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        asyncio.run(close_pool())
