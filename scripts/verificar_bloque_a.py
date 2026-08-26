#!/usr/bin/env python3
"""Verifica en la Pi los arreglos del bloque A, contra datos reales de Neon.

  1. El truncador de tool results nunca devuelve JSON invalido.
  2. Diagnostico de play_history: posiciones duplicadas por el race viejo.

Correr desde la raiz del repo, con el venv activado:

    python scripts/verificar_bloque_a.py
"""
import asyncio
import json
import sys

sys.path.insert(0, ".")

from app.curator import MAX_TOOL_RESULT, _truncar
from app.db import close_pool, fetch
from app.tools import get_artist_graph, query_releases

VERDE, ROJO, GRIS, FIN = "\033[32m", "\033[31m", "\033[90m", "\033[0m"


def ok(cond: bool) -> str:
    return f"{VERDE}OK{FIN}" if cond else f"{ROJO}FALLA{FIN}"


def _chequear(nombre: str, payload) -> bool:
    """Un tool result real: pasa por el truncador y tiene que parsear."""
    nuevo = _truncar(payload)
    cabe = len(nuevo) <= MAX_TOOL_RESULT
    try:
        data = json.loads(nuevo)
        valido = True
    except json.JSONDecodeError as e:
        data, valido = None, False
        print(f"    {ROJO}{e}{FIN}")

    # Lo que recibia el modelo antes del arreglo
    viejo = json.dumps(payload, default=str)[:MAX_TOOL_RESULT]
    try:
        json.loads(viejo)
        viejo_valido = True
    except json.JSONDecodeError:
        viejo_valido = False

    print(f"  {nombre}: {len(nuevo)} chars | cabe: {cabe} | parsea: {valido} "
          f"| {ok(cabe and valido)}")
    if isinstance(data, dict) and data.get("truncado"):
        print(f"    {GRIS}nota al modelo: {data.get('nota')}{FIN}")
    if not viejo_valido:
        print(f"    {GRIS}(antes se cortaba invalido en: ...{viejo[-55:]}){FIN}")
    return cabe and valido


async def test_truncador() -> bool:
    print("\n=== 1. Truncador de tool results (datos reales de Neon) ===")
    casos = []

    releases = await query_releases(limit=60)
    casos.append(("query_releases(limit=60)", releases))

    if releases:
        chico = await query_releases(artist_mbids=[releases[0]["artist_mbid"]],
                                     limit=3)
        casos.append(("query_releases(limit=3) — no debe truncarse", chico))

        grafo = await get_artist_graph(releases[0]["artist_mbid"], max_hops=2)
        casos.append((f"get_artist_graph(max_hops=2) — {releases[0]['artist']}",
                      grafo))
    else:
        print(f"  {ROJO}la base no devolvio releases: revisá DATABASE_URL{FIN}")
        return False

    return all(_chequear(n, p) for n, p in casos)


async def test_posiciones() -> bool:
    """El race viejo dejaba dos filas con el mismo (playlist_id, position).

    Las playlists anteriores al fix pueden tener duplicados: es el daño ya
    hecho. Lo que importa es que las NUEVAS salgan limpias.
    """
    print("\n=== 2. play_history: posiciones duplicadas ===")

    filas = await fetch("""
        SELECT p.id::text AS id, p.title, p.source, p.created_at,
               count(*) AS filas,
               count(DISTINCT h.position) AS posiciones,
               count(*) - count(DISTINCT h.position) AS duplicadas
        FROM playlists p
        JOIN play_history h ON h.playlist_id = p.id
        WHERE h.position IS NOT NULL
        GROUP BY p.id, p.title, p.source, p.created_at
        ORDER BY p.created_at DESC
        LIMIT 20
    """)

    if not filas:
        print(f"  {GRIS}sin playlists con posiciones todavia{FIN}")
        return True

    print(f"  {'fecha':<17} {'source':<9} {'filas':>5} {'dup':>4}  titulo")
    sucias_hybrid = 0
    for f in filas:
        dup = f["duplicadas"]
        marca = f"{ROJO}{dup:>4}{FIN}" if dup else f"{VERDE}{dup:>4}{FIN}"
        print(f"  {f['created_at']:%Y-%m-%d %H:%M}  {f['source'] or '-':<9} "
              f"{f['filas']:>5} {marca}  {(f['title'] or '')[:40]}")
        if dup and f["source"] == "hybrid":
            sucias_hybrid += 1

    total_dup = sum(f["duplicadas"] for f in filas)
    print(f"\n  {total_dup} posiciones duplicadas en las ultimas {len(filas)} "
          f"playlists ({sucias_hybrid} en modo hybrid)")
    if total_dup:
        print(f"  {GRIS}Las de ANTES del fix se esperan sucias. Generá una "
              f"playlist hybrid nueva y volvé a correr esto:{FIN}")
        print(f"  {GRIS}la fila de arriba de todo tiene que quedar en 0.{FIN}")
    return True


async def main() -> int:
    try:
        resultados = [await test_truncador(), await test_posiciones()]
    finally:
        await close_pool()

    print()
    if all(resultados):
        print(f"{VERDE}Bloque A verificado.{FIN}")
        return 0
    print(f"{ROJO}Hay fallos.{FIN}")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
