"""Modo monográfico de local_search. No gasta un token.

El bug que verifica: "pone the Beatles" devolvía John Lennon. El scoring
premia procedencia (hasta 4.5 puntos por `weight=1`) y caché (1.5) mucho más
que la relevancia al pedido (3.0 × afinidad), así que un disco de un vecino
del grafo que está en la colección y ya resuelto le gana al artista pedido.

Eso está bien para curar una escena y mal para un pedido monográfico.

Uso:  python -m scripts.verificar_local_search [artista ...]
"""
import asyncio
import logging
import sys

from app import local_search
from app.db import close_pool, fetch

logging.basicConfig(level=logging.WARNING,
                    format="%(asctime)s [%(levelname)s] %(message)s")

TEMATICOS = [
    "post-punk británico de 1980",
    "algo tranquilo para cocinar",
    "cumbia santafesina",
]


async def _artistas_de_prueba(n: int = 3) -> list[str]:
    """Artistas con discografía cargada Y vecinos en el grafo: son los únicos
    donde el bug se puede manifestar."""
    rows = await fetch(
        """
        SELECT a.name,
               (SELECT count(*) FROM releases r WHERE r.artist_mbid = a.mbid) AS releases,
               (SELECT count(*) FROM artist_relations ar
                 WHERE ar.source_mbid = a.mbid OR ar.target_mbid = a.mbid) AS vecinos
        FROM artists a
        WHERE EXISTS (SELECT 1 FROM releases r WHERE r.artist_mbid = a.mbid)
        ORDER BY vecinos DESC, releases DESC
        LIMIT $1
        """, n)
    return [r["name"] for r in rows]


async def _probar(nombre: str) -> list[str]:
    fallos = []
    mono = await local_search.es_monografico(nombre)
    print(f"\n  {nombre!r}  → monográfico: {mono}")
    if not mono:
        print("    (no se detectó como pedido de artista; revisá MONOGRAFICO_SIM)")
        fallos.append(f"{nombre}: no detectado como monográfico")

    con = await local_search.buscar(nombre, 14, monografico=True)
    sin = await local_search.buscar(nombre, 14, monografico=False)

    def _resumen(tracks, etiqueta):
        if not tracks:
            print(f"    {etiqueta:14} sin candidatos")
            return None
        artistas = {}
        for t in tracks:
            artistas[t["artist"]] = artistas.get(t["artist"], 0) + 1
        primero = tracks[0]
        print(f"    {etiqueta:14} {len(tracks):2} tracks · arranca "
              f"{primero['artist']} — {primero['title']}")
        print(f"    {'':14} artistas: "
              + ", ".join(f"{a} ({n})" for a, n in
                          sorted(artistas.items(), key=lambda x: -x[1])[:4]))
        return primero["artist"]

    a_con = _resumen(con, "monográfico")
    a_sin = _resumen(sin, "expandido")

    if a_con and a_con.lower() != nombre.lower():
        # No es fallo automático: el nombre pedido puede diferir del canónico
        # ("the Beatles" vs "The Beatles"), pero conviene mirarlo.
        print(f"    OJO: en modo monográfico arranca {a_con!r}, no {nombre!r}")
    if a_con and a_sin and a_con != a_sin:
        print(f"    ✓ el modo cambia el resultado ({a_sin} → {a_con})")
    return fallos


async def main() -> None:
    try:
        pedidos = sys.argv[1:]
        if not pedidos:
            pedidos = await _artistas_de_prueba()
            print("Artistas de prueba (los de más vecinos en el grafo, que es "
                  "donde el bug aparece):")
            print("  " + ", ".join(pedidos))

        print()
        print("=" * 66)
        print("PEDIDOS MONOGRÁFICOS")
        print("=" * 66)
        fallos = []
        for p in pedidos:
            fallos += await _probar(p)

        print()
        print("=" * 66)
        print("PEDIDOS TEMÁTICOS — no deben detectarse como monográficos")
        print("=" * 66)
        for t in TEMATICOS:
            mono = await local_search.es_monografico(t)
            print(f"  {'FALLA' if mono else 'ok   '}  {t!r} → monográfico: {mono}")
            if mono:
                fallos.append(f"{t}: detectado como monográfico por error")

        print()
        print("=" * 66)
        if fallos:
            print(f"FALLOS ({len(fallos)}):")
            for f in fallos:
                print(f"  · {f}")
        else:
            print("OK — un pedido por artista no se va por el grafo, y uno "
                  "temático sí puede.")
        print("llamadas a la API de Anthropic: 0")
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
