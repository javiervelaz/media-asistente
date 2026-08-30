"""Telemetria del harness conversacional: una fila por turno.

Sin esta tabla no hay forma de saber si el harness ahorra o gasta. El modo de
falla que detecta es el que no se ve: una intencion que deberia matchear en la
etapa 1 del router y no matchea cae al fallback y gasta en silencio. Sin
turn_log te enteras por la factura.

`model IS NULL` = turno gratis. Esa es la metrica.

Idempotente: se puede correr varias veces.

Uso:  python -m scripts.migrate_turn_log
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
    """
    CREATE TABLE IF NOT EXISTS turn_log (
      id            bigserial PRIMARY KEY,
      session_id    text NOT NULL,
      room_id       text,
      text_in       text NOT NULL,
      intent        text,
      stage         text NOT NULL,
      confidence    real,
      model         text,
      input_tokens  int  NOT NULL DEFAULT 0,
      cached_tokens int  NOT NULL DEFAULT 0,
      output_tokens int  NOT NULL DEFAULT 0,
      latency_ms    int,
      ok            boolean NOT NULL DEFAULT true,
      created_at    timestamptz NOT NULL DEFAULT now()
    )
    """,

    # Sin CHECK en la columna: playlists.source ya ensena que un CHECK sobre
    # un vocabulario que todavia esta creciendo solo rompe los scripts de test.
    """CREATE INDEX IF NOT EXISTS turn_log_created_idx
         ON turn_log (created_at DESC)""",

    # La consulta que importa: que cayo fuera de la etapa 1.
    """CREATE INDEX IF NOT EXISTS turn_log_stage_idx
         ON turn_log (stage, created_at DESC)""",

    # Costo por sesion.
    """CREATE INDEX IF NOT EXISTS turn_log_session_idx
         ON turn_log (session_id, created_at DESC)""",
]


async def main() -> None:
    # El pool vive en ESTE loop: cerrarlo desde otro asyncio.run() revienta
    # con "Event loop is closed" aunque la migracion haya salido bien.
    try:
        await _migrar()
    finally:
        await close_pool()


async def _migrar() -> None:
    for ddl in DDL:
        await execute(ddl)
        log.info("ok: %s", " ".join(ddl.split())[:70])

    row = await fetchrow(
        "SELECT count(*) AS total, "
        "       count(*) FILTER (WHERE model IS NULL) AS gratis "
        "FROM turn_log")
    log.info("turn_log: %d turnos, %d gratis", row["total"], row["gratis"])

    if row["total"] == 0:
        log.info("tabla vacia: la telemetria arranca con el primer POST /chat")


if __name__ == "__main__":
    asyncio.run(main())
