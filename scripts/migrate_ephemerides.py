"""Migra ephemerides (release-groups ya cargados) al grafo musical.

Por cada fila resuelve el artista vía el mbid de release-group, inserta
artista + release en las tablas nuevas y corrige la fecha con el valor
autoritativo de MusicBrainz.

Uso:  python -m scripts.migrate_ephemerides
"""
import asyncio
import logging

from app.db import close_pool, execute, fetch
from app.hydrator import _month_day, _norm_date
from app.musicbrainz import mb

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("migrate")


async def main() -> None:
    filas = await fetch(
        "SELECT id, artist, album, release_date, mbid "
        "FROM ephemerides WHERE mbid IS NOT NULL ORDER BY id"
    )
    total = len(filas)
    log.info("a procesar: %d filas (~%d min por el rate limit de MusicBrainz)",
             total, int(total * 1.1 // 60))

    artistas: dict[str, str] = {}
    ok = fallos = corregidas = 0

    for i, r in enumerate(filas, 1):
        try:
            rg = await mb.get(f"release-group/{r['mbid']}", inc="artist-credits")
        except LookupError:
            log.warning("[%d/%d] mbid inexistente en MB: %s — %s",
                        i, total, r["artist"], r["album"])
            fallos += 1
            continue
        except Exception as e:
            log.error("[%d/%d] %s — %s: %s", i, total, r["artist"], r["album"], e)
            fallos += 1
            continue

        credit = rg.get("artist-credit") or []
        if not credit:
            log.warning("[%d/%d] sin artist-credit: %s", i, total, r["album"])
            fallos += 1
            continue

        a = credit[0]["artist"]

        raw = rg.get("first-release-date")
        fecha = _norm_date(raw) or _norm_date(r["release_date"])
        if not fecha:
            log.warning("[%d/%d] sin fecha usable: %s", i, total, r["album"])
            fallos += 1
            continue

        try:
            await execute(
                "INSERT INTO artists (mbid, name) VALUES ($1,$2) "
                "ON CONFLICT (mbid) DO NOTHING",
                a["id"], a["name"],
            )
            await execute(
                """
                INSERT INTO releases (mbid, release_group_mbid, artist_mbid, title,
                                      first_release_date, primary_type, secondary_types)
                VALUES ($1,$1,$2,$3,$4,$5,$6)
                ON CONFLICT (mbid) DO UPDATE SET
                  first_release_date = EXCLUDED.first_release_date,
                  artist_mbid        = EXCLUDED.artist_mbid,
                  title              = EXCLUDED.title
                """,
                rg["id"], a["id"], rg["title"], fecha,
                rg.get("primary-type"), rg.get("secondary-types", []),
            )

            if _norm_date(r["release_date"]) != fecha:
                log.info("fecha corregida: %s — %s → %s",
                         r["album"], r["release_date"], fecha.isoformat())
                corregidas += 1

            await execute(
                "UPDATE ephemerides SET release_date = $1, month_day = $2 WHERE id = $3",
                fecha.isoformat(), _month_day(raw), r["id"],
            )
        except Exception as e:
            log.error("[%d/%d] error de DB en %s — %s: %s",
                      i, total, r["artist"], r["album"], e)
            fallos += 1
            continue

        artistas[a["id"]] = a["name"]
        ok += 1

        if i % 50 == 0:
            log.info("progreso %d/%d (ok=%d fallos=%d)", i, total, ok, fallos)

    await mb.close()
    await close_pool()

    print(f"\n{'=' * 50}")
    print(f"releases migrados : {ok}")
    print(f"fallos            : {fallos}")
    print(f"fechas corregidas : {corregidas}")
    print(f"artistas únicos   : {len(artistas)}")
    print(f"{'=' * 50}")
    print("\nLos artistas quedaron sin discografía completa ni relaciones.")
    print("Se hidratan solos cuando el curador los pida, o adelantalo con:")
    print("  python -m scripts.hydrate_canon")


if __name__ == "__main__":
    asyncio.run(main())