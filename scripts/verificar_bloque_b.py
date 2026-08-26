#!/usr/bin/env python3
"""Verifica el bloque B contra Neon: la senal positiva de reproduccion.

  1. La columna `completed` existe y el scoring la puede leer.
  2. El UPDATE de register_complete marca LA fila correcta (test transaccional,
     con rollback: no ensucia datos).
  3. Estado de la senal: cuanta hay, y cuanto valia el termino viejo.

Correr desde la raiz del repo, con el venv activado:

    python scripts/verificar_bloque_b.py
"""
import asyncio
import sys
import uuid

sys.path.insert(0, ".")

from app.db import close_pool, fetchrow, init_pool

VERDE, ROJO, GRIS, FIN = "\033[32m", "\033[31m", "\033[90m", "\033[0m"


def ok(c: bool) -> str:
    return f"{VERDE}OK{FIN}" if c else f"{ROJO}FALLA{FIN}"


async def test_schema() -> bool:
    print("\n=== 1. Schema ===")
    col = await fetchrow("""
        SELECT data_type, column_default, is_nullable
        FROM information_schema.columns
        WHERE table_name = 'play_history' AND column_name = 'completed'
    """)
    if not col:
        print(f"  columna `completed`: {ROJO}NO EXISTE{FIN}")
        print(f"  {GRIS}corré primero: python -m scripts.migrate_completed{FIN}")
        return False
    bien = col["data_type"] == "boolean" and col["is_nullable"] == "NO"
    print(f"  columna `completed`: {col['data_type']}, "
          f"default {col['column_default']}, null={col['is_nullable']} {ok(bien)}")

    idx = await fetchrow("""
        SELECT indexdef FROM pg_indexes
        WHERE tablename = 'play_history' AND indexname = 'play_history_completed_idx'
    """)
    print(f"  indice parcial: {ok(bool(idx))}")
    return bien


async def test_update() -> bool:
    """El caso dificil: el mismo track dos veces en la playlist, y uno ya skipeado.

    Todo dentro de una transaccion que se revierte.
    """
    print("\n=== 2. El UPDATE marca la fila correcta (rollback al final) ===")
    pool = await init_pool()
    async with pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        try:
            pid = uuid.uuid4()
            # playlists.source tiene un CHECK: en vez de adivinar los valores
            # permitidos, reusamos uno que ya esta en la tabla.
            source = await conn.fetchval(
                "SELECT source FROM playlists WHERE source IS NOT NULL LIMIT 1")
            await conn.execute(
                "INSERT INTO playlists (id, title, source) VALUES ($1, $2, $3)",
                pid, "TEST verificar_bloque_b", source)

            # pos 0: mismo yid, ya skipeado -> no se debe tocar
            # pos 1: mismo yid, limpio     -> este es el que hay que marcar
            # pos 2: mismo yid, limpio     -> queda para el segundo eof
            for pos, skipped in ((0, True), (1, False), (2, False)):
                await conn.execute(
                    """INSERT INTO play_history
                         (playlist_id, position, artist, title, youtube_id, skipped)
                       VALUES ($1,$2,$3,$4,$5,$6)""",
                    pid, pos, "Test Artist", "Test Track", "yidTEST", skipped)

            update = """
                UPDATE play_history
                SET completed = true, skipped = false,
                    played_ms = COALESCE(played_ms, $3)
                WHERE id = (
                    SELECT id FROM play_history
                    WHERE playlist_id = $1 AND youtube_id = $2
                      AND NOT completed AND skipped IS DISTINCT FROM true
                    ORDER BY position NULLS LAST, id
                    LIMIT 1
                )
            """
            await conn.execute(update, pid, "yidTEST", 245000)

            filas = await conn.fetch(
                "SELECT position, completed, skipped, played_ms FROM play_history "
                "WHERE playlist_id = $1 ORDER BY position", pid)
            estado = {r["position"]: (r["completed"], r["skipped"], r["played_ms"])
                      for r in filas}
            print(f"  tras 1 eof: {dict(sorted(estado.items()))}")
            c1 = (estado[0] == (False, True, None)      # el skip intacto
                  and estado[1] == (True, False, 245000)  # marcado el correcto
                  and estado[2] == (False, False, None))
            print(f"  marca la primera fila limpia, no pisa el skip: {ok(c1)}")

            # segundo eof del mismo track -> tiene que caer en pos 2
            await conn.execute(update, pid, "yidTEST", 245000)
            filas = await conn.fetch(
                "SELECT position, completed FROM play_history "
                "WHERE playlist_id = $1 ORDER BY position", pid)
            comp = [r["position"] for r in filas if r["completed"]]
            c2 = comp == [1, 2]
            print(f"  segundo eof cae en la siguiente: completas={comp} {ok(c2)}")

            # tercer eof: no hay mas filas limpias, no debe romper ni marcar el skip
            res = await conn.execute(update, pid, "yidTEST", 245000)
            c3 = res.endswith("0")
            print(f"  tercer eof no marca nada ({res}): {ok(c3)}")

            return c1 and c2 and c3
        finally:
            await tx.rollback()
            print(f"  {GRIS}transacción revertida, la base quedó igual{FIN}")


async def test_senal() -> bool:
    print("\n=== 3. Estado de la señal ===")
    s = await fetchrow("""
        SELECT count(*) AS filas,
               count(*) FILTER (WHERE completed) AS completos,
               count(*) FILTER (WHERE skipped)   AS skips,
               count(*) FILTER (WHERE NOT skipped AND played_ms > 0) AS termino_viejo,
               count(DISTINCT recording_mbid) FILTER (WHERE completed) AS recordings
        FROM play_history
    """)
    print(f"  filas totales:          {s['filas']}")
    print(f"  completos (señal +):    {s['completos']}")
    print(f"  skips     (señal -):    {s['skips']}")
    print(f"  recordings con señal +: {s['recordings']}")
    print(f"  {GRIS}el término viejo del scoring valía: {s['termino_viejo']}{FIN}")
    if s["completos"] == 0:
        print(f"  {GRIS}sin señal positiva todavía: escuchá una playlist entera "
              f"y volvé a correr esto{FIN}")
    return True


async def main() -> int:
    try:
        r = [await test_schema()]
        if r[0]:
            r.append(await test_update())
            r.append(await test_senal())
    finally:
        await close_pool()
    print()
    if all(r):
        print(f"{VERDE}Bloque B verificado.{FIN}")
        return 0
    print(f"{ROJO}Hay fallos.{FIN}")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
