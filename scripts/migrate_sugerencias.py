"""Tabla de sugerencias proactivas (H5) y preferencias por sala.

**Por que una tabla y no el mecanismo de oferta del H2.1.** Ese vive en
`SessionState`: en memoria, LRU de 200 sesiones, TTL de 20 minutos, y la
`Oferta` dura 5. Sirve para una conversacion. Para un push no sirve nada: el
mensaje sale a las 20:00 y la respuesta llega a las 20:40, o a la noche, o
despues de un `systemctl restart`. Es la misma clase de bug que `_current`
perdiendose en el reinicio.

La fila es, ademas, gratis, las otras dos cosas que el bloque necesita:

  1. la memoria de lo ya sugerido — sin ella el mismo disco del estante llega
     siete dias seguidos;
  2. la tasa de aceptacion, que es la unica medida honesta de si la
     proactividad sirve o es ruido.

`mbids` guarda lo que se OFRECIO, no como se eligio. Misma leccion que el
H2.1: si la fila guardara el criterio y la playlist se recalculara al
aceptar, el usuario podria recibir algo distinto de lo que vio — y ofrecer
una cosa para reproducir otra es mentir. Para `kind='objetivo'` va vacio a
proposito: ese mensaje no nombra discos, ofrece "armo algo para el objetivo",
asi que no promete una lista concreta y se arma al aceptar.

`aceptada` es tri-estado: NULL (no contesto) es informacion distinta de false
(dijo que no). Un false es un criterio malo; un NULL sistematico es un canal
que dejaste de leer, y son decisiones opuestas.

Idempotente. Uso:  python -m scripts.migrate_sugerencias
"""
import asyncio
import logging

from app.db import close_pool, execute, fetchrow

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("migrate")

DDL = [
    """
    CREATE TABLE IF NOT EXISTS sugerencias (
      id            bigserial PRIMARY KEY,
      room_id       text NOT NULL,
      kind          text NOT NULL,
      etiqueta      text NOT NULL,
      mbids         text[] NOT NULL DEFAULT '{}',
      goal_id       int,
      enviada_at    timestamptz NOT NULL DEFAULT now(),
      respondida_at timestamptz,
      aceptada      bool
    )
    """,

    # Sin CHECK en `kind`: `playlists.source` ya enseño que un constraint
    # sobre un vocabulario que todavia esta creciendo solo rompe los scripts
    # de test.

    "CREATE INDEX IF NOT EXISTS sugerencias_room_idx "
    "  ON sugerencias (room_id, enviada_at DESC)",

    "CREATE INDEX IF NOT EXISTS sugerencias_kind_idx "
    "  ON sugerencias (kind, enviada_at DESC)",

    # El antipatron 2 en la base: "no repetir este mbid" es un @> sobre el
    # array, y sin GIN eso es un seq scan por sugerencia.
    "CREATE INDEX IF NOT EXISTS sugerencias_mbids_idx "
    "  ON sugerencias USING gin (mbids)",

    # Sin contestar: la guarda de "no apilar" la consulta en cada envio.
    "CREATE INDEX IF NOT EXISTS sugerencias_pendientes_idx "
    "  ON sugerencias (room_id, enviada_at DESC) WHERE aceptada IS NULL",

    # Preferencias por sala. Tabla propia y NO `alarm_config`: esa tiene
    # abierto el lio de `alarm_time` vs `time` entre el comando y los dos
    # crons (hallazgo 1 de la auditoria en bitacora-harness.md) y no hay
    # ninguna razon para heredarlo.
    """
    CREATE TABLE IF NOT EXISTS sala_prefs (
      room_id        text PRIMARY KEY,
      silencio_hasta timestamptz
    )
    """,
]


async def main() -> None:
    try:
        await _migrar()
    finally:
        await close_pool()


async def _migrar() -> None:
    for ddl in DDL:
        await execute(ddl)
        log.info("ok: %s", " ".join(ddl.split())[:72])

    r = await fetchrow(
        "SELECT count(*) AS n, count(*) FILTER (WHERE aceptada) AS ok, "
        "       count(*) FILTER (WHERE aceptada IS NULL) AS sin_contestar "
        "FROM sugerencias")
    log.info("sugerencias: %d filas (%d aceptadas, %d sin contestar)",
             r["n"], r["ok"], r["sin_contestar"])

    p = await fetchrow("SELECT count(*) AS n FROM sala_prefs")
    log.info("sala_prefs: %d filas", p["n"])


if __name__ == "__main__":
    asyncio.run(main())
