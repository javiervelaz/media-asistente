"""Marca tu colección de vinilos en `ephemerides` con weight=1.

`WEIGHT_COLECCION = 1` está definido en hydrator.py desde el día uno y no lo
usa nadie: la tabla tiene el canon (los 808 originales, weight=2) y lo que
trajo el uso (weight=3), pero el estante nunca entró. Sobre ese weight se
apoyan el scoring de local_search, el objetivo de colección, `nunca_escuchado`
y el endpoint /playlist/objetivo.

ENTRADA — un archivo de texto, un disco por línea. Acepta:

    Sumo — Divididos por la Felicidad
    Sumo - Divididos por la Felicidad
    Sumo; Divididos por la Felicidad
    Sumo,Divididos por la Felicidad

y también el CSV que exporta Discogs (detecta las columnas Artist y Title).
Las líneas vacías y las que empiezan con # se ignoran.

POR DEFECTO NO ESCRIBE NADA: muestra qué haría. Con --aplicar marca los que
matchearon. Con --hidratar además trae de MusicBrainz los que no están.

Uso:
    python -m scripts.cargar_coleccion vinilos.txt
    python -m scripts.cargar_coleccion vinilos.txt --aplicar
    python -m scripts.cargar_coleccion vinilos.txt --aplicar --hidratar
"""
import asyncio
import csv
import logging
import re
import sys
from pathlib import Path

from app.db import close_pool, execute, fetch, fetchrow

logging.basicConfig(level=logging.WARNING, format="%(message)s")

SIM_ARTISTA = 0.55
SIM_ALBUM = 0.45
SEPARADORES = re.compile(r"\s+[—–-]\s+|\s*[;|]\s*|\s*,\s*")


def _parsear(path: Path) -> list[tuple[str, str]]:
    texto = path.read_text(encoding="utf-8", errors="replace")

    # Discogs exporta CSV con cabecera; si está, se usa eso.
    primera = texto.splitlines()[0] if texto.strip() else ""
    if "," in primera and "artist" in primera.lower() and "title" in primera.lower():
        filas = []
        for row in csv.DictReader(texto.splitlines()):
            claves = {k.lower().strip(): v for k, v in row.items() if k}
            a, t = claves.get("artist"), claves.get("title")
            if a and t:
                filas.append((a.strip(), t.strip()))
        return filas

    filas = []
    for linea in texto.splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#"):
            continue
        partes = SEPARADORES.split(linea, maxsplit=1)
        if len(partes) == 2 and all(p.strip() for p in partes):
            filas.append((partes[0].strip(), partes[1].strip()))
        else:
            filas.append((linea, ""))     # sin álbum: se reporta aparte
    return filas


async def _buscar(artista: str, album: str) -> dict | None:
    """El disco en ephemerides, por similitud de artista y título."""
    if not album:
        return None
    return await fetchrow(
        """
        SELECT id, artist, album, weight, mbid,
               similarity(artist, $1) AS sa, similarity(album, $2) AS sb
        FROM ephemerides
        WHERE artist % $1 AND album % $2
          AND similarity(artist, $1) >= $3 AND similarity(album, $2) >= $4
        ORDER BY sa + sb DESC
        LIMIT 1
        """, artista, album, SIM_ARTISTA, SIM_ALBUM)


async def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    aplicar = "--aplicar" in sys.argv
    hidratar = "--hidratar" in sys.argv
    if not args:
        print(__doc__)
        return

    path = Path(args[0])
    if not path.exists():
        print(f"No existe: {path}")
        return

    try:
        discos = _parsear(path)
        print(f"{len(discos)} líneas leídas de {path.name}\n")

        encontrados, faltantes, sin_album = [], [], []
        for artista, album in discos:
            if not album:
                sin_album.append(artista)
                continue
            row = await _buscar(artista, album)
            (encontrados if row else faltantes).append(
                (artista, album, dict(row) if row else None))

        print("=" * 70)
        print(f"YA EN LA BASE: {len(encontrados)}")
        print("=" * 70)
        for artista, album, row in encontrados[:20]:
            marca = "ya w1" if row["weight"] == 1 else f"w{row['weight']} → w1"
            print(f"  {marca:9} {row['artist']} — {row['album']}")
            if row["artist"].lower() != artista.lower():
                print(f"            (pediste {artista!r})")
        if len(encontrados) > 20:
            print(f"  … y {len(encontrados) - 20} más")

        if faltantes:
            print()
            print("=" * 70)
            print(f"NO ESTÁN EN LA BASE: {len(faltantes)}")
            print("=" * 70)
            for artista, album, _ in faltantes[:20]:
                print(f"  {artista} — {album}")
            if len(faltantes) > 20:
                print(f"  … y {len(faltantes) - 20} más")
            if not hidratar:
                print("\n  (con --hidratar se traen de MusicBrainz; tarda ~1 s "
                      "cada uno por el rate limit)")

        if sin_album:
            print()
            print(f"SIN ÁLBUM ({len(sin_album)}): líneas que no pude separar en "
                  "artista y álbum")
            for a in sin_album[:10]:
                print(f"  {a!r}")

        if not aplicar:
            print()
            print("=" * 70)
            print("DRY RUN — no se escribió nada.")
            print("Revisá los matches de arriba: la búsqueda es por similitud y")
            print("un disco mal matcheado queda marcado como tuyo para siempre.")
            print("Si están bien, repetí con --aplicar")
            return

        ids = [row["id"] for _, _, row in encontrados if row["weight"] != 1]
        if ids:
            await execute(
                "UPDATE ephemerides SET weight = 1 WHERE id = ANY($1::int[])", ids)
            print(f"\n{len(ids)} discos marcados como tuyos (weight=1)")

        if hidratar and faltantes:
            from app.hydrator import WEIGHT_COLECCION, resolve_artist
            from app.musicbrainz import mb
            print(f"\nHidratando {len(faltantes)} desde MusicBrainz…")
            ok = 0
            for i, (artista, album, _) in enumerate(faltantes, 1):
                try:
                    if await resolve_artist(artista, WEIGHT_COLECCION):
                        ok += 1
                except Exception as e:
                    print(f"  falló {artista}: {e}")
                if i % 10 == 0:
                    print(f"  {i}/{len(faltantes)}")
            await mb.close()
            print(f"{ok} artistas hidratados")
            print("\nOJO: hidratar trae la discografía completa del artista con "
                  "weight=1.\nVolvé a correr sin --hidratar para ver si hay que "
                  "bajarle el peso\na los discos que NO tenés en vinilo.")

        fin = await fetchrow(
            "SELECT count(*) FILTER (WHERE weight = 1) AS w1, count(*) AS total "
            "FROM ephemerides")
        print(f"\nephemerides: {fin['w1']} con weight=1 de {fin['total']}")
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
