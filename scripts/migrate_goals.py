"""Objetivos de escucha. El progreso NO se almacena: se deriva.

Un contador se desincroniza de `play_history` en cuanto algo falla a mitad de
camino, y despues no hay forma de saber cual de los dos tiene razon. Todo lo
que se pueda derivar de la senal cruda, se deriva.

Idempotente.

Uso:  python -m scripts.migrate_goals
"""
import asyncio
import logging

from app.db import close_pool, execute, fetch

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("migrate")

DDL = [
    """
    CREATE TABLE IF NOT EXISTS goals (
      id          serial PRIMARY KEY,
      room_id     text NOT NULL DEFAULT 'main',
      kind        text NOT NULL,
      spec        jsonb NOT NULL DEFAULT '{}'::jsonb,
      window_days int  NOT NULL DEFAULT 30,
      active      bool NOT NULL DEFAULT true,
      created_at  timestamptz NOT NULL DEFAULT now()
    )
    """,
    # Sin CHECK en `kind`: mismo criterio que turn_log y la leccion de
    # playlists.source. El vocabulario todavia esta creciendo.
    """CREATE INDEX IF NOT EXISTS goals_activos_idx
         ON goals (room_id) WHERE active""",
    # Un objetivo activo por tipo y sala: declarar dos veces el mismo reemplaza,
    # no acumula.
    """CREATE UNIQUE INDEX IF NOT EXISTS goals_unico_activo_idx
         ON goals (room_id, kind) WHERE active""",
]


async def main() -> None:
    try:
        for ddl in DDL:
            await execute(ddl)
            log.info("ok: %s", " ".join(ddl.split())[:70])

        rows = await fetch(
            "SELECT kind, spec, window_days FROM goals WHERE active "
            "ORDER BY created_at")
        if rows:
            log.info("%d objetivos activos:", len(rows))
            for r in rows:
                log.info("  %s %s (%d dias)", r["kind"], r["spec"],
                         r["window_days"])
        else:
            log.info("sin objetivos todavia — se declaran por chat: "
                     "\"quiero escuchar mas de mi coleccion\"")
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
