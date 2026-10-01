#!/usr/bin/env python3
"""Agrega turn_log.cache_write_tokens.

Por que: el curador ya medía las escrituras de cache y las imprimía en el
log, pero nunca llegaban a la base. Una escritura cuesta 1,25x el input, y
medida sobre el 18/09-01/10 fue mas plata que todo lo que turn_log
reportaba junto. Sin esta columna, cualquier analisis de costo subestima
el gasto real y no hay forma de notarlo.

Idempotente: se puede correr dos veces.

    cd ~/media-asistente && .venv/bin/python scripts/migrate_cache_write.py
"""
import asyncio
import sys

sys.path.insert(0, ".")

from app.db import execute, fetchrow, init_pool, close_pool   # noqa: E402

SQL = """
ALTER TABLE turn_log
  ADD COLUMN IF NOT EXISTS cache_write_tokens int NOT NULL DEFAULT 0
"""


async def main() -> int:
    await init_pool()
    try:
        await execute(SQL)
        row = await fetchrow(
            "SELECT count(*) AS n FROM information_schema.columns "
            "WHERE table_name = 'turn_log' "
            "AND column_name = 'cache_write_tokens'")
        if not row or not row["n"]:
            print("MAL: la columna no quedo creada")
            return 1
        print("ok — turn_log.cache_write_tokens listo")
        print("Los turnos viejos quedan en 0: no es que no gastaron,")
        print("es que nadie lo medía. El historico anterior al 01/10")
        print("subestima el costo y hay que leerlo sabiendo eso.")
        return 0
    finally:
        await close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
