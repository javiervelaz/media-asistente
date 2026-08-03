"""Hidrata en background los artistas del canon ya migrados."""
import asyncio, logging
from app.db import close_pool, fetch
from app.hydrator import hydrate_artist, WEIGHT_CANON
from app.musicbrainz import mb

logging.basicConfig(level=logging.INFO)


async def main():
    filas = await fetch(
        "SELECT mbid, name FROM artists WHERE crawled_at IS NULL ORDER BY name")
    print(f"{len(filas)} artistas pendientes (~{len(filas) * 3 // 60} min)")
    for i, r in enumerate(filas, 1):
        try:
            await hydrate_artist(r["mbid"], WEIGHT_CANON)
        except Exception as e:
            logging.error("%s: %s", r["name"], e)
        if i % 25 == 0:
            print(f"{i}/{len(filas)}")
    await mb.close()
    await close_pool()


asyncio.run(main())