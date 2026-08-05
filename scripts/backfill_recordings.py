#!/usr/bin/env python3
"""Backfill de recordings: trae tracklists desde MusicBrainz para los
releases ya cargados.

El grafo se hidrata de forma perezosa y hoy tiene ~300 recordings contra
8651 releases, así que local_search no encuentra material. Esto lo llena
de una pasada.

Rate limit: 1 req/s (lo aplica MBClient con lock propio). ~2h para todo.
Es interrumpible: el checkpoint vive en la DB, no en un archivo.

Uso:
    source .venv/bin/activate
    python scripts/backfill_recordings.py --limit 20      # prueba
    nohup python scripts/backfill_recordings.py > /tmp/backfill.log 2>&1 &
    tail -f /tmp/backfill.log
"""
import argparse
import asyncio
import logging
import sys
import time

from app.db import close_pool, fetch, fetchval, init_pool
from app.hydrator import hydrate_recordings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("backfill")
logging.getLogger("app.musicbrainz").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)


# Prioridad: primero los vinilos, después el canon, después lo que ya
# escuchaste, al final el resto. Los primeros 15 minutos tienen que servir.
PENDIENTES_SQL = """
WITH peso AS (
    SELECT lower(artist) AS nombre, min(weight) AS weight
    FROM ephemerides GROUP BY 1
),
escuchados AS (
    SELECT DISTINCT artist_mbid FROM play_history WHERE artist_mbid IS NOT NULL
)
SELECT rl.mbid::text AS mbid,
       a.name        AS artista,
       rl.title      AS titulo,
       LEAST(COALESCE(e.weight, 9),
             CASE WHEN es.artist_mbid IS NOT NULL THEN 3 ELSE 9 END) AS prioridad
FROM releases rl
JOIN artists a ON a.mbid = rl.artist_mbid
LEFT JOIN peso e ON e.nombre = lower(a.name)
LEFT JOIN escuchados es ON es.artist_mbid = a.mbid
WHERE rl.primary_type = 'Album'
  AND rl.mbid = rl.release_group_mbid
  AND NOT EXISTS (
      SELECT 1 FROM recordings r WHERE r.release_mbid = rl.mbid
  )
ORDER BY prioridad, rl.first_release_date NULLS LAST
"""

# hydrate_recordings inserta con release_mbid = release_group_mbid y hay FK
# contra releases(mbid). Donde difieren, el INSERT reventaría.
EXCLUIDOS_SQL = """
SELECT count(*) FROM releases
WHERE primary_type = 'Album' AND mbid IS DISTINCT FROM release_group_mbid
"""


async def main(limit: int | None, desde_prioridad: int) -> int:
    await init_pool()
    try:
        antes = await fetchval("SELECT count(*) FROM recordings")
        excluidos = await fetchval(EXCLUIDOS_SQL)
        if excluidos:
            logger.warning(
                "%d releases quedan afuera (mbid != release_group_mbid). "
                "Necesitan tratamiento aparte.", excluidos)

        filas = await fetch(PENDIENTES_SQL)
        filas = [f for f in filas if f["prioridad"] <= desde_prioridad]
        if limit:
            filas = filas[:limit]

        total = len(filas)
        if not total:
            logger.info("nada pendiente. recordings actuales: %d", antes)
            return 0

        logger.info("recordings al inicio: %d", antes)
        logger.info("releases a procesar: %d (~%d min a 1 req/s)",
                    total, total // 60 + 1)

        t0 = time.monotonic()
        ok = vacios = errores = nuevos = 0

        for i, f in enumerate(filas, 1):
            try:
                tracks = await hydrate_recordings(f["mbid"])
            except asyncio.CancelledError:
                raise
            except Exception:
                errores += 1
                logger.exception("falló %s — %s", f["artista"], f["titulo"])
                continue

            if tracks:
                ok += 1
                nuevos += len(tracks)
            else:
                vacios += 1
                logger.debug("sin tracklist: %s — %s", f["artista"], f["titulo"])

            if i % 25 == 0 or i == total:
                transcurrido = time.monotonic() - t0
                ritmo = i / transcurrido if transcurrido else 0
                restante = (total - i) / ritmo / 60 if ritmo else 0
                logger.info(
                    "%d/%d — ok:%d vacios:%d err:%d tracks:%d — faltan ~%.0f min",
                    i, total, ok, vacios, errores, nuevos, restante)

        despues = await fetchval("SELECT count(*) FROM recordings")
        logger.info("listo. recordings: %d → %d (+%d)",
                    antes, despues, despues - antes)
        return 0
    finally:
        await close_pool()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=None,
                   help="procesar solo N releases (para probar)")
    p.add_argument("--prioridad", type=int, default=9,
                   help="1=vinilos, 2=canon, 3=ya escuchados, 9=todo")
    args = p.parse_args()

    try:
        sys.exit(asyncio.run(main(args.limit, args.prioridad)))
    except KeyboardInterrupt:
        logger.info("interrumpido — volvé a correrlo y retoma donde quedó")
        sys.exit(130)